"""The HTML player page loaded inside the Steam BrowserView overlay.

It pulls /stream.bin (the scrcpy video socket relayed untouched: 4-byte codec id,
then 12-byte headers for session meta / frames) and decodes each H.264 frame
with WebCodecs the moment it arrives, drawing it to a canvas. There is no
playback clock, so a phone that only sends a frame when something changes is
shown correctly instead of stalling.
"""

PLAYER_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>scrdecky</title>
<style>
  html, body { margin: 0; padding: 0; width: 100%; height: 100%; background: #000; overflow: hidden; }
  canvas { width: 100%; height: 100%; object-fit: contain; background: #000; display: block; }
  #msg {
    position: absolute; left: 0; right: 0; top: 50%; transform: translateY(-50%);
    color: #ddd; font: 14px/1.4 sans-serif; text-align: center; padding: 0 12px;
    text-shadow: 0 1px 2px #000; pointer-events: none;
  }
  #msg.hidden { display: none; }
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="msg">Connecting…</div>
<script>
// --- parser (kept dependency-free so it can also be unit tested in node) ---
const FLAG_SESSION = 0x80, FLAG_CONFIG = 0x40, FLAG_KEY = 0x20;   // bits of byte 0 of a header
class ScrcpyParser {
  constructor() { this.buf = new Uint8Array(0); this.gotCodec = false; }
  push(chunk) {
    const merged = new Uint8Array(this.buf.length + chunk.length);
    merged.set(this.buf); merged.set(chunk, this.buf.length);
    this.buf = merged;
    const out = [];
    let off = 0;
    if (!this.gotCodec) {
      if (this.buf.length < 4) return out;
      out.push({ type: 'codec', id: String.fromCharCode(...this.buf.subarray(0, 4)) });
      this.gotCodec = true; off = 4;
    }
    while (this.buf.length - off >= 12) {
      const dv = new DataView(this.buf.buffer, this.buf.byteOffset + off, 12);
      const b0 = this.buf[off];
      if (b0 & FLAG_SESSION) {
        out.push({ type: 'session', width: dv.getUint32(4), height: dv.getUint32(8) });
        off += 12; continue;
      }
      const size = dv.getUint32(8);
      if (this.buf.length - off - 12 < size) break;
      const hi = dv.getUint32(0) & 0x1fffffff, lo = dv.getUint32(4);
      out.push({
        type: (b0 & FLAG_CONFIG) ? 'config' : 'frame',
        key: !!(b0 & FLAG_KEY),
        pts: hi * 4294967296 + lo,            // microseconds
        data: this.buf.slice(off + 12, off + 12 + size),
      });
      off += 12 + size;
    }
    this.buf = this.buf.slice(off);
    return out;
  }
}
// avc1.PPCCLL from the SPS inside an Annex B config packet
function codecStringFromConfig(data) {
  for (let i = 0; i + 4 < data.length; i++) {
    if (data[i] === 0 && data[i + 1] === 0 && data[i + 2] === 1 && (data[i + 3] & 0x1f) === 7) {
      const hex = (n) => n.toString(16).padStart(2, '0');
      return 'avc1.' + hex(data[i + 4]) + hex(data[i + 5]) + hex(data[i + 6]);
    }
  }
  return 'avc1.64001F';
}
if (typeof module !== 'undefined') module.exports = { ScrcpyParser, codecStringFromConfig };
// --- player ---
if (typeof document !== 'undefined') (() => {
  const canvas = document.getElementById('c');
  const ctx = canvas.getContext('2d');
  const msg = document.getElementById('msg');
  const params = new URLSearchParams(location.search);
  let attempt = 0, stopped = false;
  const show = (t) => { msg.textContent = t; msg.classList.remove('hidden'); };
  const hide = () => msg.classList.add('hidden');

  if (typeof VideoDecoder === 'undefined') { show('WebCodecs not available in this browser'); return; }

  async function run() {
    let reader = null, decoder = null, config = null, gotFrame = false;
    const fail = (why) => {
      if (reader) { try { reader.cancel(); } catch (e) {} reader = null; }
      if (decoder && decoder.state !== 'closed') { try { decoder.close(); } catch (e) {} }
      if (!stopped) retry(why);
    };
    decoder = new VideoDecoder({
      output: (frame) => {
        if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
          canvas.width = frame.displayWidth; canvas.height = frame.displayHeight;
          // the phone rotated (or the stream just started): let the plugin resize the PIP box
          fetch('/video_size?width=' + canvas.width + '&height=' + canvas.height, { cache: 'no-store' }).catch(() => {});
        }
        ctx.drawImage(frame, 0, 0);
        frame.close();
        if (!gotFrame) { gotFrame = true; attempt = 0; hide(); }
      },
      error: (e) => { console.error(e); fail('decoder: ' + e.message); },
    });

    const url = '/stream.bin?' + params.toString() + '&t=' + Date.now();
    let resp;
    try { resp = await fetch(url, { cache: 'no-store' }); } catch (e) { return fail('connect failed'); }
    if (!resp.ok) {
      let detail = ''; try { detail = await resp.text(); } catch (e) {}
      return fail(detail || ('HTTP ' + resp.status));
    }
    reader = resp.body.getReader();
    const parser = new ScrcpyParser();
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const pkt of parser.push(value)) {
          if (pkt.type === 'config') {
            // Annex B stream: no `description`, SPS/PPS travel in-band with the next key frame
            config = pkt.data;
            decoder.configure({ codec: codecStringFromConfig(config), optimizeForLatency: true, hardwareAcceleration: 'no-preference' });
          } else if (pkt.type === 'frame') {
            if (decoder.state !== 'configured') continue;
            let data = pkt.data;
            if (pkt.key && config) {
              data = new Uint8Array(config.length + pkt.data.length);
              data.set(config); data.set(pkt.data, config.length);
            }
            decoder.decode(new EncodedVideoChunk({ type: pkt.key ? 'key' : 'delta', timestamp: pkt.pts, data }));
          }
        }
      }
    } catch (e) { /* stream closed */ }
    fail('stream ended');
  }
  function retry(why) {
    attempt += 1;
    show((why ? why + ' — ' : '') + 'reconnecting…');
    setTimeout(() => { if (!stopped) run(); }, Math.min(1000 * attempt, 5000));
  }
  window.addEventListener('beforeunload', () => { stopped = true; });
  run();
})();
</script>
</body>
</html>
"""
