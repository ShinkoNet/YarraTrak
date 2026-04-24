#include "query_window.h"
#include "theme.h"
#include "../protocol.h"
#include "../haptics.h"

#include <pebble.h>
#include <string.h>
#include <stdlib.h>

#if defined(PBL_MICROPHONE)

#define MAX_CHOICES 8
#define CHOICE_LABEL_LEN 32
#define CHOICE_VALUE_LEN 48
#define BODY_TEXT_LEN 512

static Window *s_card_window = NULL;
static TextLayer *s_title_layer = NULL;
static TextLayer *s_body_layer = NULL;
static ScrollLayer *s_scroll_layer = NULL;
static DictationSession *s_dict = NULL;

static char s_title_buf[24];
static char s_body_buf[BODY_TEXT_LEN];

// Clarification state. A clarification response can be pushed over the card.
static Window *s_choices_window = NULL;
static MenuLayer *s_choices_menu = NULL;
static char s_choices_question[64];
static char s_choices_labels[MAX_CHOICES][CHOICE_LABEL_LEN];
static char s_choices_values[MAX_CHOICES][CHOICE_VALUE_LEN];
static uint8_t s_choice_count = 0;

static void card_window_load(Window *window);
static void card_window_unload(Window *window);
static void set_card_text(const char *title, const char *body);

// ---- Card window --------------------------------------------------------

static void set_card_text(const char *title, const char *body) {
  if (!s_card_window) return;
  strncpy(s_title_buf, title ? title : "", sizeof(s_title_buf) - 1);
  s_title_buf[sizeof(s_title_buf) - 1] = '\0';
  strncpy(s_body_buf, body ? body : "", sizeof(s_body_buf) - 1);
  s_body_buf[sizeof(s_body_buf) - 1] = '\0';
  if (s_title_layer) text_layer_set_text(s_title_layer, s_title_buf);
  if (s_body_layer) {
    Layer *body = text_layer_get_layer(s_body_layer);
    GRect frame = layer_get_frame(body);
    // Expand to a tall measurement frame BEFORE setting the new text, then
    // measure. Without this, a previous short message leaves the frame at
    // e.g. height=40, which caps the next measurement and clips long text
    // like the RESULT tts_text after "Platform 2,".
    text_layer_set_size(s_body_layer, GSize(frame.size.w, 1200));
    text_layer_set_text(s_body_layer, s_body_buf);
    GSize content = text_layer_get_content_size(s_body_layer);
    int16_t height = content.h > 0 ? content.h + 8 : 40;
    text_layer_set_size(s_body_layer, GSize(frame.size.w, height));
    if (s_scroll_layer) {
      scroll_layer_set_content_size(s_scroll_layer,
          GSize(frame.size.w, height));
    }
  }
}

static void card_window_load(Window *window) {
  Layer *root = window_get_root_layer(window);
  GRect bounds = layer_get_bounds(root);
  window_set_background_color(window, theme_bg());

  s_title_layer = text_layer_create(GRect(4, 4, bounds.size.w - 8, 22));
  text_layer_set_font(s_title_layer, fonts_get_system_font(FONT_KEY_GOTHIC_18_BOLD));
  text_layer_set_text_color(s_title_layer, theme_fg());
  text_layer_set_background_color(s_title_layer, GColorClear);
  text_layer_set_text_alignment(s_title_layer, GTextAlignmentCenter);
  layer_add_child(root, text_layer_get_layer(s_title_layer));

  GRect scroll_frame = GRect(0, 28, bounds.size.w, bounds.size.h - 28);
  s_scroll_layer = scroll_layer_create(scroll_frame);
  scroll_layer_set_click_config_onto_window(s_scroll_layer, window);
  scroll_layer_set_shadow_hidden(s_scroll_layer, true);
  layer_add_child(root, scroll_layer_get_layer(s_scroll_layer));

  s_body_layer = text_layer_create(GRect(6, 0, bounds.size.w - 12, 800));
  text_layer_set_font(s_body_layer, fonts_get_system_font(FONT_KEY_GOTHIC_18));
  text_layer_set_text_color(s_body_layer, theme_fg());
  text_layer_set_background_color(s_body_layer, GColorClear);
  text_layer_set_text_alignment(s_body_layer, GTextAlignmentLeft);
  text_layer_set_overflow_mode(s_body_layer, GTextOverflowModeWordWrap);
  scroll_layer_add_child(s_scroll_layer, text_layer_get_layer(s_body_layer));

  if (s_title_buf[0] || s_body_buf[0]) {
    set_card_text(s_title_buf, s_body_buf);
  } else {
    set_card_text("Listening...", "Say where you're going.");
  }
}

