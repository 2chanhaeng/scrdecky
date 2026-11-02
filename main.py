"""scrdecky backend.

Pipeline:  phone (scrcpy-server, H.264 + 12-byte frame headers) --adb forward-->
           this process --> relayed untouched over HTTP on 127.0.0.1 -->
           player page (WebCodecs VideoDecoder, drawn to a canvas as frames arrive)
The Steam BrowserView overlay loads the player page from that HTTP server.
"""
import asyncio
import os
import random
import secrets
import socket
import sys
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import decky

sys.path.insert(0, os.path.join(decky.DECKY_PLUGIN_DIR, "py_modules"))
from player import PLAYER_HTML  # noqa: E402

BIN_DIR = os.path.join(decky.DECKY_PLUGIN_DIR, "bin")
ADB = os.path.join(BIN_DIR, "adb")
SERVER_JAR = os.path.join(BIN_DIR, "scrcpy-server")
SCRCPY_VERSION = "5.0.1"
DEVICE_JAR = "/data/local/tmp/scrdecky-server.jar"
# Private adb server port so we never fight with a user-started adb on 5037.
ADB_SERVER_PORT = 5137

SERVER_LOG_LINES = 30
# The plugin frontend (which plays the audio) runs on this origin; nothing else may read the stream.
FRONTEND_ORIGIN = "https://steamloopback.host"
CORS = f"Access-Control-Allow-Origin: {FRONTEND_ORIGIN}\r\n"


