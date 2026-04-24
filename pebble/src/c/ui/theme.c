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
  // aplite just uses fg as accent
  return is_dark() ? GColorWhite : GColorBlack;
#endif
}

GColor theme_ring(void) {
#if defined(PBL_COLOR)
  // pulse colours need to survive both themes
  return is_dark() ? GColorBlueMoon : GColorPictonBlue;
#else
  // aplite rings need foreground contrast
  return theme_fg();
#endif
}

// orange survives both themes better
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
