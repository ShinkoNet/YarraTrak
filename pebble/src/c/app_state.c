#include "app_state.h"
#include "protocol.h"

#include <limits.h>
#include <string.h>

AppState g_app_state;

void app_state_init(void) {
  memset(&g_app_state, 0, sizeof(g_app_state));
  g_app_state.conn_state = CONN_CONNECTING;
  g_app_state.watched_distance_km_x100 = INT32_MIN;
}

void app_state_clear_entries(void) {
  for (uint8_t i = 0; i < MAX_ENTRIES; i++) {
    memset(&g_app_state.entries[i], 0, sizeof(Entry));
  }
  g_app_state.entry_count = 0;
}

Entry *app_state_get_entry(uint8_t button_id) {
  if (button_id < 1 || button_id > MAX_ENTRIES) {
    return NULL;
  }
  return &g_app_state.entries[button_id - 1];
}
