#include "fx_layer.h"
#include "theme.h"
#include "../app_state.h"

#include <pebble.h>
#include <stdlib.h>
#include <string.h>

// Frame interval; ~17 fps feels smooth for these effects without burning CPU.
#define FX_STEP_MS       60
#define FX_IDLE_STEP_MS  1000

// ---- Ripple (effect 0) -------------------------------------------------
#define RIPPLE_RING_COUNT 4
#define RIPPLE_SPACING    28
#define RIPPLE_PHASE_STEP 2
#define RIPPLE_APLITE_INNER_SKIP 40

// ---- Starfield (effect 1) ----------------------------------------------
#define STAR_COUNT 30
#define STAR_Z_MAX 255
#define STAR_Z_MIN 12
#define STAR_SPEED 3
#define STAR_SPAWN_RANGE 80     // x/y world extents when respawning
#define STAR_FOV 130            // perspective focal length (bigger = more spread)

// ---- Alert (effect 3) --------------------------------------------------
// Minimal bottom-row fire, lit only when the currently watched departure
// reports a major disruption. Grid is kept shallow so it sits under the
// progress bar without encroaching on the disruption label row. Works on
// both colour and aplite: on aplite we threshold the heat to black pixels
// so flickering shows as a crackling silhouette across the bottom strip.
#define FIRE_W 36
#define FIRE_H 14
#define FIRE_BLOCK 4
#define FIRE_COOL_MAX 4         // max decay per propagation step

// ---- Cube (effect 4) ---------------------------------------------------
#define CUBE_UNIT 40            // half edge length in world units
#define CUBE_FOV 140
#define CUBE_CAM_Z 180          // camera offset along +z
#define CUBE_YAW_STEP  700      // trig-ratio units per frame
#define CUBE_PITCH_STEP 450

// 12 cube edges as pairs of vertex indices into the 8-vertex array.
static const uint8_t CUBE_EDGES[12][2] = {
  {0,1},{1,3},{3,2},{2,0},  // back face
  {4,5},{5,7},{7,6},{6,4},  // front face
  {0,4},{1,5},{2,6},{3,7},  // connectors
};

typedef struct {
  int16_t x, y;  // world coords centred at 0
  uint8_t z;     // depth, 0 = at camera, STAR_Z_MAX = far
} Star;

typedef struct {
  uint8_t mode;
  AppTimer *timer;
  uint16_t frame;
  uint16_t max_radius;          // ripple
  uint16_t ripple_phase;        // ripple
  Star stars[STAR_COUNT];       // starfield
  uint8_t heat[FIRE_W * FIRE_H];     // alert
  int32_t cube_yaw;             // cube angle in pebble trig-ratio (0..TRIG_MAX_ANGLE)
  int32_t cube_pitch;
} FxData;

// Simple xorshift PRNG — Pebble's rand() works but pulls in libc we don't
// need. Seed is stirred with the current frame on each use.
static uint32_t fx_rand(uint32_t *s) {
  uint32_t x = *s;
  x ^= x << 13;
  x ^= x >> 17;
  x ^= x << 5;
  *s = x ? x : 0xdeadbeef;
  return x;
}

// Colour palette helpers ------------------------------------------------

#if defined(PBL_COLOR)
// Map a byte 0..255 to a fire palette: dark red -> red -> orange -> yellow
// -> white. Used by the Alert effect only; the lower buckets get skipped
// entirely rather than painted dark so the band reads as "embers" against
// the watch background instead of a black horizon line.
static GColor fire_palette(uint8_t h) {
  if (h < 60)  return GColorBulgarianRose;
  if (h < 110) return GColorDarkCandyAppleRed;
  if (h < 160) return GColorRed;
  if (h < 200) return GColorOrange;
  if (h < 230) return GColorYellow;
  return GColorWhite;
}

static GColor star_color(uint8_t z) {
  if (z < 60)  return GColorWhite;
  if (z < 160) return GColorLightGray;
  return GColorDarkGray;
}

static GColor cube_color(void) {
  // The dark indigo wires were too low-contrast on the light-theme white
  // background; cerulean reads cleanly on both themes.
  return GColorVividCerulean;
}
#endif

// True when the currently watched favourite has a "major" disruption
// (anything theme_is_major_disruption returns true for). Used by the
// Alert effect to decide whether to render flames at all — absent
// disruptions, the layer stays empty so it's a silent default.
static bool current_watch_has_major_disruption(void) {
  if (g_app_state.watching_button == 0) return false;
  Entry *e = app_state_get_entry(g_app_state.watching_button);
  if (!e || e->disruption_count == 0) return false;
  for (uint8_t i = 0; i < e->disruption_count; i++) {
    if (theme_is_major_disruption(e->disruptions[i])) return true;
  }
  return false;
}

// ---- Ripple draw ------------------------------------------------------

