#pragma once

#include <pebble.h>

// Unified background-fx layer used behind the watch-window countdown.
// The active effect is selected by g_app_state.flags.bg_fx; the master kill
// switch is still g_app_state.flags.disable_ripple_vfx, which makes the
// factory return NULL and the start/stop helpers no-op.
//
// Effects implemented:
//   0 BG_FX_RIPPLE     — concentric dotted rings (default)
//   1 BG_FX_STARFIELD  — perspective starfield
//   3 BG_FX_ALERT      — minimal bottom-row fire, only flares on major
//                        disruptions on the current watched service
//   4 BG_FX_CUBE       — rotating wireframe cube
//
// Value 2 is reserved — used to be a full-frame plasma effect, removed.
// All remaining effects render correctly on both colour and aplite.

Layer *fx_layer_create(GRect bounds);
void fx_layer_destroy(Layer *layer);
void fx_layer_start(Layer *layer);
void fx_layer_stop(Layer *layer);
