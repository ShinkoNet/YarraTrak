#pragma once

#include <pebble.h>

// old bg fx ids fall back to rings

Layer *fx_layer_create(GRect bounds);
void fx_layer_destroy(Layer *layer);
void fx_layer_start(Layer *layer);
void fx_layer_stop(Layer *layer);
