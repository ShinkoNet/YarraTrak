#pragma once

#include <pebble.h>


GColor theme_bg(void);
GColor theme_fg(void);
GColor theme_accent(void);      // selected/highlight + progress bar
GColor theme_ring(void);        // ripple ring stroke (colour) or dot (aplite)

GColor theme_disruption(const char *label);

// watch-window foreground / disruption colour with the active background-fx taken into account
GColor theme_watch_fg(void);
GColor theme_watch_disruption(const char *label);
GColor theme_watch_bg(void);  // matches a clear background colour for text outline