static void card_window_unload(Window *window) {
  if (s_body_layer)  { text_layer_destroy(s_body_layer);  s_body_layer  = NULL; }
  if (s_scroll_layer){ scroll_layer_destroy(s_scroll_layer); s_scroll_layer = NULL; }
  if (s_title_layer) { text_layer_destroy(s_title_layer); s_title_layer = NULL; }
  window_destroy(s_card_window);
  s_card_window = NULL;
  if (s_dict) {
    dictation_session_destroy(s_dict);
    s_dict = NULL;
  }
}

static void ensure_card_window(void) {
  if (s_card_window) return;
  s_card_window = window_create();
  window_set_window_handlers(s_card_window, (WindowHandlers){
    .load = card_window_load,
    .unload = card_window_unload,
  });
  window_stack_push(s_card_window, true);
}

// ---- Clarification menu -------------------------------------------------

static uint16_t choices_num_rows(MenuLayer *menu_layer, uint16_t section_index, void *context) {
  return s_choice_count;
}

static void choices_draw_row(GContext *ctx, const Layer *cell_layer, MenuIndex *cell_index, void *context) {
  GRect bounds = layer_get_bounds(cell_layer);
  bool highlighted = menu_cell_layer_is_highlighted(cell_layer);
  graphics_context_set_text_color(ctx,
      highlighted ? PBL_IF_COLOR_ELSE(GColorWhite, theme_bg()) : theme_fg());
  if (cell_index->row < s_choice_count) {
    graphics_draw_text(ctx, s_choices_labels[cell_index->row],
                       fonts_get_system_font(FONT_KEY_GOTHIC_18),
                       GRect(6, 0, bounds.size.w - 12, bounds.size.h),
                       GTextOverflowModeTrailingEllipsis,
                       GTextAlignmentLeft, NULL);
  }
}

static int16_t choices_cell_height(MenuLayer *menu_layer, MenuIndex *cell_index, void *context) {
  return 34;
}

static void choices_select(MenuLayer *menu_layer, MenuIndex *cell_index, void *context) {
  if (cell_index->row >= s_choice_count) return;
  const char *value = s_choices_values[cell_index->row];
  // Fire the chosen value back to the server as a follow-up query.
  protocol_send_query(value);
  // Pop the clarification menu — the card window underneath is still there
  // and will be updated by the server's next response.
  if (s_choices_window) {
    window_stack_remove(s_choices_window, true);
  }
  set_card_text("Processing...", "");
}

static void choices_window_load(Window *window) {
  Layer *root = window_get_root_layer(window);
  GRect bounds = layer_get_bounds(root);
  window_set_background_color(window, theme_bg());

  s_choices_menu = menu_layer_create(bounds);
  menu_layer_set_callbacks(s_choices_menu, NULL, (MenuLayerCallbacks){
    .get_num_rows = choices_num_rows,
    .draw_row = choices_draw_row,
    .get_cell_height = choices_cell_height,
    .select_click = choices_select,
  });
  menu_layer_set_click_config_onto_window(s_choices_menu, window);
  menu_layer_set_normal_colors(s_choices_menu, theme_bg(), theme_fg());
  menu_layer_set_highlight_colors(s_choices_menu, theme_accent(),
                                  PBL_IF_COLOR_ELSE(GColorWhite, theme_bg()));
  layer_add_child(root, menu_layer_get_layer(s_choices_menu));
}

static void choices_window_unload(Window *window) {
  if (s_choices_menu) { menu_layer_destroy(s_choices_menu); s_choices_menu = NULL; }
  window_destroy(s_choices_window);
  s_choices_window = NULL;
}

