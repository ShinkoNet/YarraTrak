#pragma once
#include <pebble.h>
void nearby_window_show(Window *window, void (*on_back)(void));
void nearby_window_close(void);
void nearby_window_receive_row(char *payload);
void nearby_window_receive_status(char *payload);
void nearby_window_receive_tracker(char *payload);
void nearby_window_resume(void);
