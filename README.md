# scrdecky

A [Decky Loader](https://decky.xyz) plugin that mirrors your Android phone screen as a **picture-in-picture overlay** on top of whatever is running in Steam Deck game mode. Keep an eye on chat, a video, or an idle game on your phone while you play.

How it works:

```
Android phone ──USB / Wi-Fi (adb)──▶ scrcpy-server (hardware H.264 encode)
      │
      ▼  adb forward
  plugin backend (python) ──▶ relayed byte-for-byte
      │
      ▼  http://127.0.0.1:<port>/stream.bin
  Steam CEF BrowserView overlay (WebCodecs VideoDecoder → canvas) ──▶ PIP drawn over the game
      │
      ▼  http://127.0.0.1:<port>/audio.bin  (Opus)
  plugin frontend (WebCodecs AudioDecoder → AudioContext) ──▶ phone audio mixed with the game
```

- The H.264 stream encoded on the phone is passed through untouched and decoded by Steam's built-in Chromium with WebCodecs. Every frame is drawn the moment it is decoded, so there is no playback clock to stall when the phone only sends a frame when something changes.
- `adb` (platform-tools) and `scrcpy-server` are bundled with the plugin, so nothing has to be installed on SteamOS.

## Requirements

- Steam Deck (SteamOS 3.x) with Decky Loader v3
- Android 5.0+ phone with **Developer options → USB debugging** enabled
- A USB-C cable (recommended) or both devices on the same Wi-Fi (wireless debugging)

## Install

scrdecky is not listed in the Decky Plugin Store and has not gone through its review. Install it manually, and read [Security](#security) first:

1. Download `scrdecky.zip` from [Releases](https://github.com/2chanhaeng/scrdecky/releases).
2. In Decky settings, turn on **Developer mode** under **General**.
3. Open **Developer** → *Install Plugin from ZIP* and select the file.

## Usage

1. Plug the phone in over USB and accept the *Allow USB debugging* prompt on the phone (tick **Always allow**).
2. Quick Access Menu (… button) → scrdecky → **Refresh devices** → once the phone is listed, **Start PIP**.
3. Position (8 anchors), size, margin and quality can be changed live from the panel. The PIP box follows the phone's rotation automatically.
4. **Sound** is on by default and plays the phone's audio mixed with the game; use the **Volume** slider or turn it off. Audio forwarding needs Android 11+ (older phones mirror video only).

### Wireless (Wi-Fi)

Android 11+ (recommended, no typing):
1. In the panel press **Pair with QR code**; a QR code appears.
2. On the phone open *Developer options → Wireless debugging → Pair device with QR code* and scan it.
3. The plugin finds the phone over mDNS, pairs and connects automatically. Next time just press **Connect paired phones** (or **Start PIP**, which tries this on its own when nothing is on USB).

Fallbacks under *Manual connection*:
- Android 11+: type the `IP:port` and 6-digit code from *Pair device with pairing code*, then the `IP:port` from the Wireless debugging screen into **Connect**.
- Android 10 and older: connect over USB once, press **Enable TCP/IP mode** (port 5555), then connect to `phone-ip:5555`.

## Known limitations

- Audio is played from the plugin context, not the overlay page, because Steam's embedded browser views block autoplay without a user gesture. It is Opus-encoded on the phone and decoded with WebCodecs; apps that block audio capture (DRM playback) come through silent.
- Video is decoded in software (Steam's browser reports no hardware H.264 decoder for WebCodecs), so the High preset costs noticeably more CPU than Low or Medium.
- The overlay is view-only; there is no touch control of the phone.
- On the first USB connection the phone must approve the Deck. A device shown as `unauthorized` means the prompt is still waiting on the phone.

## Security

- The plugin backend runs as root, which lets the bundled `adb` reach USB devices without extra system configuration.
- A device authorized for USB or wireless debugging gives the Deck shell-level access to the phone, not just its screen. Turn debugging off when you are not using the plugin.
- To remove access, use *Revoke USB debugging authorizations* in Developer options and remove the Deck from the paired devices under Wireless debugging.

## Development

```bash
npm i --legacy-peer-deps
scripts/fetch-bins.sh  # downloads and verifies bin/adb and bin/scrcpy-server
npm run build          # produces dist/index.js
scripts/package.sh     # builds out/scrdecky.zip for manual install
```

The binaries are not stored in git. `scripts/fetch-bins.sh` pins their versions and SHA-256 hashes, and `bin/VERSIONS` records them. The scrcpy server version must match `SCRCPY_VERSION` in `main.py`.

## License

MIT, Copyright (c) 2026 ChanHaeng Lee. See [LICENSE](LICENSE). Third-party components are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