static void draw_dotted_ring(GContext *ctx, GPoint center, int radius, int step_deg) {
  for (int a = 0; a < 360; a += step_deg) {
    int32_t tangle = DEG_TO_TRIGANGLE(a);
    int32_t x = (sin_lookup(tangle) * radius) / TRIG_MAX_RATIO;
    int32_t y = (-cos_lookup(tangle) * radius) / TRIG_MAX_RATIO;
    graphics_draw_pixel(ctx, GPoint(center.x + x, center.y + y));
  }
}

static void draw_ripple(Layer *layer, GContext *ctx, FxData *d) {
  GRect bounds = layer_get_bounds(layer);
  GPoint center = grect_center_point(&bounds);
  graphics_context_set_stroke_color(ctx, theme_ring());

#if defined(PBL_COLOR)
  if (g_app_state.flags.dark_theme) {
    graphics_context_set_stroke_width(ctx, 1);
    for (int i = 0; i < RIPPLE_RING_COUNT; i++) {
      uint16_t r = (d->ripple_phase + i * RIPPLE_SPACING) % d->max_radius;
      if (r < 6) continue;
      graphics_draw_circle(ctx, center, r);
    }
  } else {
    for (int i = 0; i < RIPPLE_RING_COUNT; i++) {
      uint16_t r = (d->ripple_phase + i * RIPPLE_SPACING) % d->max_radius;
      if (r < 6) continue;
      int step = (i & 1) ? 14 : 10;
      draw_dotted_ring(ctx, center, r, step);
    }
  }
#else
  for (int i = 0; i < RIPPLE_RING_COUNT; i++) {
    uint16_t r = (d->ripple_phase + i * RIPPLE_SPACING) % d->max_radius;
    if (r < RIPPLE_APLITE_INNER_SKIP) continue;
    int step = (i & 1) ? 14 : 10;
    draw_dotted_ring(ctx, center, r, step);
  }
#endif
}

// ---- Starfield draw ---------------------------------------------------

static void spawn_star(Star *s, uint32_t *seed) {
  uint32_t r = fx_rand(seed);
  s->x = (int16_t)((int32_t)(r & 0xffff) % (2 * STAR_SPAWN_RANGE) - STAR_SPAWN_RANGE);
  r = fx_rand(seed);
  s->y = (int16_t)((int32_t)(r & 0xffff) % (2 * STAR_SPAWN_RANGE) - STAR_SPAWN_RANGE);
  s->z = STAR_Z_MAX;
}

static void draw_starfield(Layer *layer, GContext *ctx, FxData *d) {
  GRect bounds = layer_get_bounds(layer);
  int cx = bounds.size.w / 2;
  int cy = bounds.size.h / 2;
  uint32_t seed = 0x1234567u ^ d->frame;

  for (int i = 0; i < STAR_COUNT; i++) {
    Star *s = &d->stars[i];
    if (s->z <= STAR_Z_MIN) {
      spawn_star(s, &seed);
    } else {
      s->z -= STAR_SPEED;
    }
    int sx = cx + ((int32_t)s->x * STAR_FOV) / s->z;
    int sy = cy + ((int32_t)s->y * STAR_FOV) / s->z;
    if (sx < -2 || sx >= bounds.size.w + 2 ||
        sy < -2 || sy >= bounds.size.h + 2) {
      spawn_star(s, &seed);
      continue;
    }
#if defined(PBL_COLOR)
    graphics_context_set_fill_color(ctx, star_color(s->z));
#else
    graphics_context_set_fill_color(ctx, theme_fg());
#endif
    // Size scales with proximity so the depth reads at a glance: distant
    // stars are a single pixel, mid-field a 2x2 block, nearest a chunky
    // 3x3 with a 4-pixel cross around it. Fill_rect is cheaper than
    // multiple draw_pixel calls and reads as a brighter "star" on the
    // screen instead of a hairline dot that disappears under the LCD
    // pixel grid.
    int size;
    if      (s->z < 60)  size = 3;
    else if (s->z < 140) size = 2;
    else                 size = 1;
    graphics_fill_rect(ctx, GRect(sx, sy, size, size), 0, GCornerNone);
  }
}

// ---- Alert draw -------------------------------------------------------

static void fire_step(FxData *d, uint32_t *seed) {
  // Bottom row = fuel. Narrower flame-count than the full fire effect so
  // the strip reads as embers, not a full blaze. Per-column jitter keeps
  // the silhouette animated.
  for (int x = 0; x < FIRE_W; x++) {
    uint32_t r = fx_rand(seed);
    d->heat[(FIRE_H - 1) * FIRE_W + x] = 200 + (uint8_t)(r & 0x2f);
  }
  for (int y = 0; y < FIRE_H - 1; y++) {
    for (int x = 0; x < FIRE_W; x++) {
      uint32_t r = fx_rand(seed);
      int rand3 = (int)(r & 3) - 1;   // -1, 0, 1
      int src_x = x + rand3;
      if (src_x < 0) src_x = 0;
      if (src_x >= FIRE_W) src_x = FIRE_W - 1;
      int decay = (int)((r >> 4) & (FIRE_COOL_MAX - 1));
      int below = d->heat[(y + 1) * FIRE_W + src_x];
      int val = below - decay;
      if (val < 0) val = 0;
      d->heat[y * FIRE_W + x] = (uint8_t)val;
    }
  }
}

