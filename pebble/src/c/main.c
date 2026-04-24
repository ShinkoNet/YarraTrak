#include "app_state.h"
#include "protocol.h"
#include "settings_store.h"
#include "ui/splash_window.h"
#include "ui/menu_window.h"

#include <pebble.h>

static AppTimer *s_splash_timer = NULL;
static bool s_menu_pushed = false;

static void promote_to_menu(void *unused) {
  s_splash_timer = NULL;
  if (s_menu_pushed) return;
  s_menu_pushed = true;

  splash_window_pop();
  menu_window_push();
}

static void init(void) {
  app_state_init();
  settings_store_load();

  protocol_init();

  splash_window_push();
  protocol_send_ready();

  // Show the menu after a short beat regardless of PKJS readiness so users
  // with cached favourites see the UI even when the phone bridge is slow.
  s_splash_timer = app_timer_register(1500, promote_to_menu, NULL);
}

static void deinit(void) {
  if (s_splash_timer) {
    app_timer_cancel(s_splash_timer);
    s_splash_timer = NULL;
  }
  app_message_deregister_callbacks();
}

int main(void) {
  init();
  app_event_loop();
  deinit();
}
