#!/usr/bin/env bash
# Downloads the binaries under bin/ at pinned versions and verifies their hashes:
# adb from Android Platform-Tools and the scrcpy server. Run from anywhere.
set -euo pipefail
PLATFORM_TOOLS_VERSION=37.0.1
PLATFORM_TOOLS_SHA256=d230f13842f60f782a8645f9c813f8f845bf36089ea7289f28c48f17979313f1
SCRCPY_VERSION=5.0.1   # must match SCRCPY_VERSION in main.py
SCRCPY_SHA256=764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl -fsSL -o "$TMP/pt.zip" \
  "https://dl.google.com/android/repository/platform-tools_r${PLATFORM_TOOLS_VERSION}-linux.zip"
curl -fsSL -o "$TMP/scrcpy-server" \
  "https://github.com/Genymobile/scrcpy/releases/download/v${SCRCPY_VERSION}/scrcpy-server-v${SCRCPY_VERSION}"
sha256sum --quiet -c - <<SUMS
$PLATFORM_TOOLS_SHA256  $TMP/pt.zip
$SCRCPY_SHA256  $TMP/scrcpy-server
SUMS

python3 -I - "$TMP" <<'PY'
import sys, zipfile
tmp = sys.argv[1]
with zipfile.ZipFile(f"{tmp}/pt.zip") as z:
    z.extract("platform-tools/adb", tmp)
    z.extract("platform-tools/NOTICE.txt", tmp)
PY

mkdir -p "$ROOT/bin"
install -m 755 "$TMP/platform-tools/adb" "$ROOT/bin/adb"
install -m 644 "$TMP/platform-tools/NOTICE.txt" "$ROOT/bin/NOTICE-platform-tools.txt"
install -m 644 "$TMP/scrcpy-server" "$ROOT/bin/scrcpy-server"
cat > "$ROOT/bin/VERSIONS" <<VERSIONS
adb: platform-tools ${PLATFORM_TOOLS_VERSION} zip-sha256=${PLATFORM_TOOLS_SHA256}
scrcpy-server: v${SCRCPY_VERSION} sha256=${SCRCPY_SHA256}
VERSIONS
cat "$ROOT/bin/VERSIONS"
