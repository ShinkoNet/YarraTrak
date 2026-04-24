#pragma once

#include <pebble.h>

// AppMessage key IDs — must match appinfo.json appKeys.
#define KEY_INBOUND_TYPE   1
#define KEY_INBOUND_DATA   2
#define KEY_OUTBOUND_TYPE  3
#define KEY_OUTBOUND_DATA  4

// Inbound message types (JS -> C).
enum {
  IN_CONN_STATE      = 1,
  IN_FAV_UPDATE      = 2,
  IN_POSITION_UPDATE = 3,
  IN_FLAGS_SYNC      = 4,
  IN_ENTRY_SYNC      = 5,
  IN_CLEAR_ENTRIES   = 6,
  IN_WATCH_ACK       = 7,
  IN_ENTRY_SYNC_BULK = 8,
};

// Outbound message types (C -> JS).
enum {
  OUT_READY        = 1,
  OUT_WATCH_START  = 2,
  OUT_WATCH_STOP   = 3,
  OUT_OPEN_CONFIG  = 4,
  OUT_REFRESH      = 5,
};

// Connection states.
enum {
  CONN_OFFLINE    = 0,
  CONN_CONNECTING = 1,
  CONN_CONNECTED  = 2,
};

void protocol_init(void);
void protocol_send_ready(void);
void protocol_send_watch_start(uint8_t button_id, const char *run_ref,
                               int32_t stop_id, uint8_t route_type,
                               const char *route_id, int32_t direction_id);
void protocol_send_watch_stop(void);
void protocol_send_open_config(void);
void protocol_send_refresh(void);
