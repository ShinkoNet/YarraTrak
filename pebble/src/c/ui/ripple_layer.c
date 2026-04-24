#include "ripple_layer.h"
#include "theme.h"
#include "../app_state.h"

#include <pebble.h>

#define RIPPLE_RING_COUNT   4
#define RIPPLE_SPACING      28
#define RIPPLE_STEP_MS      60
#define RIPPLE_PHASE_STEP   2

// On aplite we don't draw rings behind the large countdown text — the 1-bit
// dotted pattern can otherwise compete with the white digits in the centre.
#define RIPPLE_APLITE_INNER_SKIP  40

typedef struct {
  uint16_t phase;
  uint16_t max_radius;
  AppTimer *timer;
} RippleData;

// Stippled "grey" ring: plot single pixels around the circumference instead
// of a continuous stroke. At 12° per dot (30 dots per ring) the effect reads
// as a sparse dithered circle on the 1-bit aplite display while still giving
// a sense of motion.
static void draw_dotted_ring(GContext *ctx, GPoint center, int radius, int step_deg) {
  for (int a = 0; a < 360; a += step_deg) {
    int32_t tangle = DEG_TO_TRIGANGLE(a);
    int32_t x = (sin_lookup(tangle) * radius) / TRIG_MAX_RATIO;
    int32_t y = (-cos_lookup(tangle) * radius) / TRIG_MAX_RATIO;
    graphics_draw_pixel(ctx, GPoint(center.x + x, center.y + y));
  }
}

static void ripple_update_proc(Layer *layer, GContext *ctx) {
  RippleData *d = (RippleData *)layer_get_data(layer);
  GRect bounds = layer_get_bounds(layer);
  GPoint center = grect_center_point(&bounds);

  graphics_context_set_fill_color(ctx, theme_bg());
  graphics_fill_rect(ctx, bounds, 0, GCornerNone);

  graphics_context_set_stroke_color(ctx, theme_ring());

#if defined(PBL_COLOR)
  // Dark theme: solid indigo rings on black read well. Light theme: a solid
  // stroke against white is too loud behind the black countdown, so use the
  // same stippled-pixel style as aplite for a softer dotted texture.
  if (g_app_state.flags.dark_theme) {
    graphics_context_set_stroke_width(ctx, 1);
    for (int i = 0; i < RIPPLE_RING_COUNT; i++) {
      uint16_t r = (d->phase + i * RIPPLE_SPACING) % d->max_radius;
      if (r < 6) continue;
      graphics_draw_circle(ctx, center, r);
    }
  } else {
    for (int i = 0; i < RIPPLE_RING_COUNT; i++) {
      uint16_t r = (d->phase + i * RIPPLE_SPACING) % d->max_radius;
      if (r < 6) continue;
      int step = (i & 1) ? 14 : 10;
      draw_dotted_ring(ctx, center, r, step);
    }
  }
#else
  for (int i = 0; i < RIPPLE_RING_COUNT; i++) {
    uint16_t r = (d->phase + i * RIPPLE_SPACING) % d->max_radius;
    if (r < RIPPLE_APLITE_INNER_SKIP) continue;
    // Alternate stride per ring so adjacent rings don't line up their dots.
    int step = (i & 1) ? 14 : 10;
    draw_dotted_ring(ctx, center, r, step);
  }
#endif
}

static void ripple_tick(void *context);

static void schedule_tick(Layer *layer) {
  RippleData *d = (RippleData *)layer_get_data(layer);
  if (d->timer) app_timer_cancel(d->timer);
  d->timer = app_timer_register(RIPPLE_STEP_MS, ripple_tick, layer);
}

static void ripple_tick(void *context) {
  Layer *layer = (Layer *)context;
  RippleData *d = (RippleData *)layer_get_data(layer);
  d->timer = NULL;
  if (g_app_state.flags.disable_ripple_vfx) {
    layer_mark_dirty(layer);
    return;
  }
  d->phase = (uint16_t)((d->phase + RIPPLE_PHASE_STEP) % d->max_radius);
  layer_mark_dirty(layer);
  schedule_tick(layer);
}

Layer *ripple_layer_create(GRect bounds) {
  if (g_app_state.flags.disable_ripple_vfx) return NULL;

  Layer *layer = layer_create_with_data(bounds, sizeof(RippleData));
  RippleData *d = (RippleData *)layer_get_data(layer);
  d->phase = 0;
  uint16_t half = (bounds.size.w > bounds.size.h ? bounds.size.w : bounds.size.h) / 2;
  d->max_radius = (uint16_t)(half * 6 / 5);
  d->timer = NULL;
  layer_set_update_proc(layer, ripple_update_proc);
  return layer;
}

void ripple_layer_destroy(Layer *layer) {
  if (!layer) return;
  RippleData *d = (RippleData *)layer_get_data(layer);
  if (d->timer) { app_timer_cancel(d->timer); d->timer = NULL; }
  layer_destroy(layer);
}

void ripple_layer_start(Layer *layer) {
  if (!layer) return;
  schedule_tick(layer);
}

void ripple_layer_stop(Layer *layer) {
  if (!layer) return;
  RippleData *d = (RippleData *)layer_get_data(layer);
  if (d->timer) { app_timer_cancel(d->timer); d->timer = NULL; }
}
