import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  Router,
  SliderField,
  TextField,
  ToggleField,
  findModuleChild,
  getGamepadNavigationTrees,
  staticClasses,
} from "@decky/ui";
import { addEventListener, callable, definePlugin, removeEventListener, routerHook, toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { FaMobileAlt } from "react-icons/fa";
import qrcode from "qrcode-generator";
import { AudioPlayer } from "./audio";

// ----------------------------------------------------------------- backend
type Device = { serial: string; state: string; model: string };
type DeviceInfo = { width: number; height: number; rotation: number };
type Status = { port: number; streaming: boolean; serial: string | null; error: string; server_log: string[] };
type AdbResult = { ok: boolean; message: string };
type PairState = "waiting" | "pairing" | "connecting" | "done" | "error";
type Pair = { name: string; password: string; text: string; state: PairState; message: string; address: string };

const getStatus = callable<[], Status>("get_status");
const listDevices = callable<[], Device[]>("list_devices");
const getDeviceInfo = callable<[serial: string], DeviceInfo>("get_device_info");
const adbConnect = callable<[address: string], AdbResult>("adb_connect");
const adbPair = callable<[address: string, code: string], AdbResult>("adb_pair");
const adbTcpip = callable<[serial: string], AdbResult>("adb_tcpip");
const stopStream = callable<[], void>("stop_stream");
const mdnsConnect = callable<[], AdbResult>("mdns_connect");
const qrPairStart = callable<[], Pair>("qr_pair_start");
const qrPairStatus = callable<[], Pair | null>("qr_pair_status");
const qrPairCancel = callable<[], void>("qr_pair_cancel");

// ------------------------------------------------------------------- store
enum Position { TopLeft, Top, TopRight, Left, Right, BottomLeft, Bottom, BottomRight }

const QUALITY = {
  low: { label: "Low (640p, 2 Mbps, 30 fps)", max_size: 640, bit_rate: 2_000_000, max_fps: 30 },
  medium: { label: "Medium (800p, 4 Mbps, 30 fps)", max_size: 800, bit_rate: 4_000_000, max_fps: 30 },
  high: { label: "High (1080p, 8 Mbps, 60 fps)", max_size: 1080, bit_rate: 8_000_000, max_fps: 60 },
};
type QualityKey = keyof typeof QUALITY;

type Settings = {
  position: Position;
  size: number;      // 0.5 .. 1.5
  margin: number;    // px in the 854x534 logical screen
  quality: QualityKey;
  serial: string;    // "" = first available device
  sound: boolean;
  volume: number;    // 0 .. 100
};
type State = Settings & {
  open: boolean;
  port: number;
  video: { width: number; height: number };   // current picture size, follows phone rotation
  refreshTick: number;
};

const SETTINGS_KEY = "scrdecky";
const defaultSettings: Settings = { position: Position.TopRight, size: 1, margin: 20, quality: "medium", serial: "", sound: true, volume: 100 };

function loadSettings(): Settings {
  try { return { ...defaultSettings, ...JSON.parse(localStorage.getItem(SETTINGS_KEY) ?? "{}") }; }
  catch { return defaultSettings; }
}

let state: State = { ...loadSettings(), open: false, port: 0, video: { width: 1080, height: 1920 }, refreshTick: 0 };
const listeners = new Set<(s: State) => void>();
function setState(patch: Partial<State>) {
  state = { ...state, ...patch };
  const { position, size, margin, quality, serial, sound, volume } = state;
  localStorage.setItem(SETTINGS_KEY, JSON.stringify({ position, size, margin, quality, serial, sound, volume }));
  listeners.forEach((l) => l(state));
}
function useStore(): State {
  const [s, setS] = useState(state);
  useEffect(() => { listeners.add(setS); return () => { listeners.delete(setS); }; }, []);
  return s;
}

// ---------------------------------------------------------------- overlay
// Steam composites the gamepad UI over a running game only while something
// requests it; "Notification" is the level toasts use, which is exactly what
// a PIP box wants.
enum UIComposition { Hidden = 0, Notification = 1, Overlay = 2, Opaque = 3, OverlayKeyboard = 4 }
const useUIComposition: (c: UIComposition) => void = findModuleChild((m: any) => {
  if (typeof m !== "object") return undefined;
  for (const prop in m) {
    const f = m[prop];
    if (typeof f === "function") {
      const src = f.toString();
      if (src.includes("AddMinimumCompositionStateRequest") &&
          src.includes("ChangeMinimumCompositionStateRequest") &&
          src.includes("RemoveMinimumCompositionStateRequest") &&
          !src.includes("m_mapCompositionStateRequests")) return f;
    }
  }
});

type Rect = { x: number; y: number; width: number; height: number };

function mainWindow(): any {
  return (Router as any).WindowStore?.GamepadUIMainWindowInstance;
}
function screenSize(): { w: number; h: number } {
  const win = mainWindow()?.m_BrowserWindow;
  return { w: win?.innerWidth || 854, h: win?.innerHeight || 534 };
}

// Steam's side menus live in their own windows; shrink the usable area so the
// PIP box slides out from under an open Quick Access / main menu.
function menuBounds(): { nav: Rect | null; qam: Rect | null } {
  const trees: any[] = getGamepadNavigationTrees() ?? [];
  const winOf = (id: string) => trees.find((t) => t?.id === id)?.m_Root?.m_element?.ownerDocument?.defaultView ?? null;
  const rectOf = (w: any): Rect | null =>
    !w || w.document.hidden ? null : { x: w.screenLeft, y: w.screenTop, width: w.outerWidth, height: w.outerHeight };
  return { nav: rectOf(winOf("MainNavMenuContainer")), qam: rectOf(winOf("QuickAccess-NA")) };
}

function computeBounds(s: State): Rect {
  const { w: W, h: H } = screenSize();
  const { nav, qam } = menuBounds();
  let x = 0, y = 0, width = W, height = H;
  if (nav) { x = nav.width; width = W - nav.width; }
  if (qam) { width = Math.min(width, W - qam.width - x); }
  x += s.margin; y += s.margin; width -= 2 * s.margin; height -= 2 * s.margin;

  const aspect = s.video.width / s.video.height;
  let pw: number, ph: number;
  if (aspect > 1) { pw = W * 0.4 * s.size; ph = pw / aspect; }
  else { ph = H * 0.55 * s.size; pw = ph * aspect; }
  const fit = Math.min(1, width / pw, height / ph);
  pw *= fit; ph *= fit;

  const hAlign = [0, 1, 2, 0, 2, 0, 1, 2][s.position];
  const vAlign = [0, 0, 0, 1, 1, 2, 2, 2][s.position];
  const px = x + (hAlign === 0 ? 0 : hAlign === 1 ? (width - pw) / 2 : width - pw);
  const py = y + (vAlign === 0 ? 0 : vAlign === 1 ? (height - ph) / 2 : height - ph);
  return { x: Math.round(px), y: Math.round(py), width: Math.round(pw), height: Math.round(ph) };
}

function streamUrl(s: State): string {
  const q = QUALITY[s.quality];
  const p = new URLSearchParams({ serial: s.serial, max_size: String(q.max_size), bit_rate: String(q.bit_rate), max_fps: String(q.max_fps), audio: s.sound ? "1" : "0" });
  return `http://127.0.0.1:${s.port}/?${p.toString()}`;
}

function BrowserOverlay({ url, bounds }: { url: string; bounds: Rect }) {
  useUIComposition(UIComposition.Notification);
  const [{ view, browser }] = useState(() => {
    const view = mainWindow().CreateBrowserView("scrdecky");
    return { view, browser: view.GetBrowser() };
  });
  useEffect(() => { view.LoadURL(url); }, [url]);
  useEffect(() => { browser.SetBounds(bounds.x, bounds.y, bounds.width, bounds.height); }, [bounds.x, bounds.y, bounds.width, bounds.height]);
  useEffect(() => { browser.SetVisible(true); return () => view.Destroy(); }, []);
  return null;
}

// Audio is decoded and played here, in the plugin context, not in the overlay page
// (see audio.ts). It follows the video stream's lifetime.
function useAudio(s: State) {
  const [player, setPlayer] = useState<AudioPlayer | null>(null);
  const active = s.open && s.port > 0 && s.sound;
  useEffect(() => {
    if (!active) return;
    const p = new AudioPlayer(`http://127.0.0.1:${s.port}/audio.bin`, s.volume / 100);
    p.onUnavailable = (reason) => toaster.toast({ title: "scrdecky", body: reason });
    // give the video request a moment to start the pipeline before we attach
    const id = setTimeout(() => p.start(), 1500);
    setPlayer(p);
    return () => { clearTimeout(id); p.stop(); setPlayer(null); };
  }, [active, s.port, s.serial, s.quality]);
  useEffect(() => { if (player) player.volume = s.volume / 100; }, [player, s.volume]);
}

function Overlay() {
  const s = useStore();
  useAudio(s);
  const [bounds, setBounds] = useState<Rect>(() => computeBounds(s));
  useEffect(() => {
    const tick = () => setBounds((cur) => {
      const next = computeBounds(state);
      return cur.x === next.x && cur.y === next.y && cur.width === next.width && cur.height === next.height ? cur : next;
    });
    tick();
    const id = setInterval(tick, 250);
    return () => clearInterval(id);
  }, [s.position, s.size, s.margin, s.video]);
  if (!s.open || !s.port) return null;
  return <BrowserOverlay url={streamUrl(s)} bounds={bounds} />;
}

// ------------------------------------------------------------------ panel
async function startPip() {
  const status = await getStatus();
  let devices = await listDevices();
  const pick = () => (state.serial ? devices.find((d) => d.serial === state.serial) : devices.find((d) => d.state === "device"));
  let wanted = pick();
  if (!wanted) {
    // nothing on USB: maybe a previously paired phone is on the Wi-Fi
    const r = await mdnsConnect();
    if (r.ok) { devices = await listDevices(); wanted = pick(); setState({ refreshTick: Date.now() }); }
  }
  if (!wanted) { toaster.toast({ title: "scrdecky", body: "No Android device connected" }); return; }
  if (wanted.state !== "device") { toaster.toast({ title: "scrdecky", body: `Device is "${wanted.state}" — accept the USB debugging prompt on the phone` }); return; }
  // Only a first guess to avoid a visible jump; the first decoded frame sets the real size.
  let video = state.video;
  try {
    const d = await getDeviceInfo(wanted.serial);
    video = d.rotation % 2 === 1 ? { width: d.height, height: d.width } : { width: d.width, height: d.height };
  } catch { /* keep the previous size */ }
  setState({ open: true, port: status.port, video });
}
async function stopPip() {
  setState({ open: false });
  await stopStream();
}

function DevicePanel() {
  const s = useStore();
  const [devices, setDevices] = useState<Device[]>([]);
  const [busy, setBusy] = useState(false);
  const refresh = async () => { setBusy(true); try { setDevices(await listDevices()); } finally { setBusy(false); } };
  useEffect(() => { refresh(); }, [s.refreshTick]);

  const options = [{ data: "", label: "Auto (first device)" }, ...devices.map((d) => ({
    data: d.serial, label: `${d.model || d.serial}${d.state !== "device" ? ` (${d.state})` : ""}`,
  }))];
  const summary = devices.length === 0 ? "No devices found" :
    devices.map((d) => `${d.model || d.serial}: ${d.state}`).join(", ");

  return (
    <PanelSection title="Device">
      <PanelSectionRow>
        <DropdownItem label="Phone" rgOptions={options} selectedOption={s.serial} onChange={(o) => setState({ serial: o.data })} />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={refresh}>Refresh devices</ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <Field label="Found" description={summary} />
      </PanelSectionRow>
    </PanelSection>
  );
}

function PicturePanel() {
  const s = useStore();
  const positions = [
    { data: Position.TopLeft, label: "Top left" }, { data: Position.Top, label: "Top" }, { data: Position.TopRight, label: "Top right" },
    { data: Position.Left, label: "Left" }, { data: Position.Right, label: "Right" },
    { data: Position.BottomLeft, label: "Bottom left" }, { data: Position.Bottom, label: "Bottom" }, { data: Position.BottomRight, label: "Bottom right" },
  ];
  const qualities = (Object.keys(QUALITY) as QualityKey[]).map((k) => ({ data: k, label: QUALITY[k].label }));
  return (
    <PanelSection title="Picture">
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => (s.open ? stopPip() : startPip())}>{s.open ? "Stop PIP" : "Start PIP"}</ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField label="Sound" description="Play the phone's audio (Android 11+)" checked={s.sound} onChange={(v) => setState({ sound: v })} />
      </PanelSectionRow>
      <PanelSectionRow>
        <SliderField label="Volume" value={s.volume} min={0} max={100} step={5} showValue valueSuffix="%" disabled={!s.sound} onChange={(v) => setState({ volume: v })} />
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem label="Position" rgOptions={positions} selectedOption={s.position} onChange={(o) => setState({ position: o.data })} />
      </PanelSectionRow>
      <PanelSectionRow>
        <SliderField label="Size" value={s.size} min={0.5} max={1.5} step={0.05} showValue valueSuffix="x" onChange={(v) => setState({ size: v })} />
      </PanelSectionRow>
      <PanelSectionRow>
        <SliderField label="Margin" value={s.margin} min={0} max={60} step={5} showValue onChange={(v) => setState({ margin: v })} />
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem label="Quality" description="Applied on the next Start PIP" rgOptions={qualities} selectedOption={s.quality} onChange={(o) => setState({ quality: o.data })} />
      </PanelSectionRow>
    </PanelSection>
  );
}

