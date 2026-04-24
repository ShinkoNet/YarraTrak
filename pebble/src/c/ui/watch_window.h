#pragma once

#include <stdbool.h>
#include <stdint.h>

void watch_window_push(uint8_t button_id);
void watch_window_refresh(void);
bool watch_window_is_open(void);
