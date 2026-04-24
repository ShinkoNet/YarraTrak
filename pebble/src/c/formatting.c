#include "formatting.h"

#include <pebble.h>
#include <string.h>
#include <stdio.h>

void fmt_countdown(int32_t seconds_until, const Departure *dep, char *out, size_t out_size) {
  if (!dep || !dep->has_data) {
    strncpy(out, "--", out_size);
    out[out_size - 1] = '\0';
    return;
  }

  // Below 30 seconds or passed -> NOW!
  if (seconds_until < 30) {
    strncpy(out, "NOW!", out_size);
    out[out_size - 1] = '\0';
    return;
  }

  // If only minutes-precision known, fall back to "N min" or "H:MM".
  if (dep->departure_unix == 0) {
    if (dep->minutes >= 60) {
      snprintf(out, out_size, "%ld:%02ld", (long)(dep->minutes / 60), (long)(dep->minutes % 60));
    } else {
      snprintf(out, out_size, "%ld min", (long)dep->minutes);
    }
    return;
  }

  if (seconds_until < 3600) {
    int32_t m = seconds_until / 60;
    int32_t s = seconds_until % 60;
    snprintf(out, out_size, "%ld:%02ld", (long)m, (long)s);
  } else {
    int32_t h = seconds_until / 3600;
    int32_t m = (seconds_until % 3600) / 60;
    int32_t s = seconds_until % 60;
    snprintf(out, out_size, "%ld:%02ld:%02ld", (long)h, (long)m, (long)s);
  }
}

void fmt_menu_subtitle(const Departure *dep, char *out, size_t out_size) {
  if (!dep || !dep->has_data) {
    strncpy(out, "Waiting...", out_size);
    out[out_size - 1] = '\0';
    return;
  }

  int32_t sec = departure_seconds_until(dep);
  if (sec < 60) {
    strncpy(out, "Now", out_size);
    out[out_size - 1] = '\0';
    return;
  }

  int32_t mins = sec / 60;
  if (mins < 60) {
    snprintf(out, out_size, "%ld min", (long)mins);
  } else {
    snprintf(out, out_size, "%ldhr %ldm", (long)(mins / 60), (long)(mins % 60));
  }
}

void fmt_menu_title(const Entry *entry, char *out, size_t out_size) {
  if (!entry || !entry->configured) {
    out[0] = '\0';
    return;
  }
  if (entry->dest_name[0]) {
    snprintf(out, out_size, "%s>%s", entry->name, entry->dest_name);
  } else {
    snprintf(out, out_size, "%s", entry->name);
  }
}

void fmt_watch_route(const Entry *entry, char *out, size_t out_size) {
  if (!entry || !entry->configured) {
    out[0] = '\0';
    return;
  }
  if (entry->dest_name[0]) {
    snprintf(out, out_size, "%s > %s", entry->name, entry->dest_name);
  } else {
    snprintf(out, out_size, "%s", entry->name);
  }
}
