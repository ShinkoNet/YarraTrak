#include "nearby_window.h"
#include "theme.h"
#include "../protocol.h"
#include <pebble.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

#define MAX_NEARBY 16
#define ROW_HEIGHT 64
typedef struct {
#if defined(PBL_PLATFORM_APLITE)
  char stop[17], destination[21];
#else
  char stop[33], destination[41];
#endif
  time_t departure;
  uint16_t distance;
  uint8_t mode;
} NearbyRow;

static Window *s_window;
static Layer *s_layer;
static AppTimer *s_timer;
static NearbyRow *s_rows;
static void (*s_back)(void);
static uint8_t s_count, s_selected;
static uint16_t s_request;
static bool s_loading, s_partial;
static time_t s_started;
static char s_status[64];

static int split(char *value, char **parts, int capacity) {
  int count = 0;
  while (count < capacity) {
    parts[count++] = value;
    char *next = strchr(value, '|');
    if (!next) break;
    *next = '\0'; value = next + 1;
  }
  return count;
}

static void set_status(const char *status) {
  snprintf(s_status, sizeof(s_status), "%s", status);
  if (s_layer) layer_mark_dirty(s_layer);
}

static void request(void) {
  if (++s_request == 0) ++s_request;
  s_count = s_selected = 0;
  s_partial = false;
  s_loading = s_rows != NULL;
  s_started = time(NULL);
  set_status(s_rows ? "Getting location..." : "Not enough memory");
  if (!s_rows) return;
  char id[8];
  snprintf(id, sizeof(id), "%u", (unsigned)s_request);
  protocol_send_nearby(id);
}

static GColor row_color(uint8_t mode) {
#if defined(PBL_COLOR)
  if (mode == 1) return GColorFromHEX(0xAAFFAA);
  if (mode == 3) return GColorFromHEX(0xFFAAAA);
  return GColorFromHEX(0xAAAAFF);
#else
  return theme_bg();
#endif
}

static void text(GContext *ctx, const char *value, const char *font, int x, int y, int width, int height) {
  graphics_draw_text(ctx, value, fonts_get_system_font(font), GRect(x, y, width, height),
                     GTextOverflowModeTrailingEllipsis, GTextAlignmentLeft, NULL);
}

static void draw(Layer *layer, GContext *ctx) {
  GRect b = layer_get_bounds(layer);
  graphics_context_set_fill_color(ctx, theme_bg());
  graphics_fill_rect(ctx, b, 0, GCornerNone);
  graphics_context_set_text_color(ctx, theme_fg());
  int inset = PBL_IF_ROUND_ELSE(20, 4);
  char heading[40];
  if (s_count) snprintf(heading, sizeof(heading), "%u/%u - 500m%s", s_selected + 1, s_count, s_partial ? " (partial)" : "");
  else snprintf(heading, sizeof(heading), "Nearby - 500m");
  text(ctx, heading, FONT_KEY_GOTHIC_14_BOLD, inset, 0, b.size.w - 2 * inset, 20);
  if (!s_count) {
    graphics_draw_text(ctx, s_status, fonts_get_system_font(FONT_KEY_GOTHIC_18_BOLD),
                       GRect(inset, 26, b.size.w - 2 * inset, 80), GTextOverflowModeWordWrap, GTextAlignmentLeft, NULL);
    text(ctx, "Hold select to refresh", FONT_KEY_GOTHIC_14, inset, b.size.h - 28, b.size.w - 2 * inset, 22);
    return;
  }
  int visible = (b.size.h - 22) / ROW_HEIGHT;
  if (visible < 1) visible = 1;
  int first = s_selected / visible * visible;
  for (int i = first; i < s_count && i < first + visible; ++i) {
    NearbyRow *row = &s_rows[i];
    int y = 22 + (i - first) * ROW_HEIGHT;
    graphics_context_set_fill_color(ctx, row_color(row->mode));
    graphics_fill_rect(ctx, GRect(0, y, b.size.w, ROW_HEIGHT), 0, GCornerNone);
    GColor ink = PBL_IF_COLOR_ELSE(GColorBlack, theme_fg());
    graphics_context_set_text_color(ctx, ink);
    if (i == s_selected) {
      graphics_context_set_stroke_color(ctx, ink);
      graphics_context_set_stroke_width(ctx, 2);
      graphics_draw_rect(ctx, GRect(1, y + 1, b.size.w - 2, ROW_HEIGHT - 2));
    }
    text(ctx, row->stop, FONT_KEY_GOTHIC_14, inset + 2, y, b.size.w - 2 * inset - 4, 18);
    text(ctx, row->destination, FONT_KEY_GOTHIC_18_BOLD, inset + 2, y + 17, b.size.w - 2 * inset - 4, 24);
    long seconds = (long)(row->departure - time(NULL));
    char countdown[18], detail[48];
    if (seconds < -60) snprintf(countdown, sizeof(countdown), "Departed");
    else if (seconds < 60) snprintf(countdown, sizeof(countdown), "Now");
    else snprintf(countdown, sizeof(countdown), "%ld min", seconds / 60);
    const char *mode = row->mode == 1 ? "Tram" : row->mode == 3 ? "V/Line" : "Train";
    snprintf(detail, sizeof(detail), "%s | %um | %s", countdown, (unsigned)row->distance, mode);
    text(ctx, detail, FONT_KEY_GOTHIC_14, inset + 2, y + 40, b.size.w - 2 * inset - 4, 22);
  }
}

