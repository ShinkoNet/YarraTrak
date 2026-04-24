#pragma once

#include <pebble.h>
#include <stdbool.h>


GColor theme_bg(void);
GColor theme_fg(void);
GColor theme_accent(void);      // selected/highlight + progress bar
GColor theme_ring(void);        // ripple ring stroke (colour) or dot (aplite)

GColor theme_disruption(const char *label);

// true when the given label represents a "major" (non-caution) disruption - i.e
bool theme_is_major_disruption(const char *label);
