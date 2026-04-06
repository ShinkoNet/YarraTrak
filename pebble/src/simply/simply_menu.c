#include "simply_menu.h"

#include "simply_res.h"
#include "simply_msg.h"
#include "simply_window_stack.h"

#include "simply.h"

#include "util/color.h"
#include "util/compat.h"
#include "util/display.h"
#include "util/graphics.h"
#include "util/graphics_text.h"
#include "util/menu_layer.h"
#include "util/noop.h"
#include "util/platform.h"
#include "util/string.h"

#include <pebble.h>

#define MAX_CACHED_SECTIONS 10

#define MAX_CACHED_ITEMS IF_APLITE_ELSE(6, 51)

#define EMPTY_TITLE ""

#define SPINNER_MS 66
#define MENU_ROW_GRADIENT_STEPS 10

static GColor8 prv_menu_background_color(void) {
#if defined(PBL_COLOR)
  return GColorFromRGB(41, 19, 129);
#else
  return GColor8Black;
#endif
}

typedef Packet MenuClearPacket;

typedef struct MenuClearSectionPacket MenuClearSectionPacket;

struct __attribute__((__packed__)) MenuClearSectionPacket {
  Packet packet;
  uint16_t section;
};

typedef struct MenuPropsPacket MenuPropsPacket;

struct __attribute__((__packed__)) MenuPropsPacket {
  Packet packet;
  uint16_t num_sections;
  GColor8 background_color;
  GColor8 text_color;
  GColor8 highlight_background_color;
  GColor8 highlight_text_color;
};

typedef struct MenuSectionPacket MenuSectionPacket;

struct __attribute__((__packed__)) MenuSectionPacket {
  Packet packet;
  uint16_t section;
  uint16_t num_items;
  GColor8 background_color;
  GColor8 text_color;
  uint16_t title_length;
  char title[];
};

typedef struct MenuItemPacket MenuItemPacket;

struct __attribute__((__packed__)) MenuItemPacket {
  Packet packet;
  uint16_t section;
  uint16_t item;
  uint32_t icon;
  uint16_t title_length;
  uint16_t subtitle_length;
  char buffer[];
};

typedef struct MenuItemEventPacket MenuItemEventPacket;

struct __attribute__((__packed__)) MenuItemEventPacket {
  Packet packet;
  uint16_t section;
  uint16_t item;
};

typedef Packet MenuGetSelectionPacket;

typedef struct MenuSelectionPacket MenuSelectionPacket;

struct __attribute__((__packed__)) MenuSelectionPacket {
  Packet packet;
  uint16_t section;
  uint16_t item;
  MenuRowAlign align:8;
  bool animated;
};


#if !defined(PBL_COLOR)
static GColor8 s_normal_palette[] = { { GColorBlackARGB8 }, { GColorClearARGB8 } };
static GColor8 s_inverted_palette[] = { { GColorWhiteARGB8 }, { GColorClearARGB8 } };
#endif


static void simply_menu_clear_section_items(SimplyMenu *self, int section_index);
static void simply_menu_clear(SimplyMenu *self);

static void simply_menu_set_num_sections(SimplyMenu *self, uint16_t num_sections);
static void simply_menu_add_section(SimplyMenu *self, SimplyMenuSection *section);
static void simply_menu_add_item(SimplyMenu *self, SimplyMenuItem *item);

static MenuIndex simply_menu_get_selection(SimplyMenu *self);
static void simply_menu_set_selection(SimplyMenu *self, MenuIndex menu_index, MenuRowAlign align, bool animated);

static void refresh_spinner_timer(SimplyMenu *self);


static int64_t prv_get_milliseconds(void) {
  time_t now_s;
  uint16_t now_ms_part;
  time_ms(&now_s, &now_ms_part);
  return ((int64_t) now_s) * 1000 + now_ms_part;
}

static bool prv_send_menu_item(Command type, uint16_t section, uint16_t item) {
  MenuItemEventPacket packet = {
    .packet.type = type,
    .packet.length = sizeof(packet),
    .section = section,
    .item = item,
  };
  return simply_msg_send_packet(&packet.packet);
}

