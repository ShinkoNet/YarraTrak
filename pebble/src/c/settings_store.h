#pragma once

#include <stdbool.h>
#include <stdint.h>

// Persisted flag keys.
#define PKEY_FLAG_VIBE_DISABLED    100
#define PKEY_FLAG_RIPPLE_DISABLED  101
#define PKEY_FLAG_SHAKE_DISABLED   102
#define PKEY_FLAG_AI_DISABLED      103
#define PKEY_FLAG_24H_TIME         104

// Per-entry persisted key ranges. Each entry uses 200 + 10*(N-1) + field.
// Field offsets:
#define PKEY_ENTRY_BASE            200
#define PKEY_ENTRY_STRIDE          10
#define PKEY_ENTRY_CONFIGURED      0
#define PKEY_ENTRY_NAME            1
#define PKEY_ENTRY_FULL_NAME       2
#define PKEY_ENTRY_DEST_NAME       3
#define PKEY_ENTRY_FULL_DEST_NAME  4
#define PKEY_ENTRY_STOP_ID         5
#define PKEY_ENTRY_DEST_ID         6
#define PKEY_ENTRY_DIRECTION_ID    7
#define PKEY_ENTRY_ROUTE_TYPE      8

#define PKEY_ENTRY_COUNT           199

void settings_store_load(void);
void settings_store_save_flags(void);
void settings_store_save_entries(void);
