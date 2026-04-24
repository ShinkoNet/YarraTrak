#include "watch_window.h"
#include "ripple_layer.h"
#include "theme.h"
#include "../app_state.h"
#include "../protocol.h"
#include "../departures.h"
#include "../formatting.h"
#include "../haptics.h"

#include <pebble.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>

static Window *s_window = NULL;
static Layer     *s_ripple_layer = NULL;
static TextLayer *s_status_layer = NULL;
static TextLayer *s_countdown_layer = NULL;
static TextLayer *s_platform_layer = NULL;
static TextLayer *s_route_layer = NULL;
static TextLayer *s_bottom_layer = NULL;
static Layer     *s_progress_layer = NULL;
static AppTimer  *s_tick_timer = NULL;

static char s_status_buf[24];
static char s_countdown_buf[16];
static char s_countdown_prev[16];
static char s_platform_buf[24];
static char s_route_buf[48];
static char s_bottom_buf[48];

static int32_t s_last_vibrated_minutes = -1;
static char s_last_run_ref[RUN_REF_LEN] = "";
static GRect s_countdown_home_frame;
static Animation *s_shake_anim = NULL;

static void schedule_tick(void);

static Departure *get_watched_departure(void) {
  Entry *e = app_state_get_entry(g_app_state.watching_button);
  if (!e) return NULL;
  return departures_get(e, g_app_state.watching_offset);
}

static bool has_alternate_service(void) {
  Entry *e = app_state_get_entry(g_app_state.watching_button);
  if (!e) return false;
  Departure *d = departures_get(e, 1);
  return d && d->has_data;
}

static void send_watch_start_if_needed(Departure *dep) {
  Entry *e = app_state_get_entry(g_app_state.watching_button);
  if (!e || !dep || !dep->has_data || !dep->run_ref[0]) {
    return;
  }
  if (strcmp(s_last_run_ref, dep->run_ref) == 0) {
    return;
  }
  strncpy(s_last_run_ref, dep->run_ref, sizeof(s_last_run_ref) - 1);
  s_last_run_ref[sizeof(s_last_run_ref) - 1] = '\0';
  g_app_state.watched_distance_km_x100 = INT32_MIN;
  g_app_state.watched_vehicle_desc[0] = '\0';
  protocol_send_watch_start(g_app_state.watching_button,
                            dep->run_ref,
                            e->stop_id,
                            dep->route_type,
                            dep->route_id,
                            dep->direction_id);
}

// LECO renders only digits, colon and spaces. Anything else (e.g. "NOW!",
// "-- min") has to fall back to a regular proportional font.
static bool is_numeric_countdown(const char *s) {
  for (const char *p = s; *p; p++) {
    if ((*p >= '0' && *p <= '9') || *p == ':' || *p == ' ') continue;
    return false;
  }
  return *s != '\0';
}

static void progress_update_proc(Layer *layer, GContext *ctx) {
  GRect bounds = layer_get_bounds(layer);

  Departure *dep = get_watched_departure();
  if (!dep || !dep->has_data) {
    graphics_context_set_fill_color(ctx, PBL_IF_COLOR_ELSE(GColorDarkGray, theme_fg()));
    graphics_fill_rect(ctx, bounds, 0, GCornerNone);
    return;
  }

  int32_t sec = departure_seconds_until(dep);
  if (sec < 0) sec = 0;
  int32_t within_minute = sec % 60;
  int32_t fill_width = (within_minute * bounds.size.w) / 60;

  // Unfilled portion of the track = background colour (disappears into the
  // window). Filled portion = theme accent so the bar always stands out.
  graphics_context_set_fill_color(ctx, theme_bg());
  graphics_fill_rect(ctx, bounds, 0, GCornerNone);

  graphics_context_set_fill_color(ctx, theme_accent());
  graphics_fill_rect(ctx, GRect(0, 0, fill_width, bounds.size.h), 0, GCornerNone);
}

// ---- Shake/bounce animation on countdown changes ------------------------

static void shake_anim_stopped(Animation *anim, bool finished, void *context) {
  s_shake_anim = NULL;
}

