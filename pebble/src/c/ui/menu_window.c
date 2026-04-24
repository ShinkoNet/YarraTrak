#include "menu_window.h"
#include "watch_window.h"
#include "../app_state.h"
#include "../protocol.h"
#include "../formatting.h"
#include "../departures.h"

#include <pebble.h>
#include <string.h>
#include <stdio.h>

#define TIME_BAR_HEIGHT 16

static Window *s_window = NULL;
static MenuLayer *s_menu_layer = NULL;
static TextLayer *s_time_layer = NULL;
static Layer *s_time_bar = NULL;
static char s_time_buf[8];

// The menu shows favourites only; the per-row subtitle ("Waiting...") carries
// the connection signal without a dedicated banner row. A thin top bar shows
// the current time so users don't have to leave the app to check it.

static void time_bar_update_proc(Layer *layer, GContext *ctx) {
  GRect bounds = layer_get_bounds(layer);
#if defined(PBL_COLOR)
  graphics_context_set_fill_color(ctx, GColorFromHEX(0x291381));
#else
  graphics_context_set_fill_color(ctx, GColorBlack);
#endif
  graphics_fill_rect(ctx, bounds, 0, GCornerNone);
}

static void format_time_now(void) {
  time_t now = time(NULL);
  struct tm *lt = localtime(&now);
  if (!lt) {
    s_time_buf[0] = '\0';
    return;
  }
  // 24h on aplite has no am/pm glyphs to worry about either way; match the
  // user's use_24hr_time preference if set, else default to 12h with no
  // suffix to keep the strip short.
  if (g_app_state.flags.use_24hr_time) {
    strftime(s_time_buf, sizeof(s_time_buf), "%H:%M", lt);
  } else {
    strftime(s_time_buf, sizeof(s_time_buf), "%l:%M", lt);
    // trim leading space %l leaves
    if (s_time_buf[0] == ' ') {
      memmove(s_time_buf, s_time_buf + 1, strlen(s_time_buf));
    }
  }
}

static void tick_handler(struct tm *tick_time, TimeUnits units_changed) {
  format_time_now();
  if (s_time_layer) text_layer_set_text(s_time_layer, s_time_buf);
}

static uint16_t get_num_rows(MenuLayer *menu_layer, uint16_t section_index, void *context) {
  return g_app_state.entry_count > 0 ? g_app_state.entry_count : 1;
}

static void draw_row(GContext *ctx, const Layer *cell_layer, MenuIndex *cell_index, void *context) {
  GRect bounds = layer_get_bounds(cell_layer);

  char title[64];
  char subtitle[32];
  title[0] = '\0';
  subtitle[0] = '\0';

  if (g_app_state.entry_count == 0) {
    strncpy(title, "No favourites", sizeof(title) - 1);
    title[sizeof(title) - 1] = '\0';
    strncpy(subtitle, "Open config", sizeof(subtitle) - 1);
    subtitle[sizeof(subtitle) - 1] = '\0';
    goto draw;
  }

  uint16_t row = cell_index->row;
  if (row < g_app_state.entry_count) {
    Entry *e = &g_app_state.entries[row];
    fmt_menu_title(e, title, sizeof(title));
    Departure *dep = departures_get(e, 0);

    if (e->disruption_count > 0 && (time(NULL) / 3) % 2 == 0) {
      strncpy(subtitle, e->disruptions[0], sizeof(subtitle) - 1);
      subtitle[sizeof(subtitle) - 1] = '\0';
    } else {
      fmt_menu_subtitle(dep, subtitle, sizeof(subtitle));
    }
  }

draw:;
  bool highlighted = menu_cell_layer_is_highlighted(cell_layer);
  graphics_context_set_text_color(ctx, highlighted ? GColorWhite : GColorBlack);

  graphics_draw_text(ctx, title,
                     fonts_get_system_font(FONT_KEY_GOTHIC_18_BOLD),
                     GRect(4, 0, bounds.size.w - 8, 22),
                     GTextOverflowModeTrailingEllipsis,
                     GTextAlignmentLeft, NULL);

  if (subtitle[0]) {
    graphics_draw_text(ctx, subtitle,
                       fonts_get_system_font(FONT_KEY_GOTHIC_14),
                       GRect(4, 20, bounds.size.w - 8, 18),
                       GTextOverflowModeTrailingEllipsis,
                       GTextAlignmentLeft, NULL);
  }
}