static bool prv_send_menu_get_section(uint16_t index) {
  return prv_send_menu_item(CommandMenuGetSection, index, 0);
}

static bool prv_send_menu_get_item(uint16_t section, uint16_t index) {
  return prv_send_menu_item(CommandMenuGetItem, section, index);
}

static bool prv_send_menu_select_click(uint16_t section, uint16_t index) {
  return prv_send_menu_item(CommandMenuSelect, section, index);
}

static bool prv_send_menu_select_long_click(uint16_t section, uint16_t index) {
  return prv_send_menu_item(CommandMenuLongSelect, section, index);
}

static bool prv_section_filter(List1Node *node, void *data) {
  SimplyMenuCommon *section = (SimplyMenuCommon *)node;
  const uint16_t section_index = (uint16_t)(uintptr_t) data;
  return (section->section == section_index);
}

static bool prv_item_filter(List1Node *node, void *data) {
  SimplyMenuItem *item = (SimplyMenuItem *)node;
  const uint32_t cell_index = (uint32_t)(uintptr_t) data;
  const uint16_t section_index = cell_index;
  const uint16_t row = cell_index >> 16;
  return (item->section == section_index && item->item == row);
}

static bool prv_request_item_filter(List1Node *node, void *data) {
  return (((SimplyMenuItem *)node)->title == NULL);
}

static SimplyMenuSection *prv_get_menu_section(SimplyMenu *self, int index) {
  return (SimplyMenuSection*) list1_find(self->menu_layer.sections, prv_section_filter,
                                         (void*)(uintptr_t) index);
}

static void prv_free_title(char **title) {
  if (*title && *title != EMPTY_TITLE) {
    free(*title);
    *title = NULL;
  }
}

static void prv_destroy_section(SimplyMenu *self, SimplyMenuSection *section) {
  if (!section) { return; }
  list1_remove(&self->menu_layer.sections, &section->node);
  prv_free_title(&section->title);
  free(section);
}

static void prv_destroy_section_by_index(SimplyMenu *self, int section) {
  SimplyMenuSection *section_node =
      (SimplyMenuSection *)list1_find(self->menu_layer.sections, prv_section_filter,
                                      (void *)(uintptr_t)section);
  prv_destroy_section(self, section_node);
}

static SimplyMenuItem *prv_get_menu_item(SimplyMenu *self, int section, int index) {
  const uint32_t cell_index = section | (index << 16);
  return (SimplyMenuItem *) list1_find(self->menu_layer.items, prv_item_filter,
                                      (void *)(uintptr_t) cell_index);
}

static void prv_destroy_item(SimplyMenu *self, SimplyMenuItem *item) {
  if (!item) { return; }
  list1_remove(&self->menu_layer.items, &item->node);
  prv_free_title(&item->title);
  prv_free_title(&item->subtitle);
  free(item);
}

static void prv_destroy_item_by_index(SimplyMenu *self, int section, int index) {
  const uint32_t cell_index = section | (index << 16);
  SimplyMenuItem *item =
      (SimplyMenuItem *)list1_find(self->menu_layer.items, prv_item_filter,
                                   (void *)(uintptr_t) cell_index);
  prv_destroy_item(self, item);
}

static void prv_add_section(SimplyMenu *self, SimplyMenuSection *section) {
  if (list1_size(self->menu_layer.sections) >= MAX_CACHED_SECTIONS) {
    prv_destroy_section(self, (SimplyMenuSection *)list1_last(self->menu_layer.sections));
  }
  prv_destroy_section_by_index(self, section->section);
  list1_prepend(&self->menu_layer.sections, &section->node);
}

static void prv_add_item(SimplyMenu *self, SimplyMenuItem *item) {
  if (list1_size(self->menu_layer.items) >= MAX_CACHED_ITEMS) {
    prv_destroy_item(self, (SimplyMenuItem*) list1_last(self->menu_layer.items));
  }
  prv_destroy_item_by_index(self, item->section, item->item);
  list1_prepend(&self->menu_layer.items, &item->node);
}