static void trigger_shake(void) {
  if (g_app_state.flags.disable_timer_shake) return;
  if (!s_countdown_layer) return;
  if (s_shake_anim) {
    layer_set_frame(text_layer_get_layer(s_countdown_layer), s_countdown_home_frame);
    animation_unschedule(s_shake_anim);
    s_shake_anim = NULL;
  }

  // Subtle ±1px horizontal wiggle. Each leg is roughly two frames (~66ms)
  // so the whole effect is barely a blip — intentionally gentle since it
  // fires every second the countdown changes.
  GRect home = s_countdown_home_frame;
  GRect left = home;  left.origin.x  -= 1;
  GRect right = home; right.origin.x += 1;

  PropertyAnimation *a = property_animation_create_layer_frame(
      text_layer_get_layer(s_countdown_layer), &home, &left);
  PropertyAnimation *b = property_animation_create_layer_frame(
      text_layer_get_layer(s_countdown_layer), &left, &right);
  PropertyAnimation *c = property_animation_create_layer_frame(
      text_layer_get_layer(s_countdown_layer), &right, &home);

  animation_set_duration((Animation *)a, 66);
  animation_set_duration((Animation *)b, 66);
  animation_set_duration((Animation *)c, 66);
  animation_set_curve((Animation *)a, AnimationCurveLinear);
  animation_set_curve((Animation *)b, AnimationCurveLinear);
  animation_set_curve((Animation *)c, AnimationCurveLinear);

  Animation *seq = animation_sequence_create(
      (Animation *)a, (Animation *)b, (Animation *)c, NULL);
  animation_set_handlers(seq, (AnimationHandlers){
    .stopped = shake_anim_stopped,
  }, NULL);
  s_shake_anim = seq;
  animation_schedule(seq);
}

static void render(void) {
  Entry *e = app_state_get_entry(g_app_state.watching_button);
  if (!e) {
    return;
  }

  // Auto-correct: if we're on "service after" but there's no valid alternate,
  // snap back to "next service" silently.
  if (g_app_state.watching_offset == 1 && !has_alternate_service()) {
    g_app_state.watching_offset = 0;
  }

  // Status text
  if (g_app_state.conn_state != CONN_CONNECTED) {
    strncpy(s_status_buf, "Reconnecting...", sizeof(s_status_buf) - 1);
  } else if (g_app_state.watching_offset == 0) {
    strncpy(s_status_buf, "Next Service", sizeof(s_status_buf) - 1);
  } else {
    strncpy(s_status_buf, "Service After", sizeof(s_status_buf) - 1);
  }
  s_status_buf[sizeof(s_status_buf) - 1] = '\0';
  text_layer_set_text(s_status_layer, s_status_buf);

  // Route text
  fmt_watch_route(e, s_route_buf, sizeof(s_route_buf));
  text_layer_set_text(s_route_layer, s_route_buf);

  Departure *dep = get_watched_departure();

  // Countdown — swap to a square numeric font when the string is all digits.
  int32_t sec = dep ? departure_seconds_until(dep) : INT32_MAX;
  fmt_countdown(sec, dep, s_countdown_buf, sizeof(s_countdown_buf));
  text_layer_set_font(s_countdown_layer, fonts_get_system_font(
      is_numeric_countdown(s_countdown_buf) ? FONT_KEY_LECO_42_NUMBERS
                                            : FONT_KEY_BITHAM_42_BOLD));
  text_layer_set_text(s_countdown_layer, s_countdown_buf);

  bool changed = strcmp(s_countdown_prev, s_countdown_buf) != 0;
  if (changed) {
    strncpy(s_countdown_prev, s_countdown_buf, sizeof(s_countdown_prev) - 1);
    s_countdown_prev[sizeof(s_countdown_prev) - 1] = '\0';
    trigger_shake();
  }

  // Platform
  if (dep && dep->has_data && dep->platform[0]) {
    snprintf(s_platform_buf, sizeof(s_platform_buf), "Platform %s", dep->platform);
  } else {
    s_platform_buf[0] = '\0';
  }
  text_layer_set_text(s_platform_layer, s_platform_buf);

  // Bottom: disruption, live distance (metro only, non-zero), or vehicle desc.
  s_bottom_buf[0] = '\0';
  const char *bottom_disruption = NULL;
  bool have_position = (g_app_state.watched_distance_km_x100 != INT32_MIN &&
                        g_app_state.watched_distance_km_x100 > 0 &&
                        dep && strcmp(g_app_state.watched_run_ref, dep->run_ref) == 0);

  if (e->disruption_count > 0) {
    uint32_t which = (uint32_t)(time(NULL) / 3) % e->disruption_count;
    bottom_disruption = e->disruptions[which];
    strncpy(s_bottom_buf, bottom_disruption, sizeof(s_bottom_buf) - 1);
  } else if (have_position && dep->route_type == 0 /* metro train */) {
    int32_t whole = g_app_state.watched_distance_km_x100 / 100;
    int32_t frac = g_app_state.watched_distance_km_x100 % 100;
    if (frac < 0) frac = -frac;
    snprintf(s_bottom_buf, sizeof(s_bottom_buf), "%ld.%02ld km away", (long)whole, (long)frac);
  } else if (g_app_state.watched_vehicle_desc[0] &&
             dep && strcmp(g_app_state.watched_run_ref, dep->run_ref) == 0) {
    strncpy(s_bottom_buf, g_app_state.watched_vehicle_desc, sizeof(s_bottom_buf) - 1);
  }
  s_bottom_buf[sizeof(s_bottom_buf) - 1] = '\0';
  text_layer_set_text_color(s_bottom_layer,
      bottom_disruption ? theme_disruption(bottom_disruption) : theme_fg());
  text_layer_set_text(s_bottom_layer, s_bottom_buf);

  if (s_progress_layer) layer_mark_dirty(s_progress_layer);

  // Auto-advance if current departure has fully passed. Slide the cache
  // down by one so the former service-after becomes the new current and
  // dep[2] (if any) becomes the new service-after.
  if ((!dep || sec < -60) && g_app_state.watching_offset == 0) {
    Entry *e2 = app_state_get_entry(g_app_state.watching_button);
    if (e2) {
      Departure *next = departures_get(e2, 1);
      if (next && next->has_data) {
        for (uint8_t i = 0; i + 1 < MAX_DEPS_PER_ENTRY; i++) {
          memcpy(&e2->departures[i], &e2->departures[i + 1], sizeof(Departure));
        }
        memset(&e2->departures[MAX_DEPS_PER_ENTRY - 1], 0, sizeof(Departure));
        s_last_run_ref[0] = '\0';
      }
    }
  }
}

