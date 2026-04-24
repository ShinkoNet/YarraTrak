#include "watch_window.h"
#include "fx_layer.h"
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
static Layer     *s_fx_layer = NULL;
static TextLayer *s_status_layer = NULL;
static TextLayer *s_countdown_layer = NULL;
static Layer     *s_info_layer = NULL;  // boxed platform badge + departure time
static TextLayer *s_route_layer = NULL;
static TextLayer *s_bottom_layer = NULL;
static Layer     *s_progress_layer = NULL;
static AppTimer  *s_tick_timer = NULL;

static char s_status_buf[24];
static char s_countdown_buf[16];
static char s_countdown_prev[16];
static char s_platform_badge[8];    // e.g. "P2", "P13", "PA" — "" if no platform
static char s_time_buf[10];         // "17:27" or "5:27 PM" — "" if unknown
static char s_route_buf[48];
static char s_bottom_buf[48];

static int32_t s_last_vibrated_minutes = -1;
static bool    s_now_pattern_fired = false;  // NOW played for the active run
static char s_last_run_ref[RUN_REF_LEN] = "";
static GRect s_countdown_home_frame;
static Animation *s_shake_anim = NULL;
static int32_t s_last_seconds = INT32_MAX;  // previous render's seconds_until

static void schedule_tick(void);

static Departure *get_watched_departure(void) {
  Entry *e = app_state_get_entry(g_app_state.watching_button);
  if (!e) return NULL;
  return departures_get(e, g_app_state.watching_offset);
}

static bool has_service_at_offset(uint8_t offset) {
  Entry *e = app_state_get_entry(g_app_state.watching_button);
  if (!e) return false;
  Departure *d = departures_get(e, offset);
  return d && d->has_data;
}

