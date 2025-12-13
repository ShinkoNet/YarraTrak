// pebble.js project main file

#include <pebble.h>
#include "simply/simply.h"

// by default, we 'simply' load simply and start running it
int main(void) {
  Simply *simply = simply_init();
  app_event_loop();
  simply_deinit(simply);
}