static void prv_request_menu_section(SimplyMenu *self, uint16_t section_index) {
  SimplyMenuSection *section = prv_get_menu_section(self, section_index);
  if (section) { return; }
  section = malloc(sizeof(*section));
  *section = (SimplyMenuSection) {
    .section = section_index,
  };
  prv_add_section(self, section);
  prv_send_menu_get_section(section_index);
}

static void prv_request_menu_item(SimplyMenu *self, uint16_t section_index, uint16_t item_index) {
  SimplyMenuItem *item = prv_get_menu_item(self, section_index, item_index);
  if (item) { return; }
  item = malloc(sizeof(*item));
  *item = (SimplyMenuItem) {
    .section = section_index,
    .item = item_index,
  };
  prv_add_item(self, item);
  prv_send_menu_get_item(section_index, item_index);
}

static void prv_mark_dirty(SimplyMenu *self) {
  if (self->menu_layer.menu_layer) {
    layer_mark_dirty(menu_layer_get_layer(self->menu_layer.menu_layer));
  }
}

static void prv_reload_data(SimplyMenu *self) {
  if (self->menu_layer.menu_layer) {
    menu_layer_reload_data(self->menu_layer.menu_layer);
  }
}

static void simply_menu_set_num_sections(SimplyMenu *self, uint16_t num_sections) {
  if (num_sections == 0) {
    num_sections = 1;
  }
  self->menu_layer.num_sections = num_sections;
  prv_reload_data(self);
}

static void simply_menu_add_section(SimplyMenu *self, SimplyMenuSection *section) {
  if (section->title == NULL) {
    section->title = EMPTY_TITLE;
  }
  prv_add_section(self, section);
  prv_reload_data(self);
}

static void simply_menu_add_item(SimplyMenu *self, SimplyMenuItem *item) {
  if (item->title == NULL) {
    item->title = EMPTY_TITLE;
  }
  prv_add_item(self, item);
  prv_mark_dirty(self);
}

static MenuIndex simply_menu_get_selection(SimplyMenu *self) {
  if (!self->menu_layer.menu_layer) {
    return (MenuIndex) {};
  }
  return menu_layer_get_selected_index(self->menu_layer.menu_layer);
}

static void simply_menu_set_selection(SimplyMenu *self, MenuIndex menu_index, MenuRowAlign align,
                                      bool animated) {
  menu_layer_set_selected_index(self->menu_layer.menu_layer, menu_index, align, animated);
}

static bool prv_send_menu_selection(SimplyMenu *self) {
  MenuIndex menu_index = simply_menu_get_selection(self);
  return prv_send_menu_item(CommandMenuSelectionEvent, menu_index.section, menu_index.row);
}

static void spinner_timer_callback(void *data) {
  SimplyMenu *self = data;
  self->spinner_timer = NULL;
  prv_mark_dirty(self);
  refresh_spinner_timer(self);
}

static SimplyMenuItem *get_first_request_item(SimplyMenu *self) {
  return (SimplyMenuItem *)list1_find(self->menu_layer.items, prv_request_item_filter, NULL);
}

static SimplyMenuItem *get_last_request_item(SimplyMenu *self) {
  return (SimplyMenuItem *)list1_find_last(self->menu_layer.items, prv_request_item_filter, NULL);
}

static void refresh_spinner_timer(SimplyMenu *self) {
  if (!self->spinner_timer && get_first_request_item(self)) {
    self->spinner_timer = app_timer_register(SPINNER_MS, spinner_timer_callback, self);
  }
}

static uint16_t prv_menu_get_num_sections_callback(MenuLayer *menu_layer, void *data) {
  SimplyMenu *self = data;
  return self->menu_layer.num_sections;
}