static void draw_alert(Layer *layer, GContext *ctx, FxData *d) {
  // Alert only flares when the current watch has a major disruption.
  // Everything else: silent. Clears the heat buffer so the flame dies
  // down smoothly if the disruption is dismissed mid-watch.
  if (!current_watch_has_major_disruption()) {
    memset(d->heat, 0, sizeof(d->heat));
    return;
  }

  GRect bounds = layer_get_bounds(layer);
  uint32_t seed = 0xabcdef01u ^ d->frame;
  fire_step(d, &seed);
  int off_x = (bounds.size.w - FIRE_W * FIRE_BLOCK) / 2;
  int off_y = bounds.size.h - FIRE_H * FIRE_BLOCK;
  if (off_y < 0) off_y = 0;

  for (int y = 0; y < FIRE_H; y++) {
    for (int x = 0; x < FIRE_W; x++) {
      uint8_t h = d->heat[y * FIRE_W + x];
      if (h < 40) continue;
#if defined(PBL_COLOR)
      graphics_context_set_fill_color(ctx, fire_palette(h));
#else
      // Aplite: binary threshold on the heat value gives a crackling
      // silhouette that reads as fire without colour.
      if (h < 120) continue;
      graphics_context_set_fill_color(ctx, theme_fg());
#endif
      graphics_fill_rect(ctx,
          GRect(off_x + x * FIRE_BLOCK, off_y + y * FIRE_BLOCK,
                FIRE_BLOCK, FIRE_BLOCK),
          0, GCornerNone);
    }
  }
}

// ---- Cube draw --------------------------------------------------------

static void rotate_y(int32_t *x, int32_t *z, int32_t cosA, int32_t sinA) {
  int32_t nx = ((*x) * cosA - (*z) * sinA) / TRIG_MAX_RATIO;
  int32_t nz = ((*x) * sinA + (*z) * cosA) / TRIG_MAX_RATIO;
  *x = nx; *z = nz;
}

static void rotate_x(int32_t *y, int32_t *z, int32_t cosA, int32_t sinA) {
  int32_t ny = ((*y) * cosA + (*z) * sinA) / TRIG_MAX_RATIO;
  int32_t nz = (-(*y) * sinA + (*z) * cosA) / TRIG_MAX_RATIO;
  *y = ny; *z = nz;
}

static void draw_cube(Layer *layer, GContext *ctx, FxData *d) {
  GRect bounds = layer_get_bounds(layer);
  int cx = bounds.size.w / 2;
  int cy = bounds.size.h / 2;

  d->cube_yaw   = (d->cube_yaw   + CUBE_YAW_STEP)   & (TRIG_MAX_ANGLE - 1);
  d->cube_pitch = (d->cube_pitch + CUBE_PITCH_STEP) & (TRIG_MAX_ANGLE - 1);
  int32_t cy_ = cos_lookup(d->cube_yaw);
  int32_t sy_ = sin_lookup(d->cube_yaw);
  int32_t cp_ = cos_lookup(d->cube_pitch);
  int32_t sp_ = sin_lookup(d->cube_pitch);

  GPoint proj[8];
  for (int i = 0; i < 8; i++) {
    int32_t x = (i & 1) ? CUBE_UNIT : -CUBE_UNIT;
    int32_t y = (i & 2) ? CUBE_UNIT : -CUBE_UNIT;
    int32_t z = (i & 4) ? CUBE_UNIT : -CUBE_UNIT;
    rotate_y(&x, &z, cy_, sy_);
    rotate_x(&y, &z, cp_, sp_);
    int32_t zp = z + CUBE_CAM_Z;
    if (zp < 20) zp = 20;
    proj[i].x = cx + (x * CUBE_FOV) / zp;
    proj[i].y = cy + (y * CUBE_FOV) / zp;
  }

#if defined(PBL_COLOR)
  graphics_context_set_stroke_color(ctx, cube_color());
  graphics_context_set_stroke_width(ctx, 2);
#else
  graphics_context_set_stroke_color(ctx, theme_fg());
  graphics_context_set_stroke_width(ctx, 1);
#endif
  for (int i = 0; i < 12; i++) {
    graphics_draw_line(ctx, proj[CUBE_EDGES[i][0]], proj[CUBE_EDGES[i][1]]);
  }
  graphics_context_set_stroke_width(ctx, 1);
}

