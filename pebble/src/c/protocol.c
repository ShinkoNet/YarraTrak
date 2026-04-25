#include "protocol.h"
#include "app_state.h"
#include "settings_store.h"
#include "ui/menu_window.h"
#include "ui/watch_window.h"
#include "ui/query_window.h"
#include "ui/splash_window.h"
#include "haptics.h"

#include <pebble.h>
#include <string.h>
#include <stdlib.h>
#include <limits.h>

// Split `buf` in-place on `sep` into up to `max_parts` pointers.
// Returns actual count. Does not allocate.
static int split_in_place(char *buf, char sep, char **parts, int max_parts) {
  int count = 0;
  char *start = buf;
  char *p = buf;
  while (count < max_parts) {
    if (*p == '\0') {
      parts[count++] = start;
      break;
    }
    if (*p == sep) {
      *p = '\0';
      parts[count++] = start;
      start = p + 1;
    }
    p++;
  }
  return count;
}

static void copy_bounded(char *dst, const char *src, size_t dst_size) {
  if (!src) {
    dst[0] = '\0';
    return;
  }
  strncpy(dst, src, dst_size - 1);
  dst[dst_size - 1] = '\0';
}

// ---- Menu-refresh debounce ----------------------------------------------
//
// PKJS's syncEntriesToWatch sends IN_CLEAR_ENTRIES immediately followed by
// IN_ENTRY_SYNC_BULK. AppMessage roundtrips are ~150-300 ms each, so if the
// clear refreshes the menu immediately the user sees "No favourites" flash
// between their real rows. Defer the post-clear refresh; cancel it as soon
// as a real sync arrives. Only fires if no sync shows up within the window
// (the genuine "user has zero favourites" case).
#define PENDING_CLEAR_REFRESH_MS 500
static AppTimer *s_clear_refresh_timer = NULL;

static void clear_refresh_cb(void *unused) {
  s_clear_refresh_timer = NULL;
  menu_window_refresh();
}

static void cancel_pending_clear_refresh(void) {
  if (s_clear_refresh_timer) {
    app_timer_cancel(s_clear_refresh_timer);
    s_clear_refresh_timer = NULL;
  }
}

static void schedule_clear_refresh(void) {
  cancel_pending_clear_refresh();
  s_clear_refresh_timer = app_timer_register(PENDING_CLEAR_REFRESH_MS,
                                              clear_refresh_cb, NULL);
}

// ---- Inbound handlers ----------------------------------------------------

static void handle_conn_state(char *data) {
  // Payload: "<state>" or "<state>|<diagnostic message>"
  char *msg = NULL;
  for (char *p = data; *p; p++) {
    if (*p == '|') { *p = '\0'; msg = p + 1; break; }
  }
  int v = atoi(data);
  if (v < 0) v = 0;
  if (v > 2) v = 2;
  g_app_state.conn_state = (uint8_t)v;
  splash_window_set_status(msg && msg[0] ? msg : NULL);
  menu_window_refresh();
  watch_window_refresh();
}

// FLAGS_SYNC format: "flag_bits[|bg_fx]"
// Optional 2nd pipe-delimited token is the background fx enum (0=rings,
// 1=starfield, 2=plasma, 3=fire, 4=cube). Old clients without the field
// default to rings.
//
// bit layout (post-reorg):
//   1  disable_vibration
//   2  disable_animations     (was disable_ripple_vfx — now also covers shake)
//   4  disable_distance_info  (was disable_timer_shake — repurposed)
//   8  disable_ai_assistant
//   16 use_24hr_time
//   32 dark_theme
static void handle_flags_sync(char *data) {
  char *bg_token = NULL;
  for (char *p = data; *p; p++) {
    if (*p == '|') { *p = '\0'; bg_token = p + 1; break; }
  }
  int bits = atoi(data);
  Flags *f = &g_app_state.flags;
  f->disable_vibration      = (bits & 1)  != 0;
  f->disable_animations     = (bits & 2)  != 0;
  f->disable_distance_info  = (bits & 4)  != 0;
  f->disable_ai_assistant   = (bits & 8)  != 0;
  f->use_24hr_time          = (bits & 16) != 0;
  f->dark_theme             = (bits & 32) != 0;
  f->bg_fx = 0;
  if (bg_token && *bg_token) {
    int v = atoi(bg_token);
    if (v >= 0 && v <= 4) f->bg_fx = (uint8_t)v;
  }
  settings_store_save_flags();
  g_app_state.settings_received = true;
  // The watch face caches theme colours in TextLayers and keeps a long-
  // running fx animation timer; live-swapping the theme or background
  // effect from underneath it leaves half-rendered chrome on screen.
  // Pop back to the menu so the next push starts fresh.
  if (watch_window_is_open()) {
    watch_window_close();
  }
}