static uint16_t prv_menu_get_num_rows_callback(MenuLayer *menu_layer, uint16_t section_index,
                                               void *data) {
  SimplyMenu *self = data;
  SimplyMenuSection *section = prv_get_menu_section(self, section_index);
  return section ? section->num_items : 1;
}

static int16_t prv_menu_get_header_height_callback(MenuLayer *menu_layer, uint16_t section_index,
                                                   void *data) {
  SimplyMenu *self = data;
  SimplyMenuSection *section = prv_get_menu_section(self, section_index);
  return (section && section->title &&
          section->title != EMPTY_TITLE ? MENU_CELL_BASIC_HEADER_HEIGHT : 0);
}

ROUND_USAGE static int16_t prv_menu_get_cell_height_callback(MenuLayer *menu_layer, MenuIndex *cell_index,
                                                             void *context) {
  if (PBL_IF_ROUND_ELSE(true, false)) {
    const bool is_selected = menu_layer_is_index_selected(menu_layer, cell_index);
    return is_selected ? MENU_CELL_ROUND_FOCUSED_TALL_CELL_HEIGHT :
                         MENU_CELL_ROUND_UNFOCUSED_SHORT_CELL_HEIGHT;
  } else {
    return MENU_CELL_BASIC_CELL_HEIGHT;
  }
}

static void prv_menu_draw_header_callback(GContext *ctx, const Layer *cell_layer,
                                          uint16_t section_index, void *data) {
  SimplyMenu *self = data;
  SimplyMenuSection *section = prv_get_menu_section(self, section_index);
  if (!section) {
    prv_request_menu_section(self, section_index);
    return;
  }

  list1_remove(&self->menu_layer.sections, &section->node);
  list1_prepend(&self->menu_layer.sections, &section->node);

  GRect bounds = layer_get_bounds(cell_layer);

  graphics_context_set_fill_color(ctx, gcolor8_get_or(section->title_background, GColorWhite));
  graphics_fill_rect(ctx, bounds, 0, GCornerNone);

  bounds.origin.x += 2;
  bounds.origin.y -= 1;

  graphics_context_set_text_color(ctx, gcolor8_get_or(section->title_foreground, GColorBlack));

  GTextAttributes *title_attributes = graphics_text_attributes_create();
  PBL_IF_ROUND_ELSE(
      graphics_text_attributes_enable_paging_on_layer(
          title_attributes, (Layer *)menu_layer_get_scroll_layer(self->menu_layer.menu_layer),
          &bounds, TEXT_FLOW_DEFAULT_INSET), NOOP);
  const GTextAlignment align = PBL_IF_ROUND_ELSE(GTextAlignmentCenter, GTextAlignmentLeft);
  graphics_draw_text(ctx, section->title, fonts_get_system_font(FONT_KEY_GOTHIC_14_BOLD),
                     bounds, GTextOverflowModeTrailingEllipsis, align, title_attributes);
  graphics_text_attributes_destroy(title_attributes);
}

static void simply_menu_draw_row_spinner(SimplyMenu *self, GContext *ctx,
                                         const Layer *cell_layer) {
  GRect bounds = layer_get_bounds(cell_layer);
  GPoint center = grect_center_point(&bounds);

  const int16_t min_radius = 4 * bounds.size.h / 24;
  const int16_t max_radius = 9 * bounds.size.h / 24;
  const int16_t num_lines = 16;
  const int16_t num_drawn_lines = 3;

  const int64_t now_ms = prv_get_milliseconds();
  const uint32_t start_index = (now_ms / SPINNER_MS) % num_lines;

  graphics_context_set_antialiased(ctx, true);

  GColor8 stroke_color =
      menu_cell_layer_is_highlighted(cell_layer) ? self->menu_layer.highlight_foreground :
                                                   self->menu_layer.normal_foreground;
  graphics_context_set_stroke_color(ctx, gcolor8_get_or(stroke_color, GColorBlack));

  for (int16_t i = 0; i < num_drawn_lines; i++) {
    const uint32_t angle = (i + start_index) * TRIG_MAX_ANGLE / num_lines;
    GPoint a = gpoint_add(center, gpoint_polar(angle, min_radius));
    GPoint b = gpoint_add(center, gpoint_polar(angle, max_radius));
    graphics_draw_line(ctx, a, b);
  }
}

