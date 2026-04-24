#include "settings_store.h"
#include "app_state.h"

#include <pebble.h>
#include <string.h>

static uint32_t entry_key(uint8_t index_zero, uint8_t field) {
  return PKEY_ENTRY_BASE + PKEY_ENTRY_STRIDE * index_zero + field;
}

static void read_string(uint32_t key, char *out, size_t out_size) {
  if (persist_exists(key)) {
    persist_read_string(key, out, out_size);
  } else {
    out[0] = '\0';
  }
}

void settings_store_load(void) {
  Flags *f = &g_app_state.flags;
  f->disable_vibration   = persist_read_bool(PKEY_FLAG_VIBE_DISABLED);
  f->disable_ripple_vfx  = persist_read_bool(PKEY_FLAG_RIPPLE_DISABLED);
  f->disable_timer_shake = persist_read_bool(PKEY_FLAG_SHAKE_DISABLED);
  f->disable_ai_assistant = persist_read_bool(PKEY_FLAG_AI_DISABLED);
  f->use_24hr_time       = persist_read_bool(PKEY_FLAG_24H_TIME);

  g_app_state.entry_count = 0;
  if (persist_exists(PKEY_ENTRY_COUNT)) {
    int32_t c = persist_read_int(PKEY_ENTRY_COUNT);
    if (c > 0 && c <= MAX_ENTRIES) {
      g_app_state.entry_count = (uint8_t)c;
    }
  }

  for (uint8_t i = 0; i < MAX_ENTRIES; i++) {
    Entry *e = &g_app_state.entries[i];
    e->configured = persist_read_bool(entry_key(i, PKEY_ENTRY_CONFIGURED));
    if (!e->configured) {
      continue;
    }
    read_string(entry_key(i, PKEY_ENTRY_NAME),      e->name,      sizeof(e->name));
    read_string(entry_key(i, PKEY_ENTRY_DEST_NAME), e->dest_name, sizeof(e->dest_name));
    e->stop_id      = persist_read_int(entry_key(i, PKEY_ENTRY_STOP_ID));
    e->direction_id = persist_read_int(entry_key(i, PKEY_ENTRY_DIRECTION_ID));
    e->route_type   = (uint8_t)persist_read_int(entry_key(i, PKEY_ENTRY_ROUTE_TYPE));
  }
}

void settings_store_save_flags(void) {
  const Flags *f = &g_app_state.flags;
  persist_write_bool(PKEY_FLAG_VIBE_DISABLED,    f->disable_vibration);
  persist_write_bool(PKEY_FLAG_RIPPLE_DISABLED,  f->disable_ripple_vfx);
  persist_write_bool(PKEY_FLAG_SHAKE_DISABLED,   f->disable_timer_shake);
  persist_write_bool(PKEY_FLAG_AI_DISABLED,      f->disable_ai_assistant);
  persist_write_bool(PKEY_FLAG_24H_TIME,         f->use_24hr_time);
}

void settings_store_save_entries(void) {
  persist_write_int(PKEY_ENTRY_COUNT, g_app_state.entry_count);

  for (uint8_t i = 0; i < MAX_ENTRIES; i++) {
    Entry *e = &g_app_state.entries[i];
    persist_write_bool(entry_key(i, PKEY_ENTRY_CONFIGURED), e->configured);
    if (!e->configured) {
      continue;
    }
    persist_write_string(entry_key(i, PKEY_ENTRY_NAME),      e->name);
    persist_write_string(entry_key(i, PKEY_ENTRY_DEST_NAME), e->dest_name);
    persist_write_int(entry_key(i, PKEY_ENTRY_STOP_ID),      e->stop_id);
    persist_write_int(entry_key(i, PKEY_ENTRY_DIRECTION_ID), e->direction_id);
    persist_write_int(entry_key(i, PKEY_ENTRY_ROUTE_TYPE),   e->route_type);
  }
}
