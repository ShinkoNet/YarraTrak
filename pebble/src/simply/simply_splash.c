#include "simply_splash.h"

#include "simply.h"

#include "util/graphics.h"
#include "util/platform.h"

#include <pebble.h>

static GColor prv_splash_background_color(void) {
#if defined(PBL_COLOR)
  return GColorFromRGB(41, 19, 129);
#else
  return GColorBlack;
#endif
}

void layer_update_callback(Layer *layer, GContext *ctx) {
  GRect frame = layer_get_frame(layer);
  GRect logo_frame = frame;
  logo_frame.origin.y -= 15;

  graphics_context_set_fill_color(ctx, prv_splash_background_color());
  graphics_fill_rect(ctx, frame, 0, GCornerNone);

#if defined(PBL_PLATFORM_APLITE) || defined(PBL_PLATFORM_FLINT)
  graphics_context_set_text_color(ctx, GColorWhite);
  graphics_draw_text(ctx, "PTV Notify",
                     fonts_get_system_font(FONT_KEY_GOTHIC_24_BOLD),
                     GRect(0, 58, frame.size.w, 32),
                     GTextOverflowModeTrailingEllipsis,
                     GTextAlignmentCenter,
                     NULL);
#else
  SimplySplash *self = (SimplySplash*) window_get_user_data((Window*) layer);
  graphics_draw_bitmap_centered(ctx, self->image, logo_frame);
#endif
}


static void window_load(Window *window) {
#if !defined(PBL_PLATFORM_APLITE) && !defined(PBL_PLATFORM_FLINT)
  SimplySplash *self = window_get_user_data(window);
  self->image = gbitmap_create_with_resource(RESOURCE_ID_IMAGE_LOGO_SPLASH);
#endif
}

static void window_disappear(Window *window) {
  SimplySplash *self = window_get_user_data(window);
  bool animated = false;
  window_stack_remove(self->window, animated);
  simply_splash_destroy(self);
}

SimplySplash *simply_splash_create(Simply *simply) {
  SimplySplash *self = malloc(sizeof(*self));
  *self = (SimplySplash) { .simply = simply };

  self->window = window_create();
  window_set_user_data(self->window, self);
  window_set_fullscreen(self->window, true);
  window_set_background_color(self->window, prv_splash_background_color());
  window_set_window_handlers(self->window, (WindowHandlers) {
    .load = window_load,
    .disappear = window_disappear,
  });

  layer_set_update_proc(window_get_root_layer(self->window), layer_update_callback);

  return self;
}

void simply_splash_destroy(SimplySplash *self) {
  gbitmap_destroy(self->image);

  window_destroy(self->window);

  self->simply->splash = NULL;

  free(self);
}