#if defined(PBL_COLOR)
static uint8_t prv_menu_mix_u8(uint8_t a, uint8_t b, uint16_t t, uint16_t max_t) {
  return a + (((int16_t)(b - a) * t) / max_t);
}

static uint16_t prv_menu_row_gradient_size(uint16_t num_rows) {
  if (num_rows < 3) {
    return 3;
  }
  if (num_rows > MENU_ROW_GRADIENT_STEPS) {
    return MENU_ROW_GRADIENT_STEPS;
  }
  return num_rows;
}

static GColor prv_menu_row_fill_color(uint16_t row, uint16_t num_rows) {
  const uint16_t gradient_size = prv_menu_row_gradient_size(num_rows);
  const uint16_t max_step = gradient_size - 1;
  const uint16_t step = row >= max_step ? max_step : row;
  return GColorFromRGB(
      prv_menu_mix_u8(41, 0, step, max_step),
      prv_menu_mix_u8(19, 0, step, max_step),
      prv_menu_mix_u8(129, 0, step, max_step));
}

static GColor prv_menu_highlight_fill_color(void) {
  return GColorFromRGB(0, 170, 255);
}

static void prv_menu_draw_background_callback(GContext *ctx, const Layer *bg_layer,
                                              bool highlight, void *data) {
  SimplyMenu *self = data;
  GRect bounds = layer_get_bounds(bg_layer);

  if (highlight) {
    graphics_context_set_fill_color(ctx, prv_menu_highlight_fill_color());
    graphics_fill_rect(ctx, bounds, 0, GCornerNone);
    return;
  }

  graphics_context_set_fill_color(ctx, GColorBlack);
  graphics_fill_rect(ctx, bounds, 0, GCornerNone);

  if (!self->menu_layer.menu_layer || !PBL_IF_RECT_ELSE(true, false)) {
    return;
  }

  SimplyMenuSection *section = prv_get_menu_section(self, 0);
  if (!section || !section->num_items) {
    return;
  }

  const int16_t row_height = MENU_CELL_BASIC_CELL_HEIGHT;
  const int16_t top_content_y = -scroll_layer_get_content_offset(
      menu_layer_get_scroll_layer(self->menu_layer.menu_layer)).y;

  for (uint16_t row = 0; row < section->num_items; ++row) {
    const int16_t y = row * row_height - top_content_y;
    if (y >= bounds.size.h) {
      break;
    }
    if (y + row_height <= 0) {
      continue;
    }

    graphics_context_set_fill_color(ctx, prv_menu_row_fill_color(row, section->num_items));
    graphics_fill_rect(ctx,
                       GRect(0, y, bounds.size.w, row_height),
                       0, GCornerNone);
  }
}

static void prv_menu_draw_row_chrome(GContext *ctx, const Layer *cell_layer,
                                     MenuIndex *cell_index, SimplyMenu *self,
                                     SimplyMenuSection *section) {
  GRect bounds = layer_get_bounds(cell_layer);
  const bool is_highlighted = menu_cell_layer_is_highlighted(cell_layer);

  if (is_highlighted) {
    graphics_context_set_fill_color(ctx, gcolor8_get_or(self->menu_layer.highlight_background,
                                                        prv_menu_highlight_fill_color()));
    graphics_fill_rect(ctx, bounds, 0, GCornerNone);
    return;
  }
}

