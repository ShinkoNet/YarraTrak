#pragma once

#include <stdbool.h>
#include <stdint.h>

void watch_window_push(uint8_t button_id);
void watch_window_refresh(void);
bool watch_window_is_open(void);

// Pop the countdown window back to the menu. Used when settings change
// (theme flip, bg-fx swap, entry list update) so the live watch face
// doesn't try to repaint mid-stream with mismatched colours / state.
// No-op if the window isn't on the stack.
void watch_window_close(void);
