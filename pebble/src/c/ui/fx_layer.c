#include "fx_layer.h"
#include "theme.h"
#include "../app_state.h"

#include <pebble.h>
#include <stdlib.h>
#include <string.h>

// Frame interval; ~17 fps feels smooth for these effects without burning CPU.
#define FX_STEP_MS 60

// ---- Ripple (effect 0) -------------------------------------------------
#define RIPPLE_RING_COUNT 4
#define RIPPLE_SPACING    28
#define RIPPLE_PHASE_STEP 2
#define RIPPLE_APLITE_INNER_SKIP 40

// ---- Starfield (effect 1) ----------------------------------------------
#define STAR_COUNT 34
#define STAR_Z_MAX 255
#define STAR_Z_MIN 8
#define STAR_SPEED 3
#define STAR_SPAWN_RANGE 80     // x/y world extents when respawning
#define STAR_FOV 110            // perspective focal length

// ---- Plasma (effect 2) -------------------------------------------------
#define PLASMA_BLOCK 8          // pixel size of each plasma cell
// Small 64-entry sin LUT spanning a full period at fixed-point 127.
// Computed once on layer create.
#define PLASMA_SIN_STEPS 64

// ---- Fire (effect 3) ---------------------------------------------------
// Low-res heat grid, blitted as 4x4 blocks. 36x42 = 1512 bytes + gives a
// chunky "retro" look that matches a demoscene vibe better than smooth fire.
#define FIRE_W 36
#define FIRE_H 42
#define FIRE_BLOCK 4

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
  int8_t sin_lut[PLASMA_SIN_STEPS];  // plasma
  uint8_t heat[FIRE_W * FIRE_H];     // fire
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
// Map a byte 0..255 to a fire palette: black -> dark red -> red -> orange
// -> yellow -> white. Only uses named constants in Pebble's standard
// 64-colour set.
static GColor fire_palette(uint8_t h) {
  if (h < 40)  return GColorBlack;
  if (h < 80)  return GColorBulgarianRose;
  if (h < 120) return GColorDarkCandyAppleRed;
  if (h < 160) return GColorRed;
  if (h < 200) return GColorOrange;
  if (h < 230) return GColorYellow;
  return GColorWhite;
}

// Map a byte 0..255 to a cyclic plasma palette running through the cool
// and warm halves of the 64-colour palette.
static GColor plasma_palette(uint8_t h) {
  switch (h >> 5) {  // 0..7 buckets
    case 0:  return GColorIndigo;
    case 1:  return GColorImperialPurple;
    case 2:  return GColorBlueMoon;
    case 3:  return GColorVividCerulean;
    case 4:  return GColorCyan;
    case 5:  return GColorGreen;
    case 6:  return GColorYellow;
    default: return GColorOrange;
  }
}

static GColor star_color(uint8_t z) {
  if (z < 40)  return GColorWhite;
  if (z < 120) return GColorLightGray;
  return GColorDarkGray;
}

static GColor cube_color(void) {
  return g_app_state.flags.dark_theme ? GColorVividCerulean : GColorIndigo;
}
#endif

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
    if (sx < 0 || sx >= bounds.size.w || sy < 0 || sy >= bounds.size.h) {
      spawn_star(s, &seed);
      continue;
    }
#if defined(PBL_COLOR)
    graphics_context_set_stroke_color(ctx, star_color(s->z));
#else
    graphics_context_set_stroke_color(ctx, theme_fg());
#endif
    graphics_draw_pixel(ctx, GPoint(sx, sy));
    // Nearest stars get a second pixel to look bigger.
    if (s->z < 40) {
      graphics_draw_pixel(ctx, GPoint(sx + 1, sy));
    }
  }
}

// ---- Plasma draw ------------------------------------------------------

static void init_sin_lut(FxData *d) {
  // Build a small sin lookup scaled to +/-127. Cheap fixed-point generator
  // via the Pebble trig helpers, done once at layer create.
  for (int i = 0; i < PLASMA_SIN_STEPS; i++) {
    int32_t ang = (TRIG_MAX_ANGLE * i) / PLASMA_SIN_STEPS;
    d->sin_lut[i] = (int8_t)((sin_lookup(ang) * 127) / TRIG_MAX_RATIO);
  }
}