static void prv_menu_draw_row_text(GContext *ctx, const Layer *cell_layer,
                                   SimplyMenu *self, SimplyMenuItem *item) {
  GRect bounds = layer_get_bounds(cell_layer);
  const bool is_highlighted = menu_cell_layer_is_highlighted(cell_layer);
  const GColor text_color =
      gcolor8_get_or(is_highlighted ? self->menu_layer.highlight_foreground :
                                      self->menu_layer.normal_foreground,
                     GColorWhite);
  const GFont title_font = fonts_get_system_font(item->subtitle ? FONT_KEY_GOTHIC_24_BOLD :
                                                                 FONT_KEY_GOTHIC_28_BOLD);
  const GRect title_rect = item->subtitle ? GRect(10, 0, bounds.size.w - 20, 24) :
                                            GRect(10, 7, bounds.size.w - 20, 30);

  graphics_context_set_text_color(ctx, text_color);
  graphics_draw_text(ctx, item->title, title_font, title_rect,
                     GTextOverflowModeTrailingEllipsis, GTextAlignmentLeft, NULL);

  if (!item->subtitle) {
    return;
  }

  graphics_draw_text(ctx, item->subtitle, fonts_get_system_font(FONT_KEY_GOTHIC_14),
                     GRect(10, 23, bounds.size.w - 20, 18),
                     GTextOverflowModeTrailingEllipsis, GTextAlignmentLeft, NULL);
}
#endif

static void prv_menu_draw_row_callback(GContext *ctx, const Layer *cell_layer,
                                       MenuIndex *cell_index, void *data) {
  SimplyMenu *self = data;
  SimplyMenuSection *section = prv_get_menu_section(self, cell_index->section);
  if (!section) {
    prv_request_menu_section(self, cell_index->section);
    return;
  }

  SimplyMenuItem *item = prv_get_menu_item(self, cell_index->section, cell_index->row);
  if (!item) {
    prv_request_menu_item(self, cell_index->section, cell_index->row);
    return;
  }

#if defined(PBL_COLOR)
  prv_menu_draw_row_chrome(ctx, cell_layer, cell_index, self, section);
#endif

  if (item->title == NULL) {
    SimplyMenuItem *last_request = get_last_request_item(self);
    if (last_request == item) {
      simply_menu_draw_row_spinner(self, ctx, cell_layer);
      refresh_spinner_timer(self);
    }
    return;
  }

  list1_remove(&self->menu_layer.items, &item->node);
  list1_prepend(&self->menu_layer.items, &item->node);

#if defined(PBL_COLOR)
  prv_menu_draw_row_text(ctx, cell_layer, self, item);
#else
  SimplyImage *image = simply_res_get_image(self->window.simply->res, item->icon);
  GColor8 *palette = NULL;

  if (image && image->is_palette_black_and_white) {
    palette = gbitmap_get_palette(image->bitmap);
    const bool is_highlighted = menu_cell_layer_is_highlighted(cell_layer);
    gbitmap_set_palette(image->bitmap, is_highlighted ? s_inverted_palette : s_normal_palette,
                        false);
  }

  graphics_context_set_alpha_blended(ctx, true);
  menu_cell_basic_draw(ctx, cell_layer, item->title, item->subtitle, image ? image->bitmap : NULL);

  if (palette) {
    gbitmap_set_palette(image->bitmap, palette, false);
  }
#endif
}

static void prv_menu_select_click_callback(MenuLayer *menu_layer, MenuIndex *cell_index,
                                           void *data) {
  prv_send_menu_select_click(cell_index->section, cell_index->row);
}

static void prv_menu_select_long_click_callback(MenuLayer *menu_layer, MenuIndex *cell_index,
                                                void *data) {
  prv_send_menu_select_long_click(cell_index->section, cell_index->row);
}

static void prv_single_click_handler(ClickRecognizerRef recognizer, void *context) {
  Window *base_window = layer_get_window(context);
  SimplyWindow *window = window_get_user_data(base_window);
  simply_window_single_click_handler(recognizer, window);
}

static void prv_click_config_provider(void *context) {
  window_single_click_subscribe(BUTTON_ID_BACK, prv_single_click_handler);
  menu_layer_click_config(context);
}

