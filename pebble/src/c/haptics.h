#pragma once

#include <stdint.h>

// Play the minute-to-pattern haptic.
void haptics_play_for_minutes(int32_t minutes);

// Quick confirmation buzz.
void haptics_short(void);

// delays get a long pulse too
void haptics_long(void);

// Cancel any active vibration.
void haptics_cancel(void);
