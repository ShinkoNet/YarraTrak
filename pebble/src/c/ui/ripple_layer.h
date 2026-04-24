#pragma once

#include <pebble.h>

// creates a background layer that draws animated concentric rings expanding from the centre
Layer *ripple_layer_create(GRect bounds);

// Destroys the layer and any active animation timer. Safe to call with NULL.
void ripple_layer_destroy(Layer *layer);

// call when the parent window becomes visible / hidden to start / stop the ring animation timer
void ripple_layer_start(Layer *layer);
void ripple_layer_stop(Layer *layer);
