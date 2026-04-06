#!/bin/bash
set -e

cd pebble

if ! qemu-pebble --version >/dev/null 2>&1; then
  fallback_qemu="$HOME/.pebble-sdk/SDKs/4.9.77/toolchain/bin/qemu-pebble"
  if [ -x "$fallback_qemu" ] && "$fallback_qemu" --version >/dev/null 2>&1; then
    export PEBBLE_QEMU_PATH="$fallback_qemu"
    echo "=== Using fallback emulator: $PEBBLE_QEMU_PATH ==="
  fi
fi

echo "=== Building Pebble App ==="
pebble build

echo "=== Installing to Emulator ==="
pebble install --emulator aplite

echo "=== Configuring App ==="
pebble emu-app-config --emulator aplite
