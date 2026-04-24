#pragma once

#include <stdint.h>

// Play the minute-to-pattern haptic.
void haptics_play_for_minutes(int32_t minutes);

// Quick confirmation buzz.
void haptics_short(void);

// Single long pulse — fired alongside the left/right shake when a delay is
// detected, so the wrist feels the slippage as well as sees it.
void haptics_long(void);

// Cancel any active vibration.
void haptics_cancel(void);
