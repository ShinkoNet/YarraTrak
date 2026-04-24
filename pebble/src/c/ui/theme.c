#include "theme.h"
#include "../app_state.h"

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
