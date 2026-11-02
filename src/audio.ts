// Plays the phone's audio inside the plugin context (SharedJSContext), which
// has user activation and therefore a running AudioContext. The overlay
// BrowserView does not, so audio cannot be played from the player page.
//
// Wire format (scrcpy audio socket, relayed as-is by /audio.bin):
//   4 bytes codec id ("opus"; 0 = phone could not capture audio)
//   then per packet: 8 bytes pts/flags (bit 62 = config), 4 bytes size, payload

const FLAG_CONFIG = 0x40;

type Packet = { type: "codec"; id: string; raw: number } | { type: "config" | "frame"; pts: number; data: Uint8Array };

export class ScrcpyAudioParser {
  private buf = new Uint8Array(0);
  private gotCodec = false;

  push(chunk: Uint8Array): Packet[] {
    const merged = new Uint8Array(this.buf.length + chunk.length);
    merged.set(this.buf); merged.set(chunk, this.buf.length);
    this.buf = merged;
    const out: Packet[] = [];
    let off = 0;
    if (!this.gotCodec) {
      if (this.buf.length < 4) return out;
      const dv = new DataView(this.buf.buffer, this.buf.byteOffset, 4);
      out.push({ type: "codec", id: String.fromCharCode(...this.buf.subarray(0, 4)), raw: dv.getUint32(0) });
      this.gotCodec = true; off = 4;
    }
    while (this.buf.length - off >= 12) {
      const dv = new DataView(this.buf.buffer, this.buf.byteOffset + off, 12);
      const b0 = this.buf[off];
      if (b0 & 0x80) { off += 12; continue; }           // session meta (video only, but be safe)
      const size = dv.getUint32(8);
      if (this.buf.length - off - 12 < size) break;
      const hi = dv.getUint32(0) & 0x1fffffff, lo = dv.getUint32(4);
      out.push({ type: (b0 & FLAG_CONFIG) ? "config" : "frame", pts: hi * 4294967296 + lo, data: this.buf.slice(off + 12, off + 12 + size) });
      off += 12 + size;
    }
    this.buf = this.buf.slice(off);
    return out;
  }
}

export class AudioPlayer {
  private ctx: AudioContext | null = null;
  private gain: GainNode | null = null;
  private decoder: AudioDecoder | null = null;
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  private playhead = 0;
  private running = false;
  private attempt = 0;
  onUnavailable?: (reason: string) => void;

  constructor(private url: string, volume: number) {
    this.volume = volume;
  }

  private _volume = 1;
  set volume(v: number) {
    this._volume = Math.max(0, Math.min(1, v));
    if (this.gain) this.gain.gain.value = this._volume;
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.ctx = new AudioContext({ sampleRate: 48000 });
    this.gain = this.ctx.createGain();
    this.gain.gain.value = this._volume;
    this.gain.connect(this.ctx.destination);
    this.ctx.resume().catch(() => {});
    this.loop();
  }

  stop() {
    this.running = false;
    this.reader?.cancel().catch(() => {});
    this.reader = null;
    this.closeDecoder();
    this.ctx?.close().catch(() => {});
    this.ctx = null; this.gain = null;
  }

  private closeDecoder() {
    if (this.decoder && this.decoder.state !== "closed") { try { this.decoder.close(); } catch { /* already closed */ } }
    this.decoder = null;
  }

  private async loop() {
    while (this.running) {
      const why = await this.session();
      if (!this.running) return;
      this.attempt += 1;
      console.log("[scrdecky] audio session ended:", why);
      await new Promise((r) => setTimeout(r, Math.min(1000 * this.attempt, 5000)));
    }
  }

  private async session(): Promise<string> {
    let resp: Response;
    try { resp = await fetch(this.url + "?t=" + Date.now(), { cache: "no-store" }); }
    catch (e) { return "connect failed: " + e; }
    if (!resp.ok || !resp.body) return "HTTP " + resp.status;
    this.reader = resp.body.getReader();
    const parser = new ScrcpyAudioParser();
    let config: Uint8Array | null = null;
    try {
      while (this.running) {
        const { value, done } = await this.reader.read();
        if (done) break;
        for (const pkt of parser.push(value)) {
          if (pkt.type === "codec") {
            if (pkt.raw === 0 || pkt.raw === 1) { this.onUnavailable?.(pkt.raw === 0 ? "Phone could not capture audio (needs Android 11+)" : "Audio configuration error on the phone"); this.running = false; return "audio disabled by device"; }
            if (pkt.id !== "opus") { this.onUnavailable?.("Unexpected audio codec " + pkt.id); this.running = false; return "unsupported codec"; }
          } else if (pkt.type === "config") {
            config = pkt.data;
            this.closeDecoder();
            this.decoder = new AudioDecoder({ output: (d) => this.play(d), error: (e) => console.error("[scrdecky] audio decoder", e) });
            // scrcpy ships the OpusHead as the config packet, which is exactly the WebCodecs description
            this.decoder.configure({ codec: "opus", sampleRate: 48000, numberOfChannels: 2, description: config.length >= 19 ? config : undefined });
          } else if (this.decoder && this.decoder.state === "configured") {
            this.attempt = 0;
            this.decoder.decode(new EncodedAudioChunk({ type: "key", timestamp: pkt.pts, data: pkt.data }));
          }
        }
      }
    } catch (e) { return "stream error: " + e; }
    finally { this.reader = null; this.closeDecoder(); }
    return "stream ended";
  }

  private play(data: AudioData) {
    const ctx = this.ctx, gain = this.gain;
    if (!ctx || !gain) { data.close(); return; }
    const channels = data.numberOfChannels, frames = data.numberOfFrames;
    const buffer = ctx.createBuffer(channels, frames, data.sampleRate);
    if (data.format && data.format.endsWith("-planar")) {
      for (let c = 0; c < channels; c++) {
        const plane = new Float32Array(frames);
        data.copyTo(plane, { planeIndex: c, format: "f32-planar" });
        buffer.copyToChannel(plane, c);
      }
    } else {
      const inter = new Float32Array(frames * channels);
      data.copyTo(inter, { planeIndex: 0, format: "f32" });
      for (let c = 0; c < channels; c++) {
        const plane = new Float32Array(frames);
        for (let i = 0; i < frames; i++) plane[i] = inter[i * channels + c];
        buffer.copyToChannel(plane, c);
      }
    }
    data.close();
    // Schedule back to back; if we fall behind (network burst) or run dry, re-anchor
    // just ahead of "now" so latency stays bounded at the cost of a small glitch.
    const now = ctx.currentTime;
    if (this.playhead < now + 0.02 || this.playhead > now + 0.5) this.playhead = now + 0.06;
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(gain);
    src.start(this.playhead);
    this.playhead += buffer.duration;
  }
}
