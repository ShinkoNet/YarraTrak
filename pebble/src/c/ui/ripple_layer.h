#pragma once

#include <pebble.h>

// Creates a background layer that draws animated concentric rings expanding
// from the centre. Disabled when the `disable_ripple_vfx` flag is set.
// On 1-bit (aplite) the effect is a no-op: the function returns NULL.
Layer *ripple_layer_create(GRect bounds);

// Destroys the layer and any active animation timer. Safe to call with NULL.
void ripple_layer_destroy(Layer *layer);

// Call when the parent window becomes visible / hidden to start / stop the
// ring animation timer. Safe to call with NULL.
void ripple_layer_start(Layer *layer);
void ripple_layer_stop(Layer *layer);