static void push_choices_window(void) {
  if (s_choices_window) {
    window_stack_remove(s_choices_window, false);
  }
  s_choices_window = window_create();
  window_set_window_handlers(s_choices_window, (WindowHandlers){
    .load = choices_window_load,
    .unload = choices_window_unload,
  });
  window_stack_push(s_choices_window, true);
}

// ---- Dictation ---------------------------------------------------------

static void dict_status_cb(DictationSession *session, DictationSessionStatus status,
                           char *transcription, void *context) {
  if (status == DictationSessionStatusSuccess && transcription) {
    char body[BODY_TEXT_LEN];
    snprintf(body, sizeof(body), "You: %s", transcription);
    set_card_text("Processing...", body);
    protocol_send_query(transcription);
  } else if (status == DictationSessionStatusFailureSystemAborted) {
    // User bailed out; silently close the card.
    if (s_card_window) window_stack_remove(s_card_window, true);
  } else {
    set_card_text("Error", "Dictation failed.");
  }
}

// ---- Public API --------------------------------------------------------

void query_window_start(void) {
  s_title_buf[0] = '\0';
  s_body_buf[0] = '\0';
  ensure_card_window();

  if (!s_dict) {
    s_dict = dictation_session_create(BODY_TEXT_LEN, dict_status_cb, NULL);
  }
  if (s_dict) {
    dictation_session_start(s_dict);
  } else {
    set_card_text("Error", "Voice unavailable on this platform.");
  }
}

void query_window_show_result(const char *tts_text) {
  if (!s_card_window) return;
  set_card_text("Departures", tts_text && tts_text[0] ? tts_text : "No info");
  haptics_short();
}

// Payload: "question\x1elabel1\x1fvalue1\x1elabel2\x1fvalue2..."
// Parsed in-place: NULs are written into the buffer, so callers must own a
// mutable copy.
void query_window_show_clarification(char *mutable_payload) {
  if (!s_card_window) return;
  if (!mutable_payload) {
    query_window_show_error("Empty clarification");
    return;
  }

  s_choice_count = 0;
  char *cursor = mutable_payload;
  char *rs = strchr(cursor, 0x1e);
  if (rs) {
    *rs = '\0';
    strncpy(s_choices_question, cursor, sizeof(s_choices_question) - 1);
    s_choices_question[sizeof(s_choices_question) - 1] = '\0';
    cursor = rs + 1;
  } else {
    strncpy(s_choices_question, cursor, sizeof(s_choices_question) - 1);
    s_choices_question[sizeof(s_choices_question) - 1] = '\0';
    cursor = NULL;
  }

  while (cursor && s_choice_count < MAX_CHOICES) {
    char *next = strchr(cursor, 0x1e);
    if (next) *next = '\0';
    char *split = strchr(cursor, 0x1f);
    if (split) {
      *split = '\0';
      strncpy(s_choices_labels[s_choice_count], cursor, CHOICE_LABEL_LEN - 1);
      s_choices_labels[s_choice_count][CHOICE_LABEL_LEN - 1] = '\0';
      strncpy(s_choices_values[s_choice_count], split + 1, CHOICE_VALUE_LEN - 1);
      s_choices_values[s_choice_count][CHOICE_VALUE_LEN - 1] = '\0';
      s_choice_count++;
    }
    if (!next) break;
    cursor = next + 1;
  }

  set_card_text("Choose", s_choices_question);

  if (s_choice_count > 0) {
    push_choices_window();
  }
}

void query_window_show_error(const char *message) {
  if (!s_card_window) return;
  set_card_text("Error", message && message[0] ? message : "Query failed");
}

bool query_window_is_open(void) {
  return s_card_window != NULL;
}

#else  // !PBL_MICROPHONE — aplite has no mic. Keep the symbols as no-ops so
       // protocol.c and menu_window.c don't need platform-specific builds,
       // and the ~1KB of static clarification state stays out of the aplite
       // binary.

void query_window_start(void) { (void)0; }
void query_window_show_result(const char *t)        { (void)t; }
void query_window_show_clarification(char *p)       { (void)p; }
void query_window_show_error(const char *m)         { (void)m; }
bool query_window_is_open(void) { return false; }

#endif