// ENTRY_SYNC format: "index|name;stop_id;dest_name;route_type;direction_id"
// Only the fields the watch actually uses. full_name / full_dest_name /
// dest_id remain in PKJS localStorage for the config page.
static void handle_entry_sync(char *data) {
  char *top_parts[2];
  int top_count = split_in_place(data, '|', top_parts, 2);
  if (top_count < 2) return;

  int idx = atoi(top_parts[0]);
  if (idx < 1 || idx > MAX_ENTRIES) return;

  char *fields[5];
  int fc = split_in_place(top_parts[1], ';', fields, 5);
  if (fc < 2) return;

  Entry *e = &g_app_state.entries[idx - 1];
  memset(e, 0, sizeof(*e));
  e->configured = true;

  copy_bounded(e->name,      fc > 0 ? fields[0] : "", sizeof(e->name));
  e->stop_id      = fc > 1 ? atoi(fields[1]) : 0;
  copy_bounded(e->dest_name, fc > 2 ? fields[2] : "", sizeof(e->dest_name));
  e->route_type   = fc > 3 ? (uint8_t)atoi(fields[3]) : 0;
  e->direction_id = fc > 4 ? atoi(fields[4]) : 0;

  if (idx > g_app_state.entry_count) {
    g_app_state.entry_count = (uint8_t)idx;
  }
}

// FAV_UPDATE format:
//   "button_id|dep1|dep2|disruption_labels"
// dep = "minutes;departure_unix;route_type;direction_id;run_ref;platform;route_id"
// disruption_labels = "label1\x1flabel2..."  (using ';' as separator since \x1f is awkward)
//
// route_type / direction_id / route_id are invariant per favourite — they're
// stored on Entry. The wire format still ships them per-departure for back
// compat with PKJS; route_id is harvested into the parent Entry, the others
// are discarded since ENTRY_SYNC has already populated them.
static void parse_departure(char *blob, Departure *out, char *route_id_out, size_t route_id_size) {
  memset(out, 0, sizeof(*out));
  if (!blob || !blob[0]) {
    out->has_data = false;
    return;
  }

  char *f[7];
  int fc = split_in_place(blob, ';', f, 7);
  if (fc == 0) {
    out->has_data = false;
    return;
  }

  out->minutes         = fc > 0 && f[0][0] ? atoi(f[0]) : -1;
  out->departure_unix  = fc > 1 && f[1][0] ? (time_t)atol(f[1]) : 0;
  // f[2] (route_type) and f[3] (direction_id) ignored — Entry already has them.
  copy_bounded(out->run_ref,  fc > 4 ? f[4] : "", sizeof(out->run_ref));
  copy_bounded(out->platform, fc > 5 ? f[5] : "", sizeof(out->platform));

  if (route_id_out && route_id_size && fc > 6 && f[6][0]) {
    copy_bounded(route_id_out, f[6], route_id_size);
  }

  out->has_data = (out->minutes >= 0) || (out->departure_unix != 0);
}

