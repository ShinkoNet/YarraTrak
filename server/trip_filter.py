"""Select train departures by their actual stops, including advertised continuations."""

import asyncio
from collections import OrderedDict
import logging
import time

logger = logging.getLogger(__name__)


def reaches_destination(pattern: dict, departure: dict, dest_id: int) -> bool:
    """Require the destination after this boarding event, not in a feeder run."""
    if departure.get("stop_id") == dest_id:
        return False
    stops = pattern.get("departures", [])
    origins = [
        stop for stop in stops
        if stop.get("stop_id") == departure.get("stop_id")
        and str(stop.get("run_ref")) == str(departure.get("run_ref"))
        and stop.get("scheduled_departure_utc") == departure.get("scheduled_departure_utc")
        and stop.get("departure_sequence") is not None
    ]
    return any(
        stop.get("stop_id") == dest_id
        and stop.get("departure_sequence") is not None
        and stop["departure_sequence"] > origin["departure_sequence"]
        for origin in origins for stop in stops
    )


class TrainTripFilter:
    """Share bounded, short-lived stopping patterns across favourite requests."""

    def __init__(self, client, ttl=300, max_entries=1024):
        self.client = client
        self.ttl = ttl
        self.max_entries = max_entries
        self._cache = OrderedDict()
        # Serialise lookups for each run while bounding total PTV concurrency.
        self._locks = [asyncio.Lock() for _ in range(32)]
        self._semaphore = asyncio.Semaphore(4)

    async def _pattern(self, route_type, departure):
        ref = departure.get("run_ref")
        date = departure.get("scheduled_departure_utc")
        if not ref or not date:
            return {}
        key = (route_type, str(ref), date)
        async with self._locks[hash(key) % len(self._locks)]:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < self.ttl:
                self._cache.move_to_end(key)
                return cached[1]
            async with self._semaphore:
                pattern = await self.client.get_pattern(route_type, ref, date)
            self._cache[key] = (time.monotonic(), pattern)
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
            return pattern

    async def filter(self, departures, dest_id, route_type, limit=3):
        async def matches(departure):
            try:
                pattern = await self._pattern(route_type, departure)
                return reaches_destination(pattern, departure, dest_id)
            except Exception as exc:
                # Do not log signed request URLs or guess from stale direction data.
                logger.warning("Stopping pattern unavailable for run %s (%s)",
                               departure.get("run_ref"), type(exc).__name__)
                return False

        selected = []
        for offset in range(0, len(departures), 4):
            batch = departures[offset:offset + 4]
            results = await asyncio.gather(*(matches(d) for d in batch))
            selected.extend(d for d, match in zip(batch, results) if match)
            if len(selected) >= limit:
                break
        return selected[:limit]
