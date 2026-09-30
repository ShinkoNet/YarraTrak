#include "nearby_window.h"
#include "theme.h"
#include "watch_window.h"
#include "../app_state.h"
#include "../protocol.h"
#include <pebble.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

#define MAX_NEARBY 16
#define ROW_HEIGHT 40
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
static MenuLayer *s_menu;
static uint8_t s_tracker_index;
static AppTimer *s_timer;
static NearbyRow *s_rows;
static void (*s_back)(void);
static uint8_t s_count;
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
  if (s_menu) menu_layer_reload_data(s_menu);
}

static void request(void) {
  if (++s_request == 0) ++s_request;
  s_count = 0;
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

static uint16_t rows(MenuLayer *menu, uint16_t section, void *context) { return s_count ? s_count : 1; }
static int16_t height(MenuLayer *menu, MenuIndex *index, void *context) { return ROW_HEIGHT; }
static void draw_row(GContext *ctx, const Layer *cell, MenuIndex *index, void *context) {
  GRect b = layer_get_bounds(cell);
  if (!s_count) {
    text(ctx, s_status, FONT_KEY_GOTHIC_18_BOLD, 4, 0, b.size.w - 8, 40);
    return;
  }
  NearbyRow *row = &s_rows[index->row];
  bool selected = menu_cell_layer_is_highlighted(cell);
  graphics_context_set_fill_color(ctx, selected ? theme_accent() : row_color(row->mode));
  graphics_fill_rect(ctx, b, 0, GCornerNone);
  graphics_context_set_text_color(ctx, selected ? PBL_IF_COLOR_ELSE(GColorWhite, theme_bg()) : PBL_IF_COLOR_ELSE(GColorBlack, theme_fg()));
  // Keep the transport tint visible even on the highlighted row.
  graphics_context_set_fill_color(ctx, row_color(row->mode));
  graphics_fill_rect(ctx, GRect(0, 0, 3, ROW_HEIGHT), 0, GCornerNone);
  text(ctx, row->destination, FONT_KEY_GOTHIC_18_BOLD, 4, 0, b.size.w - 8, 22);
  long seconds = (long)(row->departure - time(NULL));
  char detail[80], countdown[18];
  if (seconds < -60) snprintf(countdown, sizeof(countdown), "Departed");
  else if (seconds < 60) snprintf(countdown, sizeof(countdown), "Now");
  else snprintf(countdown, sizeof(countdown), "%ldm", seconds / 60);
  snprintf(detail, sizeof(detail), "%s %um %s", countdown, (unsigned)row->distance, row->stop);
  text(ctx, detail, FONT_KEY_GOTHIC_14, 4, 20, b.size.w - 8, 18);
}
static void draw(Layer *layer, GContext *ctx) {
  GRect b = layer_get_bounds(layer);
  graphics_context_set_fill_color(ctx, PBL_IF_COLOR_ELSE(GColorFromHEX(0x291381), theme_fg()));
  graphics_fill_rect(ctx, b, 0, GCornerNone);
  graphics_context_set_text_color(ctx, PBL_IF_COLOR_ELSE(GColorWhite, theme_bg()));
  char heading[40];
  snprintf(heading, sizeof(heading), "Nearby - 500m%s", s_partial ? " (partial)" : "");
  graphics_draw_text(ctx, heading, fonts_get_system_font(FONT_KEY_GOTHIC_14), b,
    GTextOverflowModeTrailingEllipsis, GTextAlignmentCenter, NULL);
}
static void open_tracker(void *context) {
  if (!s_window || !s_count || g_app_state.watching_button) return;
  s_tracker_index = menu_layer_get_selected_index(s_menu).row;
  NearbyRow *row = &s_rows[s_tracker_index];
  if (!g_nearby_entry) g_nearby_entry = malloc(sizeof(Entry));
  if (!g_nearby_entry) return;
  memset(g_nearby_entry, 0, sizeof(Entry));
  g_nearby_entry->configured = true;
  snprintf(g_nearby_entry->name, sizeof(g_nearby_entry->name), "%s", row->stop);
  snprintf(g_nearby_entry->dest_name, sizeof(g_nearby_entry->dest_name), "%.32s", row->destination);
#if defined(PBL_PLATFORM_APLITE)
  // The classic watch cannot hold both the list and countdown view in 24 KB.
  if (s_timer) app_timer_cancel(s_timer);
  s_timer = NULL;
  menu_layer_destroy(s_menu); s_menu = NULL;
  layer_destroy(s_layer); s_layer = NULL;
  free(s_rows); s_rows = NULL;
  s_loading = false;
#endif
  watch_window_push(255);
  char selection[24];
  snprintf(selection, sizeof(selection), "%u|%u", s_request, s_tracker_index);
  protocol_send_nearby_track(selection);
}
static void select_row(MenuLayer *menu, MenuIndex *index, void *context) {
  if (s_count) app_timer_register(1, open_tracker, NULL);
}
static void refresh_row(MenuLayer *menu, MenuIndex *index, void *context) { request(); menu_layer_reload_data(s_menu); }
static void back(ClickRecognizerRef recognizer, void *context) {
  void (*callback)(void) = s_back;
  nearby_window_close();
  if (callback) callback();
}
static void select_row(MenuLayer *menu, MenuIndex *index, void *context);
static void up(ClickRecognizerRef recognizer, void *context) { menu_layer_set_selected_next(s_menu, true, MenuRowAlignNone, true); }
static void down(ClickRecognizerRef recognizer, void *context) { menu_layer_set_selected_next(s_menu, false, MenuRowAlignNone, true); }
static void select_click(ClickRecognizerRef recognizer, void *context) {
  MenuIndex index = menu_layer_get_selected_index(s_menu);
  select_row(s_menu, &index, NULL);
}
static void refresh_click(ClickRecognizerRef recognizer, void *context) { request(); menu_layer_reload_data(s_menu); }
static void clicks(void *context) {
  window_single_repeating_click_subscribe(BUTTON_ID_UP, 150, up);
  window_single_repeating_click_subscribe(BUTTON_ID_DOWN, 150, down);
  window_single_click_subscribe(BUTTON_ID_SELECT, select_click);
  window_long_click_subscribe(BUTTON_ID_SELECT, 600, refresh_click, NULL);
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
  layer_mark_dirty(menu_layer_get_layer(s_menu));
  s_timer = app_timer_register(1000, tick, NULL);
}

void nearby_window_show(Window *window, void (*on_back)(void)) {
  s_window = window;
  s_back = on_back;
  window_set_background_color(window, theme_bg());
  Layer *root = window_get_root_layer(window);
  GRect bounds = layer_get_bounds(root);
  s_layer = layer_create(GRect(0, 0, bounds.size.w, 16));
  layer_set_update_proc(s_layer, draw);
  layer_add_child(root, s_layer);
  s_rows = malloc(sizeof(NearbyRow) * MAX_NEARBY);
  s_menu = menu_layer_create(GRect(0, 16, bounds.size.w, bounds.size.h - 16));
  menu_layer_set_callbacks(s_menu, NULL, (MenuLayerCallbacks){.get_num_rows = rows, .get_cell_height = height,
    .draw_row = draw_row, .select_click = select_row, .select_long_click = refresh_row});
  menu_layer_set_normal_colors(s_menu, theme_bg(), theme_fg());
  menu_layer_set_highlight_colors(s_menu, theme_accent(), PBL_IF_COLOR_ELSE(GColorWhite, theme_bg()));
  layer_add_child(root, menu_layer_get_layer(s_menu));
  window_set_click_config_provider(window, clicks);
  request();
  s_timer = app_timer_register(1000, tick, NULL);
}

void nearby_window_close(void) {
  if (!s_window) return;
  if (s_timer) app_timer_cancel(s_timer);
  s_timer = NULL;
  free(s_rows); s_rows = NULL;
  free(g_nearby_entry); g_nearby_entry = NULL;
  if (s_menu) menu_layer_destroy(s_menu);
  s_menu = NULL;
  if (s_layer) layer_destroy(s_layer);
  s_layer = NULL;
  s_window = NULL;
  s_loading = false;
}

void nearby_window_resume(void) {
#if defined(PBL_PLATFORM_APLITE)
  if (s_window && !s_menu) {
    free(g_nearby_entry); g_nearby_entry = NULL;
    nearby_window_show(s_window, s_back);
  }
#endif
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
  menu_layer_reload_data(s_menu);
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
  menu_layer_reload_data(s_menu);
}

void nearby_window_receive_tracker(char *payload) {
  char *parts[6];
  if (!s_window || g_app_state.watching_button != 255 || !g_nearby_entry ||
      split(payload, parts, 6) != 6 || atoi(parts[0]) != s_request || atoi(parts[1]) != s_tracker_index) return;
  g_nearby_entry->stop_id = atol(parts[2]);
  g_nearby_entry->route_type = atoi(parts[3]);
  snprintf(g_nearby_entry->route_id, sizeof(g_nearby_entry->route_id), "%s", parts[4]);
  g_nearby_entry->direction_id = atol(parts[5]);
}