static void prv_menu_window_load(Window *window) {
  SimplyMenu *self = window_get_user_data(window);

  simply_window_load(&self->window);

  Layer *window_layer = window_get_root_layer(window);
  GRect frame = layer_get_frame(window_layer);
  frame.origin = GPointZero;

  MenuLayer *menu_layer = self->menu_layer.menu_layer = menu_layer_create(frame);
  Layer *menu_base_layer = menu_layer_get_layer(menu_layer);
  self->window.layer = menu_base_layer;
  layer_add_child(window_layer, menu_base_layer);

  menu_layer_set_callbacks(menu_layer, self, (MenuLayerCallbacks){
    .get_num_sections = prv_menu_get_num_sections_callback,
    .get_num_rows = prv_menu_get_num_rows_callback,
    .get_header_height = prv_menu_get_header_height_callback,
#if defined(PBL_ROUND)
    .get_cell_height = prv_menu_get_cell_height_callback,
#endif
    .draw_header = prv_menu_draw_header_callback,
    .draw_row = prv_menu_draw_row_callback,
    .select_click = prv_menu_select_click_callback,
    .select_long_click = prv_menu_select_long_click_callback,
#if defined(PBL_COLOR)
    .draw_background = prv_menu_draw_background_callback,
#endif
  });

  menu_layer_set_click_config_provider_onto_window(menu_layer, prv_click_config_provider, window);
}

static void prv_menu_window_appear(Window *window) {
  SimplyMenu *self = window_get_user_data(window);
  simply_window_appear(&self->window);
}

static void prv_menu_window_disappear(Window *window) {
  SimplyMenu *self = window_get_user_data(window);
  if (simply_window_disappear(&self->window)) {
    simply_res_clear(self->window.simply->res);
    simply_menu_clear(self);
  }
}

static void prv_menu_window_unload(Window *window) {
  SimplyMenu *self = window_get_user_data(window);

  menu_layer_destroy(self->menu_layer.menu_layer);
  self->menu_layer.menu_layer = NULL;

  simply_window_unload(&self->window);
}

static void simply_menu_clear_section_items(SimplyMenu *self, int section_index) {
  SimplyMenuItem *item = NULL;
  do {
    item = (SimplyMenuItem *)list1_find(self->menu_layer.items, prv_section_filter,
                                        (void *)(uintptr_t) section_index);
    prv_destroy_item(self, item);
  } while (item);
}

static void simply_menu_clear(SimplyMenu *self) {
  while (self->menu_layer.sections) {
    prv_destroy_section(self, (SimplyMenuSection *)self->menu_layer.sections);
  }

  while (self->menu_layer.items) {
    prv_destroy_item(self, (SimplyMenuItem *)self->menu_layer.items);
  }

  prv_reload_data(self);
}

static void prv_handle_menu_clear_packet(Simply *simply, Packet *data) {
  simply_menu_clear(simply->menu);
}

static void prv_handle_menu_clear_section_packet(Simply *simply, Packet *data) {
  MenuClearSectionPacket *packet = (MenuClearSectionPacket *)data;
  simply_menu_clear_section_items(simply->menu, packet->section);
}

static void prv_handle_menu_props_packet(Simply *simply, Packet *data) {
  MenuPropsPacket *packet = (MenuPropsPacket *)data;
  SimplyMenu *self = simply->menu;

  simply_menu_set_num_sections(self, packet->num_sections);

  if (!self->window.window) { return; }

  window_set_background_color(self->window.window, gcolor8_get_or(packet->background_color,
                                                                  GColorBlack));

  SimplyMenuLayer *menu_layer = &self->menu_layer;
  if (!menu_layer->menu_layer) { return; }

  menu_layer->normal_background = packet->background_color;
  menu_layer->normal_foreground = packet->text_color;
  menu_layer->highlight_background = packet->highlight_background_color;
  menu_layer->highlight_foreground = packet->highlight_text_color;

#if defined(PBL_COLOR)
  menu_layer_set_normal_colors(menu_layer->menu_layer,
                               GColorClear,
                               gcolor8_get_or(menu_layer->normal_foreground, GColorBlack));
  menu_layer_set_highlight_colors(menu_layer->menu_layer,
                                  GColorClear,
                                  gcolor8_get_or(menu_layer->highlight_foreground, GColorWhite));
#else
  menu_layer_set_normal_colors(menu_layer->menu_layer,
                               gcolor8_get_or(menu_layer->normal_background, GColorBlack),
                               gcolor8_get_or(menu_layer->normal_foreground, GColorBlack));
  menu_layer_set_highlight_colors(menu_layer->menu_layer,
                                  gcolor8_get_or(menu_layer->highlight_background, GColorBlack),
                                  gcolor8_get_or(menu_layer->highlight_foreground, GColorWhite));
#endif
}

