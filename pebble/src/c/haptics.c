#include "haptics.h"
#include "app_state.h"

#include <pebble.h>

// 64 slots is enough haptic room
#define MAX_SEGMENTS 64

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

// now buzz is the old jingle
static void play_shave_and_haircut(void) {
  static const uint32_t pat[] = {
    43, 300, 43, 71, 43, 43, 43, 100, 43, 300, 43, 643, 43, 300, 43
  };
  play_pattern(pat, sizeof(pat) / sizeof(pat[0]));
}

// minute haptics follow the old app
void haptics_play_for_minutes(int32_t minutes) {
  if (g_app_state.flags.disable_vibration) {
    return;
  }

  if (minutes <= 0) {
    play_shave_and_haircut();
    return;
  }
  if (minutes > 720) minutes = 720;

  int32_t hours = minutes / 60;
  int32_t remaining = minutes % 60;
  int32_t tens = remaining / 10;
  int32_t ones = remaining % 10;

  uint32_t pat[MAX_SEGMENTS];
  uint32_t n = 0;

  for (int32_t i = 0; i < hours && n + 1 < MAX_SEGMENTS; i++) {
    pat[n++] = 800;
    pat[n++] = 300;
  }
  if (hours > 0 && (tens > 0 || ones > 0) && n > 0) {
    pat[n - 1] += 200;
  }

  for (int32_t i = 0; i < tens && n + 1 < MAX_SEGMENTS; i++) {
    pat[n++] = 300;
    pat[n++] = 150;
  }
  if (tens > 0 && ones > 0 && n > 0) {
    pat[n - 1] += 100;
  }

  for (int32_t i = 0; i < ones && n + 1 < MAX_SEGMENTS; i++) {
    pat[n++] = 80;
    pat[n++] = 180;
  }

  if (n > 0) {
    play_pattern(pat, n);
  }
}

void haptics_short(void) {
  if (g_app_state.flags.disable_vibration) {
    return;
  }
  vibes_short_pulse();
}

void haptics_long(void) {
  if (g_app_state.flags.disable_vibration) {
    return;
  }
  vibes_long_pulse();
}

void haptics_cancel(void) {
  vibes_cancel();
}
