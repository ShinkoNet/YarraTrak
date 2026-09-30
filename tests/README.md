Run offline regression tests from the repository root:

```sh
python -m unittest discover -s tests -v
node tests/test_departures.js
node tests/test_nearby.js
node tests/test_settings_sync.js
node tests/test_tracking_connection.js
```

Install a host C compiler (`cc`) to run the watch protocol and tracking-state
tests; these use a fake clock and minimal SDK headers. They are skipped when
the compiler is unavailable. Build all watch targets with `pebble build` from
the `pebble` directory, and verify Aplite memory and navigation in its emulator.

`fixtures/parkville.json` contains public PTV v3 timetable responses captured on
30 September 2026, reduced to departure fields. No credentials or signed URLs
are included. Patterns were requested with `expand=Run`,
`include_advertised_interchange=true` and `include_skipped_stops=false`.

These cases distinguish the East Pakenham continuation from Cranbourne,
Sunbury and Watergardens services, including a Sunbury run whose feeder has
already passed Narre Warren. The countdown, HTTP and WebSocket checks must
ignore an old saved direction when a destination is supplied.
