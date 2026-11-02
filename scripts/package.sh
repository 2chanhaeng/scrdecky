#!/usr/bin/env bash
# Builds the frontend and zips the plugin in the layout Decky expects
# (a single top-level folder named after the plugin).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME=scrdecky
OUT="$ROOT/out"
cd "$ROOT"
if [ ! -x bin/adb ] || [ ! -f bin/scrcpy-server ]; then
  echo "bin/adb or bin/scrcpy-server is missing; run scripts/fetch-bins.sh first" >&2
  exit 1
fi
npm run build
rm -rf "$OUT" && mkdir -p "$OUT/$NAME"
cp -r dist bin py_modules main.py plugin.json package.json README.md LICENSE THIRD_PARTY_NOTICES.md "$OUT/$NAME/"
rm -rf "$OUT/$NAME/py_modules/__pycache__" "$OUT/$NAME/dist/index.js.map"
(cd "$OUT" && rm -f "$NAME.zip" && zip -qr "$NAME.zip" "$NAME")
echo "built $OUT/$NAME.zip"
