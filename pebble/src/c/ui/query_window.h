#pragma once

#include <stdbool.h>

// query card owns the ask flow

void query_window_start(void);

// Called from protocol inbox handlers.
void query_window_show_result(const char *tts_text);
void query_window_show_clarification(char *mutable_payload);  // in-place parsed
void query_window_show_error(const char *message);

bool query_window_is_open(void);
