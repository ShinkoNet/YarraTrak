#include "theme.h"
#include "../app_state.h"

#include <string.h>

static bool is_dark(void) {
  return g_app_state.flags.dark_theme;
}

GColor theme_bg(void) {
  return is_dark() ? GColorBlack : GColorWhite;
}

GColor theme_fg(void) {
  return is_dark() ? GColorWhite : GColorBlack;
}

GColor theme_accent(void) {
#if defined(PBL_COLOR)
  return GColorVividCerulean;
#else
  // On aplite we don't have a true accent — the menu row fills with the
  // opposite of bg when highlighted, and the progress bar uses fg.
  return is_dark() ? GColorWhite : GColorBlack;
#endif
}

GColor theme_ring(void) {
#if defined(PBL_COLOR)
  // BlueMoon on dark reads as a subtle indigo pulse; PictonBlue on light
  // gives a soft sky-blue stroke that doesn't wash out the white canvas.
  return is_dark() ? GColorBlueMoon : GColorPictonBlue;
#else
  // Aplite: rings are the foreground colour so the stippled dots are
  // visible against the background in either theme.
  return theme_fg();
#endif
}

// V1 used yellow for caution-level disruptions; orange reads better on both
// white and black backgrounds so it survives the theme flip. Anything more
// severe uses the usual red.
GColor theme_disruption(const char *label) {
#if defined(PBL_COLOR)
  if (!label || !label[0]) return theme_fg();
  if (strncmp(label, "Minor Delays", 12) == 0 ||
      strstr(label, "Buses") != NULL ||
      strstr(label, "Bus Replacement") != NULL ||
      strstr(label, "Service Change") != NULL) {
    return GColorOrange;
  }
  return GColorRed;
#else
  (void)label;
  return theme_fg();
#endif
}

static bool fx_floods_background(void) {
  // Plasma cycles through mid-bright blues/cyans/greens; fire ramps from
  // dark red through white. Both effects fill every pixel with colour, so
  // the normal theme text colours can blend into the worst-case cells.
  uint8_t fx = g_app_state.flags.bg_fx;
  return fx == BG_FX_PLASMA || fx == BG_FX_FIRE;
}

GColor theme_watch_fg(void) {
#if defined(PBL_COLOR)
  if (fx_floods_background()) return GColorBlack;
#endif
  return theme_fg();
}

GColor theme_watch_disruption(const char *label) {
#if defined(PBL_COLOR)
  if (fx_floods_background()) return GColorBlack;
#endif
  return theme_disruption(label);
}

GColor theme_watch_bg(void) {
#if defined(PBL_COLOR)
  // When the FX layer paints the whole frame anyway, the watch window's
  // own background colour is mostly irrelevant; use clear so any chrome
  // that draws a backdrop doesn't punch a hole in the effect.
  if (fx_floods_background()) return GColorClear;
#endif
  return theme_bg();
}
