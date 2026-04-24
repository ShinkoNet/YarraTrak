#pragma once

#include <stdint.h>

// Play the minute-to-pattern haptic.
void haptics_play_for_minutes(int32_t minutes);

// Quick confirmation buzz.
void haptics_short(void);

// Cancel any active vibration.
void haptics_cancel(void);
