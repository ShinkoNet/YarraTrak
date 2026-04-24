#include "menu_window.h"
#include "watch_window.h"
#include "../app_state.h"
#include "../protocol.h"
#include "../formatting.h"
#include "../departures.h"

#include <pebble.h>
#include <string.h>

static Window *s_window = NULL;
static MenuLayer *s_menu_layer = NULL;

// Row indices for system rows + favourites.
// Row 0: connection banner (if !connected)
// Then favourites 1..entry_count
static bool has_banner(void) {
  return g_app_state.conn_state != CONN_CONNECTED;
}

static uint16_t get_num_rows(MenuLayer *menu_layer, uint16_t section_index, void *context) {
  uint16_t rows = g_app_state.entry_count;
  if (has_banner()) rows += 1;
  if (rows == 0) rows = 1;  // "No favourites" row
  return rows;
}

static void draw_row(GContext *ctx, const Layer *cell_layer, MenuIndex *cell_index, void *context) {
  GRect bounds = layer_get_bounds(cell_layer);

  char title[64];
  char subtitle[32];
  title[0] = '\0';
  subtitle[0] = '\0';

  uint16_t row = cell_index->row;
  if (has_banner()) {
    if (row == 0) {
      strncpy(title, g_app_state.conn_state == CONN_CONNECTING ? "Connecting..." : "Offline",
              sizeof(title) - 1);
      title[sizeof(title) - 1] = '\0';
      subtitle[0] = '\0';
      goto draw;
    }
    row -= 1;
  }

  if (g_app_state.entry_count == 0) {
    strncpy(title, "No favourites", sizeof(title) - 1);
    title[sizeof(title) - 1] = '\0';
    strncpy(subtitle, "Open config", sizeof(subtitle) - 1);
    subtitle[sizeof(subtitle) - 1] = '\0';
    goto draw;
  }

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
  uint16_t row = cell_index->row;
  if (has_banner()) {
    if (row == 0) {
      protocol_send_refresh();
      return;
    }
    row -= 1;
  }

  if (g_app_state.entry_count == 0) {
    protocol_send_open_config();
    return;
  }

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
  s_menu_layer = menu_layer_create(bounds);
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

  // Default selection to the first favourite row rather than the banner.
  if (has_banner() && g_app_state.entry_count > 0) {
    menu_layer_set_selected_index(s_menu_layer,
                                  (MenuIndex){ .section = 0, .row = 1 },
                                  MenuRowAlignCenter, false);
  }
}

static void window_unload(Window *window) {
  if (s_menu_layer) { menu_layer_destroy(s_menu_layer); s_menu_layer = NULL; }
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