static void draw_plasma(Layer *layer, GContext *ctx, FxData *d) {
  GRect bounds = layer_get_bounds(layer);
  uint16_t t = d->frame;
  int nx = (bounds.size.w + PLASMA_BLOCK - 1) / PLASMA_BLOCK;
  int ny = (bounds.size.h + PLASMA_BLOCK - 1) / PLASMA_BLOCK;

  for (int by = 0; by < ny; by++) {
    for (int bx = 0; bx < nx; bx++) {
      // Four summed sin waves, two of which depend on time — classic plasma.
      int8_t s1 = d->sin_lut[(bx * 3 + t) & (PLASMA_SIN_STEPS - 1)];
      int8_t s2 = d->sin_lut[(by * 5 + (t >> 1)) & (PLASMA_SIN_STEPS - 1)];
      int8_t s3 = d->sin_lut[((bx + by) * 2 + t) & (PLASMA_SIN_STEPS - 1)];
      int8_t s4 = d->sin_lut[((bx * bx + by * by) >> 2) & (PLASMA_SIN_STEPS - 1)];
      int sum = (int)s1 + (int)s2 + (int)s3 + (int)s4;  // -508..+508
      uint8_t h = (uint8_t)((sum + 512) >> 2);          // 0..255

#if defined(PBL_COLOR)
      graphics_context_set_fill_color(ctx, plasma_palette(h));
#else
      // Aplite: rings fallback is used, this path only runs on colour.
      graphics_context_set_fill_color(ctx, (h & 0x80) ? theme_fg() : theme_bg());
#endif
      GRect cell = GRect(bx * PLASMA_BLOCK, by * PLASMA_BLOCK,
                         PLASMA_BLOCK, PLASMA_BLOCK);
      graphics_fill_rect(ctx, cell, 0, GCornerNone);
    }
  }
}

// ---- Fire draw --------------------------------------------------------

static void fire_step(FxData *d, uint32_t *seed) {
  // Bottom row = fuel. Light it brightly, with a bit of per-column jitter so
  // flames aren't uniform.
  for (int x = 0; x < FIRE_W; x++) {
    uint32_t r = fx_rand(seed);
    d->heat[(FIRE_H - 1) * FIRE_W + x] = 230 + (uint8_t)(r & 0x1f);
  }
  // Propagate upward with slight horizontal wander and a cooling factor.
  for (int y = 0; y < FIRE_H - 1; y++) {
    for (int x = 0; x < FIRE_W; x++) {
      uint32_t r = fx_rand(seed);
      int rand3 = (int)(r & 3) - 1;   // -1, 0, 1 (biased slightly low)
      int src_x = x + rand3;
      if (src_x < 0) src_x = 0;
      if (src_x >= FIRE_W) src_x = FIRE_W - 1;
      int decay = (int)((r >> 4) & 3);
      int below = d->heat[(y + 1) * FIRE_W + src_x];
      int val = below - decay;
      if (val < 0) val = 0;
      d->heat[y * FIRE_W + x] = (uint8_t)val;
    }
  }
}

static void draw_fire(Layer *layer, GContext *ctx, FxData *d) {
  GRect bounds = layer_get_bounds(layer);
  uint32_t seed = 0xabcdef01u ^ d->frame;
  fire_step(d, &seed);
  int off_x = (bounds.size.w - FIRE_W * FIRE_BLOCK) / 2;
  int off_y = bounds.size.h - FIRE_H * FIRE_BLOCK;
  if (off_y < 0) off_y = 0;

  for (int y = 0; y < FIRE_H; y++) {
    for (int x = 0; x < FIRE_W; x++) {
      uint8_t h = d->heat[y * FIRE_W + x];
      if (h < 20) continue;  // transparent / very dark cells skip the blit
#if defined(PBL_COLOR)
      graphics_context_set_fill_color(ctx, fire_palette(h));
#else
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
#if defined(PBL_COLOR)
    case BG_FX_PLASMA:    draw_plasma(layer, ctx, d);    break;
    case BG_FX_FIRE:      draw_fire(layer, ctx, d);      break;
#endif
    case BG_FX_CUBE:      draw_cube(layer, ctx, d);      break;
    case BG_FX_RIPPLE:
    default:              draw_ripple(layer, ctx, d);    break;
  }
}

static void fx_tick(void *context);

static void schedule_tick(Layer *layer) {
  FxData *d = (FxData *)layer_get_data(layer);
  if (d->timer) app_timer_cancel(d->timer);
  d->timer = app_timer_register(FX_STEP_MS, fx_tick, layer);
}

static void fx_tick(void *context) {
  Layer *layer = (Layer *)context;
  FxData *d = (FxData *)layer_get_data(layer);
  d->timer = NULL;
  if (g_app_state.flags.disable_ripple_vfx) {
    layer_mark_dirty(layer);
    return;
  }
  d->frame++;
  d->ripple_phase = (uint16_t)((d->ripple_phase + RIPPLE_PHASE_STEP) % d->max_radius);
  layer_mark_dirty(layer);
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
  init_sin_lut(d);
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
#if !defined(PBL_COLOR)
  // Plasma / fire want colour; fall back to rings on aplite.
  if (d->mode == BG_FX_PLASMA || d->mode == BG_FX_FIRE) {
    d->mode = BG_FX_RIPPLE;
  }
#endif
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
