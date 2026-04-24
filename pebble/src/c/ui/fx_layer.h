#pragma once

#include <pebble.h>

// unified background-fx layer used behind the watch-window countdown

Layer *fx_layer_create(GRect bounds);
void fx_layer_destroy(Layer *layer);
void fx_layer_start(Layer *layer);
void fx_layer_stop(Layer *layer);
