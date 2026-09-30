from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DepartureProtocolTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'Host C compiler required')
    def test_metadata_is_decoded_per_departure_including_zero_ids(self):
        source = (ROOT / 'pebble/src/c/protocol.c').read_text()
        def function(name):
            start = source.rfind('\n', 0, source.index(name + '(')) + 1
            return source[start:source.index('\n}', start) + 2]
        harness = '#include "departures.h"\n#include <string.h>\n#include <stdlib.h>\n#include <assert.h>\n'
        harness += '\n'.join(function(f) for f in ('split_in_place', 'copy_bounded', 'parse_departure'))
        harness += r'''
int main(void) {
    Departure a, b, c;
    char first[]="5;1800000000;0;0;100;1;0";
    char second[]="10;1800000300;0;14;101;2;20";
    char unknown[]="10;1800000300;0;;102;2;";
    parse_departure(first, &a); parse_departure(second, &b); parse_departure(unknown, &c);
    assert(a.route_id == 0 && a.direction_id == 0);
    assert(b.route_id == 20 && b.direction_id == 14);
    assert(c.route_id == -1 && c.direction_id == -1);
    assert(!strcmp(a.run_ref, "100") && !strcmp(b.run_ref, "101"));
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'pebble.h').write_text('#include <stdint.h>\n#include <stdbool.h>\n#include <stddef.h>\n#include <time.h>\n')
            (path / 'protocol.c').write_text(harness)
            subprocess.run(['cc', '-I' + folder, '-I' + str(ROOT / 'pebble/src/c'),
                str(path / 'protocol.c'), '-o', str(path / 'protocol')], check=True, capture_output=True)
            subprocess.run([str(path / 'protocol')], check=True, capture_output=True)
