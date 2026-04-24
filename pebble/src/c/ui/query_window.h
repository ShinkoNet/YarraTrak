#pragma once

#include <stdbool.h>

// The query window is a single scrollable card showing the AI assistant's
// state. Flow:
//   1. User picks "Ask" from the menu.
//   2. query_window_start() → pushes the window, opens a DictationSession.
//   3. On dictation success we send OUT_QUERY via protocol and display
//      "Processing... You: <transcription>".
//   4. PKJS forwards the server reply as IN_QUERY_RESULT / _CLARIFY / _ERROR;
//      protocol.c calls the corresponding setter here.
//   5. For clarification, a second MenuLayer window is pushed; selecting a
//      row re-fires OUT_QUERY with the chosen value.

void query_window_start(void);

// Called from protocol inbox handlers.
void query_window_show_result(const char *tts_text);
void query_window_show_clarification(char *mutable_payload);  // in-place parsed
void query_window_show_error(const char *message);

bool query_window_is_open(void);
