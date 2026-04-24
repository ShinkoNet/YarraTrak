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
bool theme_is_major_disruption(const char *label) {
  if (!label || !label[0]) return false;
  if (strncmp(label, "Minor Delays", 12) == 0 ||
      strstr(label, "Buses") != NULL ||
      strstr(label, "Bus Replacement") != NULL ||
      strstr(label, "Service Change") != NULL) {
    return false;
  }
  return true;
}

GColor theme_disruption(const char *label) {
#if defined(PBL_COLOR)
  if (!label || !label[0]) return theme_fg();
  return theme_is_major_disruption(label) ? GColorRed : GColorOrange;
#else
  (void)label;
  return theme_fg();
#endif
}