static void maybe_vibrate(Departure *dep) {
  if (!dep || !dep->has_data) return;

  int32_t sec = departure_seconds_until(dep);
  int32_t mins = sec >= 0 ? sec / 60 : 0;

  bool new_run = (strcmp(s_last_run_ref, dep->run_ref) != 0);
  bool decreased = (s_last_vibrated_minutes > 0 && mins < s_last_vibrated_minutes);

  if (new_run || decreased || s_last_vibrated_minutes < 0) {
    s_last_vibrated_minutes = mins;
    if (mins >= 0) {
      haptics_play_for_minutes(mins);
    }
  }
}

static void tick_cb(void *unused) {
  s_tick_timer = NULL;

  Departure *dep = get_watched_departure();
  if (dep) {
    send_watch_start_if_needed(dep);
    maybe_vibrate(dep);
  }
  render();
  schedule_tick();
}

static void schedule_tick(void) {
  if (s_tick_timer) {
    app_timer_cancel(s_tick_timer);
  }
  s_tick_timer = app_timer_register(1000, tick_cb, NULL);
}

static void up_click(ClickRecognizerRef rec, void *context) {
  if (g_app_state.watching_offset != 0) {
    g_app_state.watching_offset = 0;
    s_last_run_ref[0] = '\0';
    s_last_vibrated_minutes = -1;
    render();
    Departure *dep = get_watched_departure();
    if (dep) send_watch_start_if_needed(dep);
  }
}

static void down_click(ClickRecognizerRef rec, void *context) {
  Entry *e = app_state_get_entry(g_app_state.watching_button);
  if (!e) return;
  Departure *next = departures_get(e, 1);
  if (!next || !next->has_data) {
    // No valid service-after — stay silently on Next Service rather than
    // swapping into an empty state or buzzing the user for a no-op.
    return;
  }
  if (g_app_state.watching_offset == 1) return;

  g_app_state.watching_offset = 1;
  s_last_run_ref[0] = '\0';
  s_last_vibrated_minutes = -1;
  render();
  send_watch_start_if_needed(next);
}

static void click_config_provider(void *context) {
  window_single_click_subscribe(BUTTON_ID_UP, up_click);
  window_single_click_subscribe(BUTTON_ID_DOWN, down_click);
}

