Run offline regression tests from the repository root:

```sh
python -m unittest discover -s tests -v
```

`fixtures/parkville.json` contains public PTV v3 timetable responses captured on
30 September 2026, reduced to departure fields. No credentials or signed URLs
are included. Patterns were requested with `expand=Run`,
`include_advertised_interchange=true` and `include_skipped_stops=false`.

These cases distinguish the East Pakenham continuation from Cranbourne,
Sunbury and Watergardens services, including a Sunbury run whose feeder has
already passed Narre Warren. The countdown, HTTP and WebSocket checks must
ignore an old saved direction when a destination is supplied.
