#pragma once

#include <stdbool.h>
#include <stdint.h>

void menu_window_push(void);
void menu_window_refresh(void);
void menu_window_handle_entry_data(uint8_t button_id);
bool menu_window_is_on_top(void);
