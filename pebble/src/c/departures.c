#include "departures.h"

#include <limits.h>

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
