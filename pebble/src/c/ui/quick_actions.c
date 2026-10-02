#include "quick_actions.h"
#include "nearby_window.h"
#include "query_window.h"
#include "theme.h"
#include "../app_state.h"
#include "../protocol.h"

static Window *s_window;
static MenuLayer *s_menu;
static bool s_nearby;
static bool s_direct_nearby;
static void back(ClickRecognizerRef recognizer, void *context) { window_stack_pop(true); }
static void select_row(MenuLayer *menu, MenuIndex *index, void *context);
static void up(ClickRecognizerRef recognizer, void *context) { menu_layer_set_selected_next(s_menu, true, MenuRowAlignNone, true); }
static void down(ClickRecognizerRef recognizer, void *context) { menu_layer_set_selected_next(s_menu, false, MenuRowAlignNone, true); }
static void select_click(ClickRecognizerRef recognizer, void *context) {
  MenuIndex index = menu_layer_get_selected_index(s_menu);
  select_row(s_menu, &index, NULL);
}
static void clicks(void *context) {
  window_single_repeating_click_subscribe(BUTTON_ID_UP, 150, up);
  window_single_repeating_click_subscribe(BUTTON_ID_DOWN, 150, down);
  window_single_click_subscribe(BUTTON_ID_SELECT, select_click);
  window_single_click_subscribe(BUTTON_ID_BACK, back);
}
static void restore(void);

static void open_nearby(void *context) {
  if (!s_window || !s_menu) return;
  menu_layer_destroy(s_menu);
  s_menu = NULL;
  s_nearby = true;
  nearby_window_show(s_window, restore);
}

static uint16_t rows(MenuLayer *menu, uint16_t section, void *context) { return 2; }
static int16_t height(MenuLayer *menu, MenuIndex *index, void *context) { return 62; }

static void draw(GContext *ctx, const Layer *cell, MenuIndex *index, void *context) {
  if (index->row == 0) {
#if defined(PBL_MICROPHONE)
    const char *subtitle = g_app_state.flags.disable_ai_assistant ? "Enable AI in settings" : "Voice assistant";
#else
    const char *subtitle = "Microphone required";
#endif
    menu_cell_basic_draw(ctx, cell, "Ask", subtitle, NULL);
  } else {
    GRect b = layer_get_bounds(cell);
    graphics_draw_text(ctx, "Find nearest\ndepartures", fonts_get_system_font(FONT_KEY_GOTHIC_18_BOLD),
                       GRect(5, 2, b.size.w - 10, 42), GTextOverflowModeTrailingEllipsis, GTextAlignmentLeft, NULL);
    graphics_draw_text(ctx, "Within 500m", fonts_get_system_font(FONT_KEY_GOTHIC_14),
                       GRect(5, 42, b.size.w - 10, 18), GTextOverflowModeTrailingEllipsis, GTextAlignmentLeft, NULL);
  }
}

static void select_row(MenuLayer *menu, MenuIndex *index, void *context) {
  if (index->row == 1) {
    app_timer_register(1, open_nearby, NULL);
    return;
  }
#if defined(PBL_MICROPHONE)
  if (g_app_state.flags.disable_ai_assistant) protocol_send_open_config();
  else query_window_start();
#endif
}

static void load(Window *window) {
  window_set_background_color(window, theme_bg());
  if (s_direct_nearby) {
    s_nearby = true;
    nearby_window_show(window, restore);
    return;
  }
  Layer *root = window_get_root_layer(window);
  s_menu = menu_layer_create(layer_get_bounds(root));
  menu_layer_set_callbacks(s_menu, NULL, (MenuLayerCallbacks){
    .get_num_rows = rows, .get_cell_height = height, .draw_row = draw, .select_click = select_row,
  });
  menu_layer_set_normal_colors(s_menu, theme_bg(), theme_fg());
  menu_layer_set_highlight_colors(s_menu, theme_accent(), PBL_IF_COLOR_ELSE(GColorWhite, theme_bg()));
  window_set_click_config_provider(window, clicks);
  layer_add_child(root, menu_layer_get_layer(s_menu));
}

static void unload(Window *window) {
  nearby_window_close();
  s_nearby = false;
  if (s_menu) menu_layer_destroy(s_menu);
  s_menu = NULL;
  window_destroy(s_window);
  s_window = NULL;
}

static void appear(Window *window) {
  if (s_nearby) nearby_window_resume();
  if (!s_menu && !s_nearby) load(window);
}

static void restore(void) {
  s_nearby = false;
  if (s_direct_nearby) window_stack_pop(true);
  else load(s_window);
}

void quick_actions_push(void) {
  if (s_window) return;
  s_direct_nearby = g_app_state.flags.disable_ai_assistant;
  s_window = window_create();
  window_set_window_handlers(s_window, (WindowHandlers){.load = load, .appear = appear, .unload = unload});
  window_stack_push(s_window, true);
}