// ---- Dispatch ---------------------------------------------------------

static void fx_update_proc(Layer *layer, GContext *ctx) {
  FxData *d = (FxData *)layer_get_data(layer);
  GRect bounds = layer_get_bounds(layer);

  graphics_context_set_fill_color(ctx, theme_bg());
  graphics_fill_rect(ctx, bounds, 0, GCornerNone);

  switch (d->mode) {
    case BG_FX_STARFIELD: draw_starfield(layer, ctx, d); break;
    case BG_FX_ALERT:     draw_alert(layer, ctx, d);     break;
    case BG_FX_CUBE:      draw_cube(layer, ctx, d);      break;
    case BG_FX_RIPPLE:
    default:              draw_ripple(layer, ctx, d);    break;
  }
}

static void fx_tick(void *context);

// Alert mode is idle most of the time — no active disruption means the
// draw is a no-op. Poll at 1 Hz in that state so we still pick up a
// newly-arrived disruption within a second, but save the per-60ms wake
// otherwise. Every other effect animates continuously and needs 60 ms.
static uint32_t tick_interval_for(FxData *d) {
  if (d->mode == BG_FX_ALERT && !current_watch_has_major_disruption()) {
    return FX_IDLE_STEP_MS;
  }
  return FX_STEP_MS;
}

static void schedule_tick(Layer *layer) {
  FxData *d = (FxData *)layer_get_data(layer);
  if (d->timer) app_timer_cancel(d->timer);
  d->timer = app_timer_register(tick_interval_for(d), fx_tick, layer);
}

static void fx_tick(void *context) {
  Layer *layer = (Layer *)context;
  FxData *d = (FxData *)layer_get_data(layer);
  d->timer = NULL;
  if (g_app_state.flags.disable_ripple_vfx) {
    // Battery-saver mode: we already painted the static bg once on
    // window_load; skip further redraws until the flag flips.
    return;
  }
  d->frame++;
  d->ripple_phase = (uint16_t)((d->ripple_phase + RIPPLE_PHASE_STEP) % d->max_radius);
  // Alert idle: draw_alert will no-op. Skipping the mark_dirty tells
  // Pebble's OS the display is static, which keeps the LCD off the bus.
  if (!(d->mode == BG_FX_ALERT && !current_watch_has_major_disruption())) {
    layer_mark_dirty(layer);
  }
  schedule_tick(layer);
}

static void seed_state(FxData *d, GRect bounds) {
  uint32_t seed = (uint32_t)time(NULL) ^ 0xc001u;
  for (int i = 0; i < STAR_COUNT; i++) {
    spawn_star(&d->stars[i], &seed);
    // Pre-stagger the depth so stars don't all spawn at the far plane.
    d->stars[i].z = STAR_Z_MIN + (uint8_t)(fx_rand(&seed) % (STAR_Z_MAX - STAR_Z_MIN));
  }
  memset(d->heat, 0, sizeof(d->heat));
  uint16_t half = (bounds.size.w > bounds.size.h ? bounds.size.w : bounds.size.h) / 2;
  d->max_radius = (uint16_t)(half * 6 / 5);
  d->ripple_phase = 0;
  d->frame = 0;
  d->cube_yaw = 0;
  d->cube_pitch = TRIG_MAX_ANGLE / 16;
}

Layer *fx_layer_create(GRect bounds) {
  if (g_app_state.flags.disable_ripple_vfx) return NULL;

  Layer *layer = layer_create_with_data(bounds, sizeof(FxData));
  FxData *d = (FxData *)layer_get_data(layer);
  memset(d, 0, sizeof(*d));
  d->mode = g_app_state.flags.bg_fx;
  // Value 2 was BG_FX_PLASMA in an earlier build. If a user's persist
  // still has it, fall through to the default rings instead of rendering
  // nothing.
  if (d->mode != BG_FX_RIPPLE && d->mode != BG_FX_STARFIELD &&
      d->mode != BG_FX_ALERT && d->mode != BG_FX_CUBE) {
    d->mode = BG_FX_RIPPLE;
  }
  d->timer = NULL;
  seed_state(d, bounds);
  layer_set_update_proc(layer, fx_update_proc);
  return layer;
}

void fx_layer_destroy(Layer *layer) {
  if (!layer) return;
  FxData *d = (FxData *)layer_get_data(layer);
  if (d->timer) { app_timer_cancel(d->timer); d->timer = NULL; }
  layer_destroy(layer);
}

void fx_layer_start(Layer *layer) {
  if (!layer) return;
  schedule_tick(layer);
}

void fx_layer_stop(Layer *layer) {
  if (!layer) return;
  FxData *d = (FxData *)layer_get_data(layer);
  if (d->timer) { app_timer_cancel(d->timer); d->timer = NULL; }
}
