#pragma once

#include <pebble.h>

// Colour getters keyed off g_app_state.flags.dark_theme. Default (unset) is
// light theme: white background, black text, cerulean highlight. Dark theme
// flips bg/fg. On aplite this collapses to pure black/white since the display
// has no colour; the ripple ring helper returns the foreground colour so the
// stippled dots show up correctly on either background.

GColor theme_bg(void);
GColor theme_fg(void);
GColor theme_accent(void);      // selected/highlight + progress bar
GColor theme_ring(void);        // ripple ring stroke (colour) or dot (aplite)
