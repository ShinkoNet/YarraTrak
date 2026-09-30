"""Exercise watch tracking transitions with the production C functions and a fake clock."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TrackerStateTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'Host C compiler required')
    def test_service_identity_rollover_delay_and_alerts(self):
        source = (ROOT / 'pebble/src/c/ui/watch_window.c').read_text()

        def function(name):
            start = source.rfind('\n', 0, source.index(name + '(')) + 1
            end = source.index('\n}', start) + 2
            return source[start:end]

        functions = '\n'.join(function(name) for name in (
            'get_watched_departure', 'reconcile_service',
            'send_watch_start_if_needed', 'maybe_vibrate'))
        harness = r'''
#include "app_state.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <limits.h>
AppState g_app_state;
static char s_last_run_ref[RUN_REF_LEN], s_position_run_ref[RUN_REF_LEN];
static int32_t s_last_vibrated_minutes = -1, s_last_seconds = INT32_MAX;
static bool s_now_pattern_fired;
static int buzzes, last_buzz, starts;
static time_t now = 10000;
time_t fake_time(time_t *ptr) { if (ptr) *ptr = now; return now; }
#define time fake_time
Entry *app_state_get_entry(uint8_t id) { return &g_app_state.entries[0]; }
void protocol_send_watch_start(uint8_t id, const char *ref, int32_t stop,
    uint8_t type, const char *route, int32_t direction) { starts++; }
void haptics_play_for_minutes(int32_t minutes) { buzzes++; last_buzz = minutes; }
'''
        harness += (ROOT / 'pebble/src/c/departures.c').read_text() + '\n' + functions
        harness += r'''
void tick(void) {
    Departure *dep = reconcile_service();
    if (dep) { send_watch_start_if_needed(dep); maybe_vibrate(dep); }
}
void seed(void) {
    memset(&g_app_state, 0, sizeof(g_app_state));
    Entry *e = app_state_get_entry(1); e->configured = true;
    now = 10000; buzzes = starts = 0;
    s_last_run_ref[0] = s_position_run_ref[0] = 0;
    s_last_vibrated_minutes = -1; s_now_pattern_fired = false;
    for (int i = 0; i < 3; i++) {
        Departure *d = &e->departures[i];
        d->has_data = true; d->departure_unix = 10000 + i * 600;
        d->run_ref[0] = 'A' + i;
    }
}
int main(void) {
    seed(); Entry *e = app_state_get_entry(1);
    g_app_state.watching_offset = 1; tick();
    assert(!strcmp(s_last_run_ref, "B"));
    now += 61; tick();
    assert(g_app_state.watching_offset == 0 && !strcmp(s_last_run_ref, "B"));
    assert(starts == 1); // Same vehicle moved into the next-service slot.

    // Fresh buffer [B,C,D] must keep B selected, with C now service-after.
    e->departures[0] = e->departures[1]; e->departures[1] = e->departures[2];
    strcpy(e->departures[2].run_ref, "D"); e->departures[2].departure_unix = 11800;
    tick(); assert(!strcmp(s_last_run_ref, "B"));
    assert(!strcmp(departures_get(e, 1)->run_ref, "C"));

    // When the tracked vehicle itself leaves, select the next one and reset alerts.
    seed(); tick(); assert(s_now_pattern_fired);
    s_last_seconds = -60; now += 61; tick();
    assert(!strcmp(s_last_run_ref, "B") && s_last_seconds == INT32_MAX);
    assert(!s_now_pattern_fired && buzzes == 2);

    // A reordered live estimate must preserve A even when it moves later.
    seed(); tick(); Departure a = e->departures[0];
    a.departure_unix = 11200; e->departures[0] = e->departures[1]; e->departures[1] = a;
    tick(); assert(!strcmp(s_last_run_ref, "A") && g_app_state.watching_offset == 1);

    // NOW followed by a delay must count down and announce arrival again.
    seed(); tick(); assert(s_now_pattern_fired && last_buzz == 0);
    e->departures[0].departure_unix = now + 180; tick();
    assert(!s_now_pattern_fired);
    now += 61; tick(); assert(last_buzz == 2);
    now = 10160; tick(); assert(s_now_pattern_fired && last_buzz == 0);

    // An empty transient response does not erase the selected vehicle identity.
    seed(); g_app_state.watching_offset = 2; tick();
    for (int i = 0; i < 3; i++) e->departures[i].has_data = false;
    tick(); assert(!strcmp(s_last_run_ref, "C"));
    e->departures[2].has_data = true; tick();
    assert(!strcmp(s_last_run_ref, "C") && g_app_state.watching_offset == 0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'pebble.h').write_text('#include <stdint.h>\n#include <stdbool.h>\n#include <stddef.h>\n#include <time.h>\n')
            (path / 'tracker.c').write_text(harness)
            subprocess.run(['cc', '-I' + folder, '-I' + str(ROOT / 'pebble/src/c'),
                            str(path / 'tracker.c'), '-o', str(path / 'tracker')], check=True, capture_output=True)
            subprocess.run([str(path / 'tracker')], check=True, capture_output=True)