static void move_up(ClickRecognizerRef recognizer, void *context) {
  if (s_selected) --s_selected;
  layer_mark_dirty(s_layer);
}
static void move_down(ClickRecognizerRef recognizer, void *context) {
  if (s_selected + 1 < s_count) ++s_selected;
  layer_mark_dirty(s_layer);
}
static void refresh(ClickRecognizerRef recognizer, void *context) { request(); }
static void back(ClickRecognizerRef recognizer, void *context) {
  void (*callback)(void) = s_back;
  nearby_window_close();
  if (callback) callback();
}
static void clicks(void *context) {
  window_single_repeating_click_subscribe(BUTTON_ID_UP, 150, move_up);
  window_single_repeating_click_subscribe(BUTTON_ID_DOWN, 150, move_down);
  window_long_click_subscribe(BUTTON_ID_SELECT, 600, refresh, NULL);
  window_single_click_subscribe(BUTTON_ID_BACK, back);
}
static void tick(void *context) {
  s_timer = NULL;
  if (!s_window) return;
  if (s_loading && time(NULL) - s_started >= 45) {
    s_loading = false;
    set_status("Request timed out");
  }
  layer_mark_dirty(s_layer);
  s_timer = app_timer_register(1000, tick, NULL);
}

void nearby_window_show(Window *window, void (*on_back)(void)) {
  s_window = window;
  s_back = on_back;
  Layer *root = window_get_root_layer(window);
  s_layer = layer_create(layer_get_bounds(root));
  layer_set_update_proc(s_layer, draw);
  layer_add_child(root, s_layer);
  s_rows = malloc(sizeof(NearbyRow) * MAX_NEARBY);
  window_set_click_config_provider(window, clicks);
  request();
  s_timer = app_timer_register(1000, tick, NULL);
}

void nearby_window_close(void) {
  if (!s_window) return;
  if (s_timer) app_timer_cancel(s_timer);
  s_timer = NULL;
  free(s_rows); s_rows = NULL;
  layer_destroy(s_layer); s_layer = NULL;
  s_window = NULL;
  s_loading = false;
}

void nearby_window_receive_row(char *payload) {
  char *parts[8];
  if (!s_window || !s_loading || !s_rows || split(payload, parts, 8) != 8 || atoi(parts[0]) != s_request) return;
  int index = atoi(parts[7]);
  if (index < 0 || index >= MAX_NEARBY || index > s_count) return;
  if (index == s_count) ++s_count;
  NearbyRow *row = &s_rows[index];
  row->mode = atoi(parts[1]); row->distance = atoi(parts[2]);
  row->departure = (time_t)atol(parts[3]);
  snprintf(row->stop, sizeof(row->stop), "%s", parts[4]);
  snprintf(row->destination, sizeof(row->destination), "%s", parts[5]);
  layer_mark_dirty(s_layer);
}

void nearby_window_receive_status(char *payload) {
  char *parts[3];
  if (!s_window || !s_loading || split(payload, parts, 3) != 3 || atoi(parts[0]) != s_request) return;
  if (!strcmp(parts[1], "done")) {
    s_loading = false;
    if (atoi(parts[2]) != s_count) s_partial = true;
    if (!s_count) set_status(s_partial ? "Some stops unavailable" : "No departures within 500m");
  } else if (!strcmp(parts[1], "error")) {
    s_loading = false;
    set_status(parts[2]);
  } else if (!strcmp(parts[1], "partial")) s_partial = true;
  else set_status(parts[2]);
  layer_mark_dirty(s_layer);
}