static void window_load(Window *window) {
  Layer *root = window_get_root_layer(window);
  GRect bounds = layer_get_bounds(root);
  GColor fg = theme_fg();
  window_set_background_color(window, theme_bg());

  // Ripple sits at the back.
  s_ripple_layer = ripple_layer_create(bounds);
  if (s_ripple_layer) {
    layer_add_child(root, s_ripple_layer);
  }

  s_status_layer = text_layer_create(GRect(0, 4, bounds.size.w, 18));
  text_layer_set_font(s_status_layer, fonts_get_system_font(FONT_KEY_GOTHIC_14));
  text_layer_set_text_color(s_status_layer, fg);
  text_layer_set_background_color(s_status_layer, GColorClear);
  text_layer_set_text_alignment(s_status_layer, GTextAlignmentCenter);
  layer_add_child(root, text_layer_get_layer(s_status_layer));

  s_route_layer = text_layer_create(GRect(4, 22, bounds.size.w - 8, 20));
  text_layer_set_font(s_route_layer, fonts_get_system_font(FONT_KEY_GOTHIC_14));
  text_layer_set_text_color(s_route_layer, fg);
  text_layer_set_background_color(s_route_layer, GColorClear);
  text_layer_set_text_alignment(s_route_layer, GTextAlignmentCenter);
  text_layer_set_overflow_mode(s_route_layer, GTextOverflowModeTrailingEllipsis);
  layer_add_child(root, text_layer_get_layer(s_route_layer));

  s_countdown_home_frame = GRect(0, bounds.size.h / 2 - 32, bounds.size.w, 48);
  s_countdown_layer = text_layer_create(s_countdown_home_frame);
  text_layer_set_font(s_countdown_layer, fonts_get_system_font(FONT_KEY_LECO_42_NUMBERS));
  text_layer_set_text_color(s_countdown_layer, fg);
  text_layer_set_background_color(s_countdown_layer, GColorClear);
  text_layer_set_text_alignment(s_countdown_layer, GTextAlignmentCenter);
  layer_add_child(root, text_layer_get_layer(s_countdown_layer));

  s_platform_layer = text_layer_create(GRect(4, bounds.size.h / 2 + 20, bounds.size.w - 8, 18));
  text_layer_set_font(s_platform_layer, fonts_get_system_font(FONT_KEY_GOTHIC_14));
  text_layer_set_text_color(s_platform_layer, fg);
  text_layer_set_background_color(s_platform_layer, GColorClear);
  text_layer_set_text_alignment(s_platform_layer, GTextAlignmentCenter);
  layer_add_child(root, text_layer_get_layer(s_platform_layer));

  s_bottom_layer = text_layer_create(GRect(4, bounds.size.h - 40, bounds.size.w - 8, 18));
  text_layer_set_font(s_bottom_layer, fonts_get_system_font(FONT_KEY_GOTHIC_14));
  text_layer_set_text_color(s_bottom_layer, fg);
  text_layer_set_background_color(s_bottom_layer, GColorClear);
  text_layer_set_text_alignment(s_bottom_layer, GTextAlignmentCenter);
  text_layer_set_overflow_mode(s_bottom_layer, GTextOverflowModeTrailingEllipsis);
  layer_add_child(root, text_layer_get_layer(s_bottom_layer));

  s_progress_layer = layer_create(GRect(0, bounds.size.h - 6, bounds.size.w, 4));
  layer_set_update_proc(s_progress_layer, progress_update_proc);
  layer_add_child(root, s_progress_layer);

  window_set_click_config_provider(window, click_config_provider);

  s_countdown_prev[0] = '\0';

  // Kick first render + watch_start.
  Departure *dep = get_watched_departure();
  if (dep) {
    send_watch_start_if_needed(dep);
    maybe_vibrate(dep);
  }
  render();
  ripple_layer_start(s_ripple_layer);
  schedule_tick();
}

static void window_unload(Window *window) {
  if (s_tick_timer) { app_timer_cancel(s_tick_timer); s_tick_timer = NULL; }
  if (s_shake_anim) { animation_unschedule(s_shake_anim); s_shake_anim = NULL; }
  ripple_layer_stop(s_ripple_layer);
  if (s_ripple_layer) { ripple_layer_destroy(s_ripple_layer); s_ripple_layer = NULL; }
  if (s_status_layer) { text_layer_destroy(s_status_layer); s_status_layer = NULL; }
  if (s_route_layer) { text_layer_destroy(s_route_layer); s_route_layer = NULL; }
  if (s_countdown_layer) { text_layer_destroy(s_countdown_layer); s_countdown_layer = NULL; }
  if (s_platform_layer) { text_layer_destroy(s_platform_layer); s_platform_layer = NULL; }
  if (s_bottom_layer) { text_layer_destroy(s_bottom_layer); s_bottom_layer = NULL; }
  if (s_progress_layer) { layer_destroy(s_progress_layer); s_progress_layer = NULL; }
  window_destroy(s_window);
  s_window = NULL;
  g_app_state.watching_button = 0;
  g_app_state.watching_offset = 0;
  s_last_run_ref[0] = '\0';
  s_last_vibrated_minutes = -1;
  haptics_cancel();
  protocol_send_watch_stop();
}

void watch_window_push(uint8_t button_id) {
  Entry *e = app_state_get_entry(button_id);
  if (!e || !e->configured) return;

  g_app_state.watching_button = button_id;
  g_app_state.watching_offset = 0;
  g_app_state.watched_distance_km_x100 = INT32_MIN;
  g_app_state.watched_vehicle_desc[0] = '\0';
  g_app_state.watched_run_ref[0] = '\0';
  s_last_run_ref[0] = '\0';
  s_last_vibrated_minutes = -1;

  if (s_window) return;

  s_window = window_create();
  window_set_window_handlers(s_window, (WindowHandlers){
    .load = window_load,
    .unload = window_unload,
  });
  window_stack_push(s_window, true);
}

void watch_window_refresh(void) {
  if (s_window) {
    render();
  }
}

bool watch_window_is_open(void) {
  return s_window != NULL;
}
