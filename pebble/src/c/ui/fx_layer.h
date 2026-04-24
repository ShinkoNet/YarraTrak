#pragma once

#include <pebble.h>

// Unified background-fx layer used behind the watch-window countdown.
// The active effect is selected by g_app_state.flags.bg_fx; the master kill
// switch is still g_app_state.flags.disable_ripple_vfx, which makes the
// factory return NULL and the start/stop helpers no-op.
//
// Effects implemented:
//   0 BG_FX_RIPPLE     — concentric dotted rings (original ripple)
//   1 BG_FX_STARFIELD  — perspective starfield
//   2 BG_FX_PLASMA     — 8x8 blocked sin-field colour cycle
//   3 BG_FX_FIRE       — bottom-up Doom-menu fire
//   4 BG_FX_CUBE       — rotating wireframe cube (Amiga-demo classic)
//
// Aplite falls back to the rings effect for anything other than 0 — the
// 1-bit display can't reasonably carry the others without dithering that
// would fight the large centred countdown.

Layer *fx_layer_create(GRect bounds);
void fx_layer_destroy(Layer *layer);
void fx_layer_start(Layer *layer);
void fx_layer_stop(Layer *layer);
