#include "haptics.h"
#include "app_state.h"

#include <pebble.h>

#define MAX_SEGMENTS 40

static void play_pattern(const uint32_t *segments, uint32_t count) {
  if (g_app_state.flags.disable_vibration) {
    vibes_cancel();
    return;
  }
  VibePattern pat = {
    .durations = (uint32_t *)segments,
    .num_segments = count,
  };
  vibes_enqueue_custom_pattern(pat);
}

// Shave-and-a-haircut: "NOW" pattern. Total ~7 segments.
static void play_shave_and_haircut(void) {
  static const uint32_t pat[] = {
    200, 100, 100, 100, 200, 150, 400
  };
  play_pattern(pat, sizeof(pat) / sizeof(pat[0]));
}

void haptics_play_for_minutes(int32_t minutes) {
  if (g_app_state.flags.disable_vibration) {
    return;
  }

  if (minutes <= 0) {
    play_shave_and_haircut();
    return;
  }

  uint32_t segments[MAX_SEGMENTS];
  uint32_t count = 0;

  int32_t hours = minutes / 60;
  int32_t remaining = minutes % 60;
  int32_t tens = remaining / 10;
  int32_t ones = remaining % 10;

  // Long buzzes for each hour.
  for (int32_t i = 0; i < hours && count < MAX_SEGMENTS - 1; i++) {
    if (count > 0) segments[count++] = 300;  // gap
    segments[count++] = 700;
  }
  if (hours > 0 && tens + ones > 0 && count < MAX_SEGMENTS - 1) {
    segments[count++] = 500;
  }

  // Medium buzzes for each ten-minute.
  for (int32_t i = 0; i < tens && count < MAX_SEGMENTS - 1; i++) {
    if (count > 0) segments[count++] = 200;
    segments[count++] = 350;
  }
  if (tens > 0 && ones > 0 && count < MAX_SEGMENTS - 1) {
    segments[count++] = 400;
  }

  // Short buzzes for each single-minute.
  for (int32_t i = 0; i < ones && count < MAX_SEGMENTS - 1; i++) {
    if (count > 0) segments[count++] = 150;
    segments[count++] = 120;
  }

  if (count > 0) {
    play_pattern(segments, count);
  }
}

void haptics_short(void) {
  if (g_app_state.flags.disable_vibration) {
    return;
  }
  vibes_short_pulse();
}

void haptics_cancel(void) {
  vibes_cancel();
}
