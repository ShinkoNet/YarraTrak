#pragma once

void splash_window_push(void);
void splash_window_pop(void);

// Replace the subtitle shown under the logo. Empty string restores the
// default "Connecting..." placeholder.
void splash_window_set_status(const char *text);