static void handle_fav_update(char *data) {
  // New format (5 parts): "button_id|dep1|dep2|dep3|labels"
  // Old format (4 parts): "button_id|dep1|dep2|labels" — still accepted so a
  // client running this build against a PKJS that only sends two deps
  // doesn't lose data.
  char *parts[5];
  int pc = split_in_place(data, '|', parts, 5);
  if (pc < 1) return;

  int button_id = atoi(parts[0]);
  Entry *e = app_state_get_entry((uint8_t)button_id);
  if (!e) return;

  for (uint8_t i = 0; i < MAX_DEPS_PER_ENTRY; i++) {
    memset(&e->departures[i], 0, sizeof(Departure));
  }
  e->disruption_count = 0;

  int dep_count, labels_idx;
  if (pc >= 5)      { dep_count = 3; labels_idx = 4; }
  else if (pc >= 4) { dep_count = 2; labels_idx = 3; }
  else              { dep_count = 0; labels_idx = -1; }
  if (dep_count > MAX_DEPS_PER_ENTRY) dep_count = MAX_DEPS_PER_ENTRY;
  for (int i = 0; i < dep_count; i++) {
    if (parts[1 + i][0]) {
      parse_departure(parts[1 + i], &e->departures[i],
                      e->route_id, sizeof(e->route_id));
    }
  }

  if (labels_idx >= 0 && parts[labels_idx][0]) {
    char *labels[MAX_DISRUPTIONS];
    int lc = split_in_place(parts[labels_idx], 0x1e, labels, MAX_DISRUPTIONS);
    for (int i = 0; i < lc && i < MAX_DISRUPTIONS; i++) {
      copy_bounded(e->disruptions[i], labels[i], DISRUPTION_LEN);
      if (e->disruptions[i][0]) {
        e->disruption_count++;
      }
    }
  }

  menu_window_refresh();
  watch_window_refresh();
}

// POSITION_UPDATE format: "distance_km_x100|vehicle_desc|run_ref"
static void handle_position_update(char *data) {
  char *f[3];
  int fc = split_in_place(data, '|', f, 3);

  if (fc > 0 && f[0][0]) {
    g_app_state.watched_distance_km_x100 = atoi(f[0]);
  } else {
    g_app_state.watched_distance_km_x100 = INT32_MIN;
  }
  copy_bounded(g_app_state.watched_vehicle_desc,
               fc > 1 ? f[1] : "", sizeof(g_app_state.watched_vehicle_desc));
  copy_bounded(g_app_state.watched_run_ref,
               fc > 2 ? f[2] : "", sizeof(g_app_state.watched_run_ref));

  watch_window_refresh();
}

static void inbox_received_handler(DictionaryIterator *iter, void *context) {
  Tuple *type_tuple = dict_find(iter, KEY_INBOUND_TYPE);
  Tuple *data_tuple = dict_find(iter, KEY_INBOUND_DATA);
  if (!type_tuple) return;

  uint8_t type = (uint8_t)type_tuple->value->uint8;
  char *data = data_tuple ? data_tuple->value->cstring : "";

  switch (type) {
    case IN_CONN_STATE:
      handle_conn_state(data);
      break;
    case IN_FAV_UPDATE:
      handle_fav_update(data);
      break;
    case IN_POSITION_UPDATE:
      handle_position_update(data);
      break;
    case IN_FLAGS_SYNC:
      handle_flags_sync(data);
      break;
    case IN_ENTRY_SYNC:
    case IN_ENTRY_SYNC_BULK:
    case IN_ENTRY_SYNC_REPLACE:
      // All three follow the same shape: cancel any pending clear-refresh,
      // optionally wipe the existing set (REPLACE only), parse one or more
      // \x1f-separated entry chunks, then persist + refresh + close any
      // open watch face. Folded together so we don't carry three near-
      // identical copies of the boilerplate.
      cancel_pending_clear_refresh();
      if (type == IN_ENTRY_SYNC_REPLACE) {
        app_state_clear_entries();
      }
      if (type == IN_ENTRY_SYNC) {
        handle_entry_sync(data);
      } else {
        char *chunks[MAX_ENTRIES];
        int cc = split_in_place(data, 0x1f, chunks, MAX_ENTRIES);
        for (int i = 0; i < cc; i++) {
          if (chunks[i][0]) handle_entry_sync(chunks[i]);
        }
      }
      settings_store_save_entries();
      menu_window_refresh();
      if (watch_window_is_open()) watch_window_close();
      break;
    case IN_CLEAR_ENTRIES:
      app_state_clear_entries();
      settings_store_save_entries();
      // Defer the refresh: if a sync lands within the debounce window the
      // pre-existing rows stay on screen; otherwise we fall through to the
      // "No favourites" state once the timer fires.
      schedule_clear_refresh();
      if (watch_window_is_open()) watch_window_close();
      break;
    case IN_QUERY_RESULT:
      query_window_show_result(data);
      break;
    case IN_QUERY_CLARIFY:
      query_window_show_clarification(data);
      break;
    case IN_QUERY_ERROR:
      query_window_show_error(data);
      break;
    case IN_QUERY_PROGRESS:
      query_window_show_progress(data);
      break;
    case IN_QUERY_SAVED:
      // Agent stashed a favourite; PKJS is about to re-sync entries so we
      // just buzz to confirm and let the menu update itself.
      haptics_short();
      break;
    default:
      break;
  }
}