function qrDataUrl(text: string): string {
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  return qr.createDataURL(5, 4);
}

function QrPairPanel() {
  const [pair, setPair] = useState<Pair | null>(null);
  useEffect(() => {
    qrPairStatus().then(setPair).catch(() => {});
    const listener = addEventListener<[Pair]>("scrdecky_pair", (p) => {
      setPair({ ...p });
      if (p.state === "done") setState({ refreshTick: Date.now() });
    });
    return () => removeEventListener("scrdecky_pair", listener);
  }, []);
  const start = async () => setPair(await qrPairStart());
  const cancel = async () => { await qrPairCancel(); setPair(null); };
  const active = pair && (pair.state === "waiting" || pair.state === "pairing" || pair.state === "connecting");

  return (
    <PanelSection title="Wireless (Wi-Fi)">
      <PanelSectionRow>
        <ButtonItem layout="below" description="Android 11+: Developer options → Wireless debugging → Pair device with QR code"
          onClick={active ? cancel : start}>{active ? "Cancel pairing" : "Pair with QR code"}</ButtonItem>
      </PanelSectionRow>
      {pair && pair.state === "waiting" && (
        <PanelSectionRow>
          <div style={{ display: "flex", justifyContent: "center", padding: "8px 0" }}>
            <img src={qrDataUrl(pair.text)} alt="pairing QR code" style={{ width: 220, height: 220, imageRendering: "pixelated", background: "#fff", borderRadius: 6 }} />
          </div>
        </PanelSectionRow>
      )}
      {pair && (
        <PanelSectionRow>
          <Field label={{ waiting: "Waiting for scan", pairing: "Pairing", connecting: "Connecting", done: "Connected", error: "Failed" }[pair.state]} description={pair.message} />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" description="Reconnect phones that were paired before"
          onClick={async () => { const r = await mdnsConnect(); toaster.toast({ title: "scrdecky", body: r.message }); if (r.ok) setState({ refreshTick: Date.now() }); }}>
          Connect paired phones
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

function ManualWirelessPanel() {
  const s = useStore();
  const [address, setAddress] = useState("");
  const [pairAddress, setPairAddress] = useState("");
  const [pairCode, setPairCode] = useState("");
  const [message, setMessage] = useState("");
  const run = async (p: Promise<AdbResult>) => {
    try { const r = await p; setMessage(r.message); if (r.ok) setState({ refreshTick: Date.now() }); }
    catch (e) { setMessage(String(e)); }
  };
  return (
    <PanelSection title="Manual connection">
      <PanelSectionRow>
        <TextField label="Connect to IP:port" value={address} onChange={(e) => setAddress(e.target.value)} />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={!address} onClick={() => run(adbConnect(address))}>Connect</ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <TextField label="Pairing IP:port (Android 11+)" value={pairAddress} onChange={(e) => setPairAddress(e.target.value)} />
      </PanelSectionRow>
      <PanelSectionRow>
        <TextField label="Pairing code" value={pairCode} onChange={(e) => setPairCode(e.target.value)} />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={!pairAddress || !pairCode} onClick={() => run(adbPair(pairAddress, pairCode))}>Pair with code</ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" description="Android 10 and older: run once over USB, then connect to phone-ip:5555"
          onClick={() => run(adbTcpip(s.serial))}>Enable TCP/IP mode (USB)</ButtonItem>
      </PanelSectionRow>
      {message && <PanelSectionRow><Field label="Result" description={message} /></PanelSectionRow>}
    </PanelSection>
  );
}

function StatusPanel() {
  const [status, setStatus] = useState<Status | null>(null);
  useEffect(() => {
    let alive = true;
    const poll = () => getStatus().then((st) => alive && setStatus(st)).catch(() => {});
    poll();
    const id = setInterval(poll, 2000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  if (!status) return null;
  const text = status.streaming ? `Streaming from ${status.serial}` : status.error ? `Error: ${status.error}` : "Idle";
  return (
    <PanelSection title="Status">
      <PanelSectionRow><Field label={text} description={status.server_log.slice(-2).join("\n")} /></PanelSectionRow>
    </PanelSection>
  );
}

function Content() {
  return (<>
    <PicturePanel />
    <DevicePanel />
    <QrPairPanel />
    <ManualWirelessPanel />
    <StatusPanel />
  </>);
}

export default definePlugin(() => {
  routerHook.addGlobalComponent("scrdecky", Overlay);
  const listener = addEventListener<[{ streaming: boolean; serial: string | null; error: string }]>("scrdecky_status", (st) => {
    if (st.error && state.open) toaster.toast({ title: "scrdecky", body: st.error });
  });
  const sizeListener = addEventListener<[width: number, height: number]>("scrdecky_video_size", (width, height) => {
    setState({ video: { width, height } });
  });
  return {
    name: "scrdecky",
    titleView: <div className={staticClasses.Title}>scrdecky</div>,
    content: <Content />,
    icon: <FaMobileAlt />,
    onDismount() {
      removeEventListener("scrdecky_status", listener);
      removeEventListener("scrdecky_video_size", sizeListener);
      routerHook.removeGlobalComponent("scrdecky");
      stopStream().catch(() => {});
    },
  };
});
