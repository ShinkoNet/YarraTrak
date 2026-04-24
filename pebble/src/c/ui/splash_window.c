#include "splash_window.h"

#include <pebble.h>

static Window *s_window = NULL;
static TextLayer *s_text_layer = NULL;
static TextLayer *s_sub_text_layer = NULL;
static BitmapLayer *s_logo_layer = NULL;
static GBitmap *s_logo_bitmap = NULL;

static void window_load(Window *window) {
  Layer *root = window_get_root_layer(window);
  GRect bounds = layer_get_bounds(root);
  window_set_background_color(window, GColorBlack);

#if defined(PBL_BW)
  // Aplite has no logo resource. Use text only.
  s_text_layer = text_layer_create(GRect(0, bounds.size.h / 2 - 30, bounds.size.w, 40));
  text_layer_set_text(s_text_layer, "YarraTrak");
  text_layer_set_font(s_text_layer, fonts_get_system_font(FONT_KEY_GOTHIC_28_BOLD));
  text_layer_set_text_color(s_text_layer, GColorWhite);
  text_layer_set_background_color(s_text_layer, GColorClear);
  text_layer_set_text_alignment(s_text_layer, GTextAlignmentCenter);
  layer_add_child(root, text_layer_get_layer(s_text_layer));
#else
  s_logo_bitmap = gbitmap_create_with_resource(RESOURCE_ID_IMAGE_LOGO_SPLASH);
  if (s_logo_bitmap) {
    GSize sz = gbitmap_get_bounds(s_logo_bitmap).size;
    s_logo_layer = bitmap_layer_create(GRect((bounds.size.w - sz.w) / 2,
                                             (bounds.size.h - sz.h) / 2 - 10,
                                             sz.w, sz.h));
    bitmap_layer_set_bitmap(s_logo_layer, s_logo_bitmap);
    bitmap_layer_set_compositing_mode(s_logo_layer, GCompOpSet);
    layer_add_child(root, bitmap_layer_get_layer(s_logo_layer));
  }
#endif

  s_sub_text_layer = text_layer_create(GRect(0, bounds.size.h - 40, bounds.size.w, 24));
  text_layer_set_text(s_sub_text_layer, "Connecting...");
  text_layer_set_font(s_sub_text_layer, fonts_get_system_font(FONT_KEY_GOTHIC_14));
  text_layer_set_text_color(s_sub_text_layer, GColorWhite);
  text_layer_set_background_color(s_sub_text_layer, GColorClear);
  text_layer_set_text_alignment(s_sub_text_layer, GTextAlignmentCenter);
  layer_add_child(root, text_layer_get_layer(s_sub_text_layer));
}

static void window_unload(Window *window) {
  if (s_text_layer) { text_layer_destroy(s_text_layer); s_text_layer = NULL; }
  if (s_sub_text_layer) { text_layer_destroy(s_sub_text_layer); s_sub_text_layer = NULL; }
  if (s_logo_layer) { bitmap_layer_destroy(s_logo_layer); s_logo_layer = NULL; }
  if (s_logo_bitmap) { gbitmap_destroy(s_logo_bitmap); s_logo_bitmap = NULL; }
  window_destroy(s_window);
  s_window = NULL;
}

void splash_window_push(void) {
  if (s_window) return;
  s_window = window_create();
  window_set_window_handlers(s_window, (WindowHandlers){
    .load = window_load,
    .unload = window_unload,
  });
  window_stack_push(s_window, true);
}

void splash_window_pop(void) {
  if (s_window) {
    window_stack_remove(s_window, true);
  }
}
