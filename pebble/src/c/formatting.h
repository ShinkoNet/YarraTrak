#pragma once

#include "departures.h"

// Format countdown as "NOW!", "M:SS", "H:MM:SS", or "N min". Writes to out.
void fmt_countdown(int32_t seconds_until, const Departure *dep, char *out, size_t out_size);

// Format a short subtitle for menu rows ("Waiting...", "Now", "N min", "Hhr Mm", etc.).
void fmt_menu_subtitle(const Departure *dep, char *out, size_t out_size);

// Build menu row title ("Start>Dest"). Uses shortened names.
void fmt_menu_title(const Entry *entry, char *out, size_t out_size);

// Build watch-mode route text ("Start > Dest").
void fmt_watch_route(const Entry *entry, char *out, size_t out_size);