static int16_t get_cell_height(MenuLayer *menu_layer, MenuIndex *cell_index, void *context) {
  return 40;
}

static void select_click(MenuLayer *menu_layer, MenuIndex *cell_index, void *context) {
  if (g_app_state.entry_count == 0) {
    protocol_send_open_config();
    return;
  }

  uint16_t row = cell_index->row;
  if (row < g_app_state.entry_count) {
    uint8_t button_id = (uint8_t)(row + 1);
    watch_window_push(button_id);
  }
}

static void select_long_click(MenuLayer *menu_layer, MenuIndex *cell_index, void *context) {
  protocol_send_open_config();
}

static void window_load(Window *window) {
  Layer *root = window_get_root_layer(window);
  GRect bounds = layer_get_bounds(root);

  // Top time strip: filled rect with the current time centred.
  s_time_bar = layer_create(GRect(0, 0, bounds.size.w, TIME_BAR_HEIGHT));
  layer_set_update_proc(s_time_bar, time_bar_update_proc);
  layer_add_child(root, s_time_bar);

  format_time_now();
  s_time_layer = text_layer_create(GRect(0, -2, bounds.size.w, TIME_BAR_HEIGHT + 2));
  text_layer_set_text(s_time_layer, s_time_buf);
  text_layer_set_font(s_time_layer, fonts_get_system_font(FONT_KEY_GOTHIC_14_BOLD));
  text_layer_set_text_color(s_time_layer, GColorWhite);
  text_layer_set_background_color(s_time_layer, GColorClear);
  text_layer_set_text_alignment(s_time_layer, GTextAlignmentCenter);
  layer_add_child(root, text_layer_get_layer(s_time_layer));

  // Menu fills everything below the time strip.
  GRect menu_frame = GRect(0, TIME_BAR_HEIGHT,
                           bounds.size.w,
                           bounds.size.h - TIME_BAR_HEIGHT);
  s_menu_layer = menu_layer_create(menu_frame);
  menu_layer_set_callbacks(s_menu_layer, NULL, (MenuLayerCallbacks){
    .get_num_rows = get_num_rows,
    .draw_row = draw_row,
    .get_cell_height = get_cell_height,
    .select_click = select_click,
    .select_long_click = select_long_click,
  });
  menu_layer_set_click_config_onto_window(s_menu_layer, window);
#if defined(PBL_COLOR)
  menu_layer_set_normal_colors(s_menu_layer, GColorWhite, GColorBlack);
  menu_layer_set_highlight_colors(s_menu_layer, GColorVividCerulean, GColorWhite);
#endif
  layer_add_child(root, menu_layer_get_layer(s_menu_layer));

  tick_timer_service_subscribe(MINUTE_UNIT, tick_handler);
}

static void window_unload(Window *window) {
  tick_timer_service_unsubscribe();
  if (s_menu_layer) { menu_layer_destroy(s_menu_layer); s_menu_layer = NULL; }
  if (s_time_layer) { text_layer_destroy(s_time_layer); s_time_layer = NULL; }
  if (s_time_bar)   { layer_destroy(s_time_bar); s_time_bar = NULL; }
  window_destroy(s_window);
  s_window = NULL;
}

void menu_window_push(void) {
  if (s_window) {
    window_stack_push(s_window, true);
    return;
  }
  s_window = window_create();
  window_set_window_handlers(s_window, (WindowHandlers){
    .load = window_load,
    .unload = window_unload,
  });
  window_stack_push(s_window, true);
}

void menu_window_refresh(void) {
  if (s_menu_layer) {
    menu_layer_reload_data(s_menu_layer);
  }
}

bool menu_window_is_on_top(void) {
  return s_window && window_stack_get_top_window() == s_window;
}