static void inbox_dropped_handler(AppMessageResult reason, void *context) {
  APP_LOG(APP_LOG_LEVEL_WARNING, "Inbox dropped: %d", reason);
}

static void outbox_failed_handler(DictionaryIterator *iter, AppMessageResult reason, void *context) {
  APP_LOG(APP_LOG_LEVEL_WARNING, "Outbox failed: %d", reason);
}

// ---- Outbound helpers ----------------------------------------------------

static void send_outbound(uint8_t type, const char *data) {
  DictionaryIterator *iter;
  AppMessageResult begin_res = app_message_outbox_begin(&iter);
  if (begin_res != APP_MSG_OK) {
    APP_LOG(APP_LOG_LEVEL_WARNING, "outbox_begin failed: type=%u res=%d",
            (unsigned)type, (int)begin_res);
    return;
  }
  dict_write_uint8(iter, KEY_OUTBOUND_TYPE, type);
  dict_write_cstring(iter, KEY_OUTBOUND_DATA, data ? data : "");
  AppMessageResult send_res = app_message_outbox_send();
  if (send_res != APP_MSG_OK) {
    APP_LOG(APP_LOG_LEVEL_WARNING, "outbox_send failed: type=%u res=%d",
            (unsigned)type, (int)send_res);
  }
}

void protocol_send_ready(void) {
  send_outbound(OUT_READY, "");
}

void protocol_send_watch_start(uint8_t button_id, const char *run_ref,
                               int32_t stop_id, uint8_t route_type,
                               const char *route_id, int32_t direction_id) {
  char buf[96];
  snprintf(buf, sizeof(buf), "%u|%s|%ld|%u|%s|%ld",
           (unsigned)button_id,
           run_ref ? run_ref : "",
           (long)stop_id,
           (unsigned)route_type,
           route_id ? route_id : "",
           (long)direction_id);
  send_outbound(OUT_WATCH_START, buf);
}

void protocol_send_watch_stop(void) {
  send_outbound(OUT_WATCH_STOP, "");
}

void protocol_send_open_config(void) {
  send_outbound(OUT_OPEN_CONFIG, "");
}

void protocol_send_refresh(void) {
  send_outbound(OUT_REFRESH, "");
}

void protocol_send_query(const char *text) {
  send_outbound(OUT_QUERY, text ? text : "");
}

void protocol_init(void) {
  app_message_register_inbox_received(inbox_received_handler);
  app_message_register_inbox_dropped(inbox_dropped_handler);
  app_message_register_outbox_failed(outbox_failed_handler);

  // Cap explicitly to keep aplite heap usage bounded. Protocol messages are
  // small (entry syncs ~250B, fav updates ~300B, watch_start ~96B). Bulk
  // entry syncs target 960-byte payloads; with two-tuple Dict overhead
  // (~17 B) that lands at ~977 B, leaving 47 B of headroom inside 1024.
  app_message_open(1024, 256);
}
