#pragma once

#include "departures.h"
#include <pebble.h>
#include <stdbool.h>

typedef struct {
  bool disable_vibration;
  bool disable_ripple_vfx;
  bool disable_timer_shake;
  bool disable_ai_assistant;
  bool use_24hr_time;
} Flags;

typedef struct {
  Flags flags;
  Entry entries[MAX_ENTRIES];
  uint8_t entry_count;

  uint8_t conn_state;       // CONN_OFFLINE / CONN_CONNECTING / CONN_CONNECTED
  bool settings_received;   // true once PKJS has sent at least one FLAGS_SYNC

  // Current watched entry (1..entry_count, 0 = none).
  uint8_t watching_button;
  uint8_t watching_offset;  // 0 = next service, 1 = service after

  // Position info for currently watched run.
  int32_t watched_distance_km_x100;  // *100 for 2 decimals. INT32_MIN = unknown.
  char watched_vehicle_desc[VEHICLE_DESC_LEN];
  char watched_run_ref[RUN_REF_LEN];
} AppState;

extern AppState g_app_state;

void app_state_init(void);
void app_state_clear_entries(void);
Entry *app_state_get_entry(uint8_t button_id);  // 1-indexed