class StreamError(Exception):
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Plugin:
    http_server: Optional[asyncio.AbstractServer] = None
    http_port: int = 0

    # Active pipeline pieces (one stream at a time)
    serial: Optional[str] = None
    local_port: int = 0
    server_proc: Optional[asyncio.subprocess.Process] = None
    device_reader: Optional[asyncio.StreamReader] = None
    device_writer: Optional[asyncio.StreamWriter] = None
    first_bytes: bytes = b""
    audio_reader: Optional[asyncio.StreamReader] = None
    audio_writer: Optional[asyncio.StreamWriter] = None
    tasks: list
    server_log: list
    last_error: str = ""
    streaming: bool = False

    # QR pairing session (Android 11+ wireless debugging)
    pair: Optional[dict] = None
    pair_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ adb
    def _adb_env(self) -> dict:
        env = dict(os.environ)
        # adb keeps its RSA key in $HOME/.android; pin it to the settings dir so
        # the phone's "always allow" choice survives across reboots and users.
        env["HOME"] = decky.DECKY_PLUGIN_SETTINGS_DIR
        env["ANDROID_ADB_SERVER_PORT"] = str(ADB_SERVER_PORT)
        return env

    async def _adb(self, *args: str, serial: Optional[str] = None, timeout: float = 20) -> tuple[int, str, str]:
        argv = [ADB]
        if serial:
            argv += ["-s", serial]
        argv += list(args)
        proc = await asyncio.create_subprocess_exec(
            *argv, env=self._adb_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "", f"adb {' '.join(args)} timed out"
        return proc.returncode or 0, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()

    async def list_devices(self) -> list[dict]:
        rc, out, err = await self._adb("devices", "-l")
        if rc != 0:
            self.last_error = err or out
            return []
        devices = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            info = {"serial": parts[0], "state": parts[1], "model": ""}
            for p in parts[2:]:
                if p.startswith("model:"):
                    info["model"] = p[6:].replace("_", " ")
            devices.append(info)
        return devices

    async def get_device_info(self, serial: str) -> dict:
        """Screen size + rotation so the frontend can size the PIP box."""
        width, height, rotation = 1080, 1920, 0
        rc, out, _ = await self._adb("shell", "wm", "size", serial=serial)
        if rc == 0:
            for line in out.splitlines():
                # "Override size" (if present) wins over "Physical size"
                if "size:" in line:
                    try:
                        w, h = line.split(":", 1)[1].strip().split("x")
                        width, height = int(w), int(h)
                    except ValueError:
                        pass
        rc, out, _ = await self._adb(
            "shell", "dumpsys input | grep -m1 SurfaceOrientation", serial=serial, timeout=10)
        if rc == 0 and ":" in out:
            try:
                rotation = int(out.rsplit(":", 1)[1].strip())
            except ValueError:
                pass
        return {"width": width, "height": height, "rotation": rotation}

    async def adb_connect(self, address: str) -> dict:
        rc, out, err = await self._adb("connect", address.strip())
        ok = rc == 0 and "connected" in out and "cannot" not in out
        return {"ok": ok, "message": out or err}

    async def adb_pair(self, address: str, code: str) -> dict:
        rc, out, err = await self._adb("pair", address.strip(), code.strip())
        return {"ok": rc == 0 and "Successfully" in out, "message": out or err}

    async def adb_tcpip(self, serial: str) -> dict:
        rc, out, err = await self._adb("tcpip", "5555", serial=serial)
        return {"ok": rc == 0, "message": out or err}

    async def _mdns_services(self) -> list[tuple[str, str, str]]:
        """(instance name, service type, host:port) for every service adb's mDNS sees."""
        rc, out, _ = await self._adb("mdns", "services", timeout=15)
        found = []
        if rc != 0:
            return found
        for line in out.splitlines():
            parts = line.split("\t") if "\t" in line else line.split()
            if len(parts) >= 3 and parts[-2].startswith("_adb-"):
                found.append((parts[0], parts[-2], parts[-1]))
        return found

    async def mdns_connect(self) -> dict:
        """Connect every already-paired phone that advertises itself on the LAN."""
        targets = [addr for _, svc, addr in await self._mdns_services() if svc == "_adb-tls-connect._tcp"]
        if not targets:
            return {"ok": False, "message": "No wireless-debugging phones found on this network"}
        messages = []
        ok = False
        for addr in targets:
            r = await self.adb_connect(addr)
            ok = ok or r["ok"]
            messages.append(r["message"])
        return {"ok": ok, "message": "; ".join(messages)}

    # ---------------------------------------------------------- QR pairing
    # The phone's "Pair device with QR code" screen expects a Wi-Fi style QR:
    # WIFI:T:ADB;S:<service name>;P:<password>;;  After scanning, the phone
    # advertises _adb-tls-pairing._tcp under that service name; we find it via
    # mDNS, pair with the password, then connect to its _adb-tls-connect port.
    async def _set_pair(self, **patch: Any) -> None:
        if self.pair is None:
            return
        self.pair.update(patch)
        await decky.emit("scrdecky_pair", self.pair)

    async def qr_pair_start(self) -> dict:
        await self.qr_pair_cancel()
        name = "scrdecky-" + secrets.token_hex(3)
        password = secrets.token_hex(4)
        self.pair = {
            "name": name, "password": password,
            "text": f"WIFI:T:ADB;S:{name};P:{password};;",
            "state": "waiting",
            "message": "On the phone: Developer options → Wireless debugging → Pair device with QR code",
            "address": "",
        }
        self.pair_task = asyncio.create_task(self._pair_loop(name, password))
        return self.pair

    async def qr_pair_status(self) -> Optional[dict]:
        return self.pair

    async def qr_pair_cancel(self) -> None:
        if self.pair_task and not self.pair_task.done():
            self.pair_task.cancel()
        self.pair_task = None
        self.pair = None

    async def _pair_loop(self, name: str, password: str) -> None:
        try:
            deadline = time.monotonic() + 180
            pair_addr = ""
            while time.monotonic() < deadline and not pair_addr:
                for inst, svc, addr in await self._mdns_services():
                    if svc == "_adb-tls-pairing._tcp" and inst == name:
                        pair_addr = addr
                        break
                if not pair_addr:
                    await asyncio.sleep(1)
            if not pair_addr:
                await self._set_pair(state="error", message="Timed out waiting for the phone to scan the code")
                return

            await self._set_pair(state="pairing", message=f"Pairing with {pair_addr}…", address=pair_addr)
            r = await self.adb_pair(pair_addr, password)
            if not r["ok"]:
                await self._set_pair(state="error", message="Pairing failed: " + r["message"])
                return

            await self._set_pair(state="connecting", message="Paired. Connecting…")
            host = pair_addr.rsplit(":", 1)[0]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                candidates = [addr for _, svc, addr in await self._mdns_services()
                              if svc == "_adb-tls-connect._tcp"]
                # the phone we just paired first, then anything else that's paired
                candidates.sort(key=lambda a: 0 if a.rsplit(":", 1)[0] == host else 1)
                for addr in candidates:
                    r = await self.adb_connect(addr)
                    if r["ok"]:
                        await self._set_pair(state="done", message=f"Connected to {addr}", address=addr)
                        return
                await asyncio.sleep(1)
            await self._set_pair(state="error", message="Paired, but could not connect. Make sure Wireless debugging is still on.")
        except asyncio.CancelledError:
            pass
        except Exception as e:  # keep the panel informative instead of silently dying
            decky.logger.exception("pairing failed")
            await self._set_pair(state="error", message=f"Pairing error: {e}")

    # ------------------------------------------------------------ pipeline
    def _log_server(self, line: str) -> None:
        self.server_log.append(line)
        del self.server_log[:-SERVER_LOG_LINES]
        decky.logger.info("[scrcpy-server] %s", line)

    async def _pump_lines(self, stream: asyncio.StreamReader, tag: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if tag == "server":
                self._log_server(text)
            else:
                decky.logger.info("[%s] %s", tag, text)

    async def _emit_status(self) -> None:
        await decky.emit("scrdecky_status", {
            "streaming": self.streaming, "serial": self.serial, "error": self.last_error,
        })

    async def _start_pipeline(self, serial: str, max_size: int, bit_rate: int, max_fps: int, audio: bool) -> None:
        await self._stop_pipeline()
        self.server_log = []
        self.last_error = ""
        self.serial = serial

        rc, out, err = await self._adb("push", SERVER_JAR, DEVICE_JAR, serial=serial, timeout=60)
        if rc != 0:
            raise StreamError(f"adb push failed: {err or out}")

        scid = random.randint(1, 0x7FFFFFFF)
        self.local_port = _free_port()
        rc, out, err = await self._adb(
            "forward", f"tcp:{self.local_port}", f"localabstract:scrcpy_{scid:08x}", serial=serial)
        if rc != 0:
            raise StreamError(f"adb forward failed: {err or out}")

        server_cmd = " ".join([
            f"CLASSPATH={DEVICE_JAR}", "app_process", "/", "com.genymobile.scrcpy.Server", SCRCPY_VERSION,
            f"scid={scid:08x}", "log_level=info", "tunnel_forward=true",
            # keep the 12-byte per-frame headers (pts/flags/size) and the session
            # meta (resolution); drop the device name and the dummy byte
            "send_device_meta=false", "send_dummy_byte=false", "send_stream_meta=true", "send_frame_meta=true",
            f"audio={'true' if audio else 'false'}", "audio_codec=opus", "audio_bit_rate=128000",
            "control=false", "cleanup=true", "video_codec=h264",
            f"max_size={max_size}", f"video_bit_rate={bit_rate}", f"max_fps={max_fps}",
        ])
        self.server_proc = await asyncio.create_subprocess_exec(
            ADB, "-s", serial, "shell", server_cmd, env=self._adb_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        self.tasks.append(asyncio.create_task(self._pump_lines(self.server_proc.stdout, "server")))

        # adb accepts the forwarded TCP connection immediately and only then dials
        # the device socket, so a connection that closes without data just means
        # the server is not listening yet. Retry until bytes arrive. The server
        # accepts the video socket first and, when audio is on, waits for the
        # audio socket before it starts streaming, so both are opened together.
        first = b""
        deadline = time.monotonic() + 12
        while True:
            if self.server_proc.returncode is not None:
                raise StreamError("scrcpy server exited: " + (self.server_log[-1] if self.server_log else "no output"))
            audio_conn = None
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.local_port)
                if audio:
                    audio_conn = await asyncio.open_connection("127.0.0.1", self.local_port)
                first = await asyncio.wait_for(reader.read(65536), 5)
                if first:
                    self.device_writer = writer
                    if audio_conn:
                        self.audio_reader, self.audio_writer = audio_conn
                    break
                writer.close()
                if audio_conn:
                    audio_conn[1].close()
            except (OSError, asyncio.TimeoutError):
                if audio_conn:
                    audio_conn[1].close()
            if time.monotonic() > deadline:
                raise StreamError("timed out waiting for video from the phone: " +
                                  (self.server_log[-1] if self.server_log else "no server output"))
            await asyncio.sleep(0.25)

        self.device_reader = reader
        self.first_bytes = first
        self.streaming = True
        await self._emit_status()

    async def _stop_pipeline(self) -> None:
        was_streaming = self.streaming
        self.streaming = False
        for t in self.tasks:
            t.cancel()
        self.tasks = []
        if self.device_writer:
            self.device_writer.close()
            self.device_writer = None
        self.device_reader = None
        self.first_bytes = b""
        if self.audio_writer:
            self.audio_writer.close()
            self.audio_writer = None
        self.audio_reader = None
        proc = self.server_proc
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        self.server_proc = None
        if self.local_port and self.serial:
            await self._adb("forward", "--remove", f"tcp:{self.local_port}", serial=self.serial, timeout=5)
        self.local_port = 0
        if was_streaming:
            await self._emit_status()

    # ---------------------------------------------------------------- http
    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            writer.close()
            return
        request_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
        parts = request_line.split()
        path = parts[1] if len(parts) >= 2 else "/"
        url = urlsplit(path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        headers = {}
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.decode(errors="replace").partition(":")
            headers[name.strip().lower()] = value.strip()

        def respond(status: str, ctype: str, body: bytes) -> None:
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                f"Cache-Control: no-store\r\n{CORS}Connection: close\r\n\r\n".encode() + body)

        try:
            if not self._request_allowed(url.path, headers):
                respond("403 Forbidden", "text/plain", b"forbidden")
            elif url.path == "/":
                respond("200 OK", "text/html; charset=utf-8", PLAYER_HTML.encode())
            elif url.path == "/stream.bin":
                await self._serve_stream(writer, query)
            elif url.path == "/audio.bin":
                await self._serve_audio(writer)
            elif url.path == "/video_size":
                await self._video_size(query)
                respond("200 OK", "text/plain", b"ok")
            else:
                respond("404 Not Found", "text/plain", b"not found")
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    def _request_allowed(self, path: str, headers: dict) -> bool:
        """Only the overlay page and the plugin frontend may touch this server.

        The Host check stops DNS rebinding; the Origin/Sec-Fetch-Site checks stop
        other websites open on the Deck from starting or reading the stream.
        """
        if headers.get("host") != f"127.0.0.1:{self.http_port}":
            return False
        if path == "/":
            return True
        own_origin = f"http://127.0.0.1:{self.http_port}"
        origin = headers.get("origin")
        if origin is not None:
            return origin in (own_origin, FRONTEND_ORIGIN)
        return headers.get("sec-fetch-site", "none") in ("same-origin", "none")

    async def _video_size(self, query: dict) -> None:
        try:
            width, height = int(query["width"]), int(query["height"])
        except (KeyError, ValueError):
            return
        if width > 0 and height > 0:
            await decky.emit("scrdecky_video_size", width, height)

    async def _serve_stream(self, writer: asyncio.StreamWriter, query: dict) -> None:
        serial = query.get("serial", "")
        if not serial:
            devices = [d for d in await self.list_devices() if d["state"] == "device"]
            if not devices:
                self.last_error = "no authorized device connected"
                await self._emit_status()
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain\r\n" + CORS.encode() +
                             b"Connection: close\r\n\r\nno authorized device connected")
                return
            serial = devices[0]["serial"]
        try:
            await self._start_pipeline(
                serial,
                max_size=int(query.get("max_size", 800)),
                bit_rate=int(query.get("bit_rate", 4_000_000)),
                max_fps=int(query.get("max_fps", 30)),
                audio=query.get("audio", "0") == "1",
            )
        except StreamError as e:
            self.last_error = str(e)
            decky.logger.error("stream start failed: %s", e)
            await self._stop_pipeline()
            await self._emit_status()
            body = str(e).encode()
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain\r\n" + CORS.encode() +
                         b"Connection: close\r\n\r\n" + body)
            return

        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                     b"Cache-Control: no-store\r\n" + CORS.encode() + b"Connection: close\r\n\r\n")
        reader = self.device_reader
        writer.write(self.first_bytes)
        try:
            while reader is self.device_reader:
                chunk = await reader.read(256 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        finally:
            if reader is self.device_reader:
                await self._stop_pipeline()

    async def _serve_audio(self, writer: asyncio.StreamWriter) -> None:
        """Relay the scrcpy audio socket (4-byte codec id, then 12-byte frame headers + Opus packets)."""
        reader = self.audio_reader
        if reader is None:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain\r\n" + CORS.encode() +
                         b"Connection: close\r\n\r\nno audio stream running")
            return
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                     b"Cache-Control: no-store\r\n" + CORS.encode() + b"Connection: close\r\n\r\n")
        while reader is self.audio_reader:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()

    # ------------------------------------------------------------ frontend
    async def get_status(self) -> dict:
        return {
            "port": self.http_port, "streaming": self.streaming, "serial": self.serial,
            "error": self.last_error, "server_log": self.server_log[-5:],
        }

    async def stop_stream(self) -> None:
        await self._stop_pipeline()

    # ----------------------------------------------------------- lifecycle
    async def _main(self) -> None:
        self.tasks = []
        self.server_log = []
        os.chmod(ADB, 0o755)
        self.http_server = await asyncio.start_server(self._handle_http, "127.0.0.1", 0)
        self.http_port = self.http_server.sockets[0].getsockname()[1]
        decky.logger.info("scrdecky http on 127.0.0.1:%d", self.http_port)
        rc, out, err = await self._adb("start-server", timeout=30)
        if rc != 0:
            decky.logger.error("adb start-server failed: %s %s", out, err)

    async def _unload(self) -> None:
        await self.qr_pair_cancel()
        await self._stop_pipeline()
        if self.http_server:
            self.http_server.close()
        await self._adb("kill-server", timeout=10)

    async def _uninstall(self) -> None:
        await self._unload()
