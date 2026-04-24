#pragma once

#include <pebble.h>
#include <stdbool.h>

#define MAX_ENTRIES         10
#define MAX_DEPS_PER_ENTRY  2
#define MAX_DISRUPTIONS     3

#define NAME_LEN        24
#define FULL_NAME_LEN   48
#define RUN_REF_LEN     24
#define PLATFORM_LEN    8
#define ROUTE_ID_LEN    12
#define DISRUPTION_LEN  32
#define VEHICLE_DESC_LEN 40

typedef struct {
  bool has_data;
  int32_t minutes;            // Minutes-since-now fallback. -1 = unknown.
  time_t departure_unix;      // UTC epoch; 0 = unknown.
  uint8_t route_type;
  int32_t direction_id;
  char run_ref[RUN_REF_LEN];
  char platform[PLATFORM_LEN];
  char route_id[ROUTE_ID_LEN];
} Departure;

typedef struct {
  bool configured;
  char name[NAME_LEN];
  char full_name[FULL_NAME_LEN];
  char dest_name[NAME_LEN];
  char full_dest_name[FULL_NAME_LEN];
  int32_t stop_id;
  int32_t dest_id;
  int32_t direction_id;
  uint8_t route_type;

  Departure departures[MAX_DEPS_PER_ENTRY];
  uint8_t disruption_count;
  char disruptions[MAX_DISRUPTIONS][DISRUPTION_LEN];
} Entry;

// Pick the first valid departure for this entry. Returns NULL if none.
// `offset` = 0 for "next service", 1 for "service after".
Departure *departures_get(Entry *entry, uint8_t offset);

// Compute seconds until departure. Returns INT32_MAX if unknown.
int32_t departure_seconds_until(const Departure *dep);
