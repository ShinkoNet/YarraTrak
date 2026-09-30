#include "departures.h"

#include <limits.h>
#include <string.h>

int32_t departure_seconds_until(const Departure *dep) {
  if (!dep || !dep->has_data) {
    return INT32_MAX;
  }
  if (dep->departure_unix != 0) {
    return (int32_t)(dep->departure_unix - time(NULL));
  }
  if (dep->minutes >= 0) {
    return dep->minutes * 60;
  }
  return INT32_MAX;
}

Departure *departures_get(Entry *entry, uint8_t offset) {
  if (!entry || !entry->configured) {
    return NULL;
  }

  uint8_t seen = 0;
  for (uint8_t i = 0; i < MAX_DEPS_PER_ENTRY; i++) {
    Departure *dep = &entry->departures[i];
    if (!dep->has_data) {
      continue;
    }
    // Grace window: a departure that's passed by <= 60s is still "current".
    int32_t sec = departure_seconds_until(dep);
    if (sec < -60) {
      continue;
    }
    if (seen == offset) {
      return dep;
    }
    seen++;
  }
  return NULL;
}

uint8_t departures_rebase_offset(Entry *entry, const char *run_ref, uint8_t offset) {
  if (run_ref && run_ref[0]) {
    for (uint8_t i = 0; i < MAX_DEPS_PER_ENTRY; ++i) {
      Departure *dep = departures_get(entry, i);
      if (dep && strcmp(dep->run_ref, run_ref) == 0) return i;
    }
    // The selected vehicle has left the live list. Start with the next service.
    return 0;
  }
  while (offset && !departures_get(entry, offset)) --offset;
  return offset;
}
