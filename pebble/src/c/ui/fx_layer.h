#pragma once

#include <pebble.h>

// Unified background-fx layer used behind the watch-window countdown.
// The active effect is selected by g_app_state.flags.bg_fx; the master kill
// switch is g_app_state.flags.disable_animations, which makes the factory
// return NULL and the start/stop helpers no-op. The same flag also kills
// the per-tick countdown bump and delay shake in watch_window.c.
//
// Effects implemented:
//   0 BG_FX_RIPPLE     — concentric dotted rings (default)
//   1 BG_FX_STARFIELD  — perspective starfield
//   4 BG_FX_CUBE       — rotating wireframe cube
//
// Values 2 and 3 are reserved — used to be plasma and a disruption-
// triggered fire effect respectively, both removed for being too noisy.
// All remaining effects render correctly on both colour and aplite.

Layer *fx_layer_create(GRect bounds);
void fx_layer_destroy(Layer *layer);
void fx_layer_start(Layer *layer);
void fx_layer_stop(Layer *layer);