static bool has_alternate_service(void) {
  return has_service_at_offset(1);
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

// Draws the "[P2]  17:27" badge + time row centred in its own frame. A
// bordered rectangle wraps the platform label so it reads as a symbol, not
// a word; the time floats to the right with a small gap.
static void info_layer_update_proc(Layer *layer, GContext *ctx) {
  GRect bounds = layer_get_bounds(layer);
  GColor fg = theme_fg();
  graphics_context_set_text_color(ctx, fg);
  graphics_context_set_stroke_color(ctx, fg);
  graphics_context_set_stroke_width(ctx, 1);

  GFont font = fonts_get_system_font(FONT_KEY_GOTHIC_18_BOLD);

  GSize plat_sz = GSize(0, 0);
  if (s_platform_badge[0]) {
    plat_sz = graphics_text_layout_get_content_size(
        s_platform_badge, font, GRect(0, 0, 80, 30),
        GTextOverflowModeTrailingEllipsis, GTextAlignmentCenter);
  }
  GSize time_sz = GSize(0, 0);
  if (s_time_buf[0]) {
    time_sz = graphics_text_layout_get_content_size(
        s_time_buf, font, GRect(0, 0, 120, 30),
        GTextOverflowModeTrailingEllipsis, GTextAlignmentLeft);
  }

  const int16_t BOX_PAD_X = 4;
  const int16_t BOX_H = 20;
  const int16_t GAP = 6;
  int16_t box_w = s_platform_badge[0] ? plat_sz.w + BOX_PAD_X * 2 : 0;
  int16_t total_w = box_w + (s_platform_badge[0] && s_time_buf[0] ? GAP : 0) + time_sz.w;
  int16_t x = (bounds.size.w - total_w) / 2;
  int16_t y = (bounds.size.h - BOX_H) / 2;

  if (s_platform_badge[0]) {
    GRect box = GRect(x, y, box_w, BOX_H);
    graphics_draw_rect(ctx, box);
    // Nudge text up 3px because GOTHIC_18_BOLD has an ascender-heavy
    // bounding box — centres visually, not numerically.
    graphics_draw_text(ctx, s_platform_badge, font,
                       GRect(x, y - 3, box_w, BOX_H),
                       GTextOverflowModeTrailingEllipsis,
                       GTextAlignmentCenter, NULL);
    x += box_w + GAP;
  }

  if (s_time_buf[0]) {
    graphics_draw_text(ctx, s_time_buf, font,
                       GRect(x, y - 3, time_sz.w + 4, BOX_H),
                       GTextOverflowModeTrailingEllipsis,
                       GTextAlignmentLeft, NULL);
  }
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

static void cancel_running_anim(void) {
  if (!s_shake_anim) return;
  layer_set_frame(text_layer_get_layer(s_countdown_layer), s_countdown_home_frame);
  animation_unschedule(s_shake_anim);
  s_shake_anim = NULL;
}

// V1 'bounce': the countdown ticked down — tiny +1y bob then back.
// Fires every second the user's watching, so kept minimal (~70 ms).
static void trigger_bounce(void) {
  if (g_app_state.flags.disable_timer_shake) return;
  if (!s_countdown_layer) return;
  cancel_running_anim();

  GRect home = s_countdown_home_frame;
  GRect down = home; down.origin.y += 1;

  PropertyAnimation *a = property_animation_create_layer_frame(
      text_layer_get_layer(s_countdown_layer), &home, &down);
  PropertyAnimation *b = property_animation_create_layer_frame(
      text_layer_get_layer(s_countdown_layer), &down, &home);
  animation_set_duration((Animation *)a, 35);
  animation_set_duration((Animation *)b, 35);
  animation_set_curve((Animation *)a, AnimationCurveLinear);
  animation_set_curve((Animation *)b, AnimationCurveLinear);

  Animation *seq = animation_sequence_create(
      (Animation *)a, (Animation *)b, NULL);
  animation_set_handlers(seq, (AnimationHandlers){
    .stopped = shake_anim_stopped,
  }, NULL);
  s_shake_anim = seq;
  animation_schedule(seq);
}

// V1 'shake': the countdown *increased* — ETA slipped, i.e. a delay was
// detected. Horizontal wiggle -2, +2, -1, +1, 0 over ~180 ms to draw the
// eye to the number. Much more intrusive than the per-tick bounce.
static void trigger_shake(void) {
  if (g_app_state.flags.disable_timer_shake) return;
  if (!s_countdown_layer) return;
  cancel_running_anim();

  GRect home = s_countdown_home_frame;
  GRect l2 = home; l2.origin.x -= 2;
  GRect r2 = home; r2.origin.x += 2;
  GRect l1 = home; l1.origin.x -= 1;
  GRect r1 = home; r1.origin.x += 1;

  PropertyAnimation *a = property_animation_create_layer_frame(
      text_layer_get_layer(s_countdown_layer), &home, &l2);
  PropertyAnimation *b = property_animation_create_layer_frame(
      text_layer_get_layer(s_countdown_layer), &l2, &r2);
  PropertyAnimation *c = property_animation_create_layer_frame(
      text_layer_get_layer(s_countdown_layer), &r2, &l1);
  PropertyAnimation *d = property_animation_create_layer_frame(
      text_layer_get_layer(s_countdown_layer), &l1, &r1);
  PropertyAnimation *e = property_animation_create_layer_frame(
      text_layer_get_layer(s_countdown_layer), &r1, &home);
  animation_set_duration((Animation *)a, 45);
  animation_set_duration((Animation *)b, 45);
  animation_set_duration((Animation *)c, 45);
  animation_set_duration((Animation *)d, 45);
  animation_set_duration((Animation *)e, 45);
  animation_set_curve((Animation *)a, AnimationCurveLinear);
  animation_set_curve((Animation *)b, AnimationCurveLinear);
  animation_set_curve((Animation *)c, AnimationCurveLinear);
  animation_set_curve((Animation *)d, AnimationCurveLinear);
  animation_set_curve((Animation *)e, AnimationCurveLinear);

  Animation *seq = animation_sequence_create(
      (Animation *)a, (Animation *)b, (Animation *)c,
      (Animation *)d, (Animation *)e, NULL);
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

  // Auto-correct: if the current offset no longer has data (service ran
  // through, cache shrank after a sync), fall back one step at a time
  // until we land on one that does — never past 0.
  while (g_app_state.watching_offset > 0 &&
         !has_service_at_offset(g_app_state.watching_offset)) {
    g_app_state.watching_offset--;
  }

  // Status text. Three browseable slots total: Next / After / 3rd. The
  // labels stay short to fit the narrow status row without ellipses.
  if (g_app_state.conn_state != CONN_CONNECTED) {
    strncpy(s_status_buf, "Reconnecting...", sizeof(s_status_buf) - 1);
  } else {
    const char *label;
    switch (g_app_state.watching_offset) {
      case 0:  label = "Next Service";   break;
      case 1:  label = "Service After";  break;
      default: label = "Third Service";  break;
    }
    strncpy(s_status_buf, label, sizeof(s_status_buf) - 1);
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

    // V1 semantics: countdown went DOWN → bounce (tick). Countdown went
    // UP → shake (ETA slipped). First render after watch start has no
    // prior value and skips the animation entirely.
    int32_t new_sec = sec;
    if (s_last_seconds != INT32_MAX && new_sec != INT32_MAX) {
      if (new_sec < s_last_seconds) {
        trigger_bounce();
      } else if (new_sec > s_last_seconds + 1) {
        // Guard against +1 s jitter from minute-precision deps — require
        // a real jump before interpreting as a delay. Fire a long pulse
        // alongside the horizontal shake so the wrist feels the slip too.
        trigger_shake();
        haptics_long();
      }
    }
    s_last_seconds = new_sec;
  }

  // Platform badge (drawn as a bordered "P<num>" box by s_info_layer) +
  // absolute HH:MM time. Countdown tells you how long; time tells you
  // *which* service. 12h mode tacks a PM/AM suffix per the user's flag.
  s_time_buf[0] = '\0';
  if (dep && dep->has_data && dep->departure_unix != 0) {
    time_t t = dep->departure_unix;
    struct tm *lt = localtime(&t);
    if (lt) {
      const char *fmt = g_app_state.flags.use_24hr_time ? "%H:%M" : "%l:%M %p";
      strftime(s_time_buf, sizeof(s_time_buf), fmt, lt);
      if (s_time_buf[0] == ' ') memmove(s_time_buf, s_time_buf + 1, strlen(s_time_buf));
    }
  }

  s_platform_badge[0] = '\0';
  if (dep && dep->has_data && dep->platform[0]) {
    snprintf(s_platform_badge, sizeof(s_platform_badge), "P%s", dep->platform);
  }

  if (s_info_layer) layer_mark_dirty(s_info_layer);

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
  // V1 JS parity: count ticks for the *rounded* minute so rolling 3:00→2:59
  // buzzes three times (2 + 1) and 1:00→0:59 buzzes once (0 + 1). Under 30 s
  // the round-up collapses to 0 and haptics_play_for_minutes fires the
  // shave-and-haircut NOW pattern.
  int32_t extra = (sec >= 0 && (sec % 60) >= 30) ? 1 : 0;

  bool new_run = (strcmp(s_last_run_ref, dep->run_ref) != 0);
  bool decreased = (s_last_vibrated_minutes > 0 && mins < s_last_vibrated_minutes);

  if (new_run || decreased || s_last_vibrated_minutes < 0) {
    if (new_run) s_now_pattern_fired = false;
    s_last_vibrated_minutes = mins;
    if (mins >= 0) {
      haptics_play_for_minutes(mins + extra);
      if (sec < 30) s_now_pattern_fired = true;
    }
    return;
  }

  // Minute floor stops changing under 60 s, but the display flips to "NOW!"
  // at the 30 s mark — fire the NOW haptic exactly once on that transition.
  if (!s_now_pattern_fired && sec >= 0 && sec < 30) {
    s_now_pattern_fired = true;
    haptics_play_for_minutes(0);
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

static void change_offset(uint8_t new_offset) {
  if (g_app_state.watching_offset == new_offset) return;
  g_app_state.watching_offset = new_offset;
  s_last_run_ref[0] = '\0';
  s_last_vibrated_minutes = -1;
  s_now_pattern_fired = false;
  s_last_seconds = INT32_MAX;
  render();
  Departure *dep = get_watched_departure();
  if (dep) send_watch_start_if_needed(dep);
}

static void up_click(ClickRecognizerRef rec, void *context) {
  // Step one slot closer to "now": 2 -> 1 -> 0 -> stop.
  uint8_t cur = g_app_state.watching_offset;
  if (cur == 0) return;
  change_offset(cur - 1);
}

static void down_click(ClickRecognizerRef rec, void *context) {
  // Step one slot further out: 0 -> 1 -> 2 -> stop. Skip if the next
  // slot has no data (server only shipped N deps, or we're at the back
  // of the cache).
  uint8_t cur = g_app_state.watching_offset;
  if (cur >= MAX_DEPS_PER_ENTRY - 1) return;
  if (!has_service_at_offset(cur + 1)) return;
  change_offset(cur + 1);
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
  s_fx_layer = fx_layer_create(bounds);
  if (s_fx_layer) {
    layer_add_child(root, s_fx_layer);
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

  s_info_layer = layer_create(GRect(4, bounds.size.h / 2 + 18, bounds.size.w - 8, 24));
  layer_set_update_proc(s_info_layer, info_layer_update_proc);
  layer_add_child(root, s_info_layer);

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
  fx_layer_start(s_fx_layer);
  schedule_tick();
}

static void window_unload(Window *window) {
  if (s_tick_timer) { app_timer_cancel(s_tick_timer); s_tick_timer = NULL; }
  if (s_shake_anim) { animation_unschedule(s_shake_anim); s_shake_anim = NULL; }
  fx_layer_stop(s_fx_layer);
  if (s_fx_layer) { fx_layer_destroy(s_fx_layer); s_fx_layer = NULL; }
  if (s_status_layer) { text_layer_destroy(s_status_layer); s_status_layer = NULL; }
  if (s_route_layer) { text_layer_destroy(s_route_layer); s_route_layer = NULL; }
  if (s_countdown_layer) { text_layer_destroy(s_countdown_layer); s_countdown_layer = NULL; }
  if (s_info_layer) { layer_destroy(s_info_layer); s_info_layer = NULL; }
  if (s_bottom_layer) { text_layer_destroy(s_bottom_layer); s_bottom_layer = NULL; }
  if (s_progress_layer) { layer_destroy(s_progress_layer); s_progress_layer = NULL; }
  window_destroy(s_window);
  s_window = NULL;
  g_app_state.watching_button = 0;
  g_app_state.watching_offset = 0;
  s_last_run_ref[0] = '\0';
  s_last_vibrated_minutes = -1;
  s_now_pattern_fired = false;
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
  s_now_pattern_fired = false;
  s_last_seconds = INT32_MAX;

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

void watch_window_close(void) {
  // window_stack_remove triggers window_unload which tears the layers
  // down, cancels timers and resets s_window to NULL. Safe even if the
  // watch isn't currently the top window.
  if (s_window) {
    window_stack_remove(s_window, true);
  }
}
