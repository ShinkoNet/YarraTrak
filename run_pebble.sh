#!/bin/bash
set -e

TARGET="${1:-emu}"
PHONE_IP="${PHONE_IP:-10.1.0.54}"
EMU_PLATFORM="${EMU_PLATFORM:-aplite}"

cd pebble

if [ "$TARGET" = "emu" ] && ! qemu-pebble --version >/dev/null 2>&1; then
  fallback_qemu="$HOME/.pebble-sdk/SDKs/4.9.77/toolchain/bin/qemu-pebble"
  if [ -x "$fallback_qemu" ] && "$fallback_qemu" --version >/dev/null 2>&1; then
    export PEBBLE_QEMU_PATH="$fallback_qemu"
    echo "=== Using fallback emulator: $PEBBLE_QEMU_PATH ==="
  fi
fi

echo "=== Bundling PKJS sources ==="
python3 tools/build_pkjs_bundle.py src/pkjs_src appinfo.json src/pkjs/pebble-js-app.js

echo "=== Building Pebble App ==="
pebble build

case "$TARGET" in
  phone)
    echo "=== Installing to Phone ($PHONE_IP) ==="
    pebble install --phone="$PHONE_IP" --logs
    ;;
  emu|emulator)
    echo "=== Installing to Emulator ($EMU_PLATFORM) ==="
    pebble install --emulator "$EMU_PLATFORM" --logs
    ;;
  *)
    echo "Usage: $0 [emu|phone]  (default: emu)" >&2
    echo "  PHONE_IP=... to override phone IP (default: $PHONE_IP)" >&2
    echo "  EMU_PLATFORM=... to override emulator (default: $EMU_PLATFORM)" >&2
    exit 1
    ;;
esac
