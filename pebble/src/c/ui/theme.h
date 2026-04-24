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

// Colour used for disruption labels in menu subtitles and the watch bottom
// line. Picks orange for caution-level events (Minor Delays, bus
// replacements, upcoming service changes) and red for warning-level
// (anything else). Falls back to theme_fg on aplite since it's 1-bit.
GColor theme_disruption(const char *label);

// Watch-window foreground / disruption colour with the active background-FX
// taken into account. Plasma and Fire flood the screen with mid-bright
// colours, so the regular black-or-white text + orange/red disruption
// label both lose contrast — these helpers force a high-contrast colour
// (black, the only thing readable across every fire/plasma cell) when
// those effects are running. Other effects fall through to the regular
// theme_fg / theme_disruption.
GColor theme_watch_fg(void);
GColor theme_watch_disruption(const char *label);
GColor theme_watch_bg(void);  // matches a clear background colour for text outline
