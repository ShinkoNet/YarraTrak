#!/bin/bash
set -e

cd pebble

echo "=== Building Pebble App ==="
pebble build

echo "=== Installing to Emulator ==="
pebble install --emulator aplite

echo "=== Configuring App ==="
pebble emu-app-config --emulator aplite
