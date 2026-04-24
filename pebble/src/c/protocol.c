#include "protocol.h"
#include "app_state.h"
#include "settings_store.h"
#include "ui/menu_window.h"
#include "ui/watch_window.h"

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

// ---- Inbound handlers ----------------------------------------------------

static void handle_conn_state(const char *data) {
  int v = atoi(data);
  if (v < 0) v = 0;
  if (v > 2) v = 2;
  g_app_state.conn_state = (uint8_t)v;
  menu_window_refresh();
  watch_window_refresh();
}

// FLAGS_SYNC format: "flag_bits"
// bit0 disable_vibration, bit1 disable_ripple_vfx, bit2 disable_timer_shake,
// bit3 disable_ai_assistant, bit4 use_24hr_time
static void handle_flags_sync(const char *data) {
  int bits = atoi(data);
  Flags *f = &g_app_state.flags;
  f->disable_vibration    = (bits & 1)  != 0;
  f->disable_ripple_vfx   = (bits & 2)  != 0;
  f->disable_timer_shake  = (bits & 4)  != 0;
  f->disable_ai_assistant = (bits & 8)  != 0;
  f->use_24hr_time        = (bits & 16) != 0;
  settings_store_save_flags();
  g_app_state.settings_received = true;
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
static void parse_departure(char *blob, Departure *out) {
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
  out->route_type      = fc > 2 ? (uint8_t)atoi(f[2]) : 0;
  out->direction_id    = fc > 3 ? atoi(f[3]) : 0;
  copy_bounded(out->run_ref,  fc > 4 ? f[4] : "", sizeof(out->run_ref));
  copy_bounded(out->platform, fc > 5 ? f[5] : "", sizeof(out->platform));
  copy_bounded(out->route_id, fc > 6 ? f[6] : "", sizeof(out->route_id));

  out->has_data = (out->minutes >= 0) || (out->departure_unix != 0);
}

static void handle_fav_update(char *data) {
  char *parts[4];
  int pc = split_in_place(data, '|', parts, 4);
  if (pc < 1) return;

  int button_id = atoi(parts[0]);
  Entry *e = app_state_get_entry((uint8_t)button_id);
  if (!e) return;

  // Clear existing departures / disruptions before filling.
  for (uint8_t i = 0; i < MAX_DEPS_PER_ENTRY; i++) {
    memset(&e->departures[i], 0, sizeof(Departure));
  }
  e->disruption_count = 0;

  if (pc > 1 && parts[1][0]) {
    parse_departure(parts[1], &e->departures[0]);
  }
  if (pc > 2 && parts[2][0]) {
    parse_departure(parts[2], &e->departures[1]);
  }

  if (pc > 3 && parts[3][0]) {
    char *labels[MAX_DISRUPTIONS];
    int lc = split_in_place(parts[3], 0x1e, labels, MAX_DISRUPTIONS);
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
      handle_entry_sync(data);
      settings_store_save_entries();
      menu_window_refresh();
      break;
    case IN_ENTRY_SYNC_BULK: {
      // "entry1\x1fentry2\x1f..." where each sub-chunk is the same format
      // IN_ENTRY_SYNC accepts: "index|name;full_name;stop_id;dest_name;
      // full_dest_name;dest_id;route_type;direction_id".
      char *chunks[MAX_ENTRIES];
      int cc = split_in_place(data, 0x1f, chunks, MAX_ENTRIES);
      for (int i = 0; i < cc; i++) {
        if (chunks[i][0]) handle_entry_sync(chunks[i]);
      }
      settings_store_save_entries();
      menu_window_refresh();
      break;
    }
    case IN_CLEAR_ENTRIES:
      app_state_clear_entries();
      settings_store_save_entries();
      menu_window_refresh();
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
  if (app_message_outbox_begin(&iter) != APP_MSG_OK) {
    return;
  }
  dict_write_uint8(iter, KEY_OUTBOUND_TYPE, type);
  dict_write_cstring(iter, KEY_OUTBOUND_DATA, data ? data : "");
  app_message_outbox_send();
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

void protocol_init(void) {
  app_message_register_inbox_received(inbox_received_handler);
  app_message_register_inbox_dropped(inbox_dropped_handler);
  app_message_register_outbox_failed(outbox_failed_handler);

  // Cap explicitly to keep aplite heap usage bounded. Protocol messages are
  // small (entry syncs ~250B, fav updates ~300B, watch_start ~96B).
  app_message_open(1024, 256);
}