static void prv_handle_menu_section_packet(Simply *simply, Packet *data) {
  MenuSectionPacket *packet = (MenuSectionPacket *)data;
  SimplyMenuSection *section = malloc(sizeof(*section));
  *section = (SimplyMenuSection) {
    .section = packet->section,
    .num_items = packet->num_items,
    .title_foreground = packet->text_color,
    .title_background = packet->background_color,
    .title = packet->title_length ? strdup2(packet->title) : NULL,
  };
  simply_menu_add_section(simply->menu, section);
}

static void prv_handle_menu_item_packet(Simply *simply, Packet *data) {
  MenuItemPacket *packet = (MenuItemPacket *)data;
  SimplyMenuItem *item = malloc(sizeof(*item));
  *item = (SimplyMenuItem) {
    .section = packet->section,
    .item = packet->item,
    .title = packet->title_length ? strdup2(packet->buffer) : NULL,
    .subtitle = packet->subtitle_length ? strdup2(packet->buffer + packet->title_length + 1) : NULL,
    .icon = packet->icon,
  };
  simply_menu_add_item(simply->menu, item);
}

static void prv_handle_menu_get_selection_packet(Simply *simply, Packet *data) {
  prv_send_menu_selection(simply->menu);
}

static void prv_handle_menu_selection_packet(Simply *simply, Packet *data) {
  MenuSelectionPacket *packet = (MenuSelectionPacket *)data;
  MenuIndex menu_index = {
    .section = packet->section,
    .row = packet->item,
  };
  simply_menu_set_selection(simply->menu, menu_index, packet->align, packet->animated);
}

bool simply_menu_handle_packet(Simply *simply, Packet *packet) {
  switch (packet->type) {
    case CommandMenuClear:
      prv_handle_menu_clear_packet(simply, packet);
      return true;
    case CommandMenuClearSection:
      prv_handle_menu_clear_section_packet(simply, packet);
      return true;
    case CommandMenuProps:
      prv_handle_menu_props_packet(simply, packet);
      return true;
    case CommandMenuSection:
      prv_handle_menu_section_packet(simply, packet);
      return true;
    case CommandMenuItem:
      prv_handle_menu_item_packet(simply, packet);
      return true;
    case CommandMenuSelection:
      prv_handle_menu_selection_packet(simply, packet);
      return true;
    case CommandMenuGetSelection:
      prv_handle_menu_get_selection_packet(simply, packet);
      return true;
  }
  return false;
}

SimplyMenu *simply_menu_create(Simply *simply) {
  SimplyMenu *self = malloc(sizeof(*self));
  *self = (SimplyMenu) {
    .window.simply = simply,
#if defined(PBL_ROUND)
    .window.status_bar_insets_bottom = true,
#endif
    .menu_layer.num_sections = 1,
  };

  static const WindowHandlers s_window_handlers = {
    .load = prv_menu_window_load,
    .appear = prv_menu_window_appear,
    .disappear = prv_menu_window_disappear,
    .unload = prv_menu_window_unload,
  };
  self->window.window_handlers = &s_window_handlers;

  simply_window_init(&self->window, simply);
  simply_window_set_background_color(&self->window, prv_menu_background_color());

  return self;
}

void simply_menu_destroy(SimplyMenu *self) {
  if (!self) {
    return;
  }

  simply_window_deinit(&self->window);

  free(self);
}
