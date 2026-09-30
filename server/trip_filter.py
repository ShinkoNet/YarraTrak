"""Select train departures by their actual stops, including advertised continuations."""

import asyncio
from collections import OrderedDict
import logging
import time

logger = logging.getLogger(__name__)


def trip_stops(pattern: dict, departure: dict, dest_id: int) -> list[dict]:
    """Require the destination after this boarding event, not in a feeder run."""
    if departure.get("stop_id") == dest_id:
        return []
    stops = pattern.get("departures", [])
    origins = [
        stop for stop in stops
        if stop.get("stop_id") == departure.get("stop_id")
        and str(stop.get("run_ref")) == str(departure.get("run_ref"))
        and stop.get("scheduled_departure_utc") == departure.get("scheduled_departure_utc")
        and stop.get("departure_sequence") is not None
    ]
    for origin in origins:
        onward = sorted((stop for stop in stops
                         if stop.get("departure_sequence") is not None
                         and stop["departure_sequence"] >= origin["departure_sequence"]),
                        key=lambda stop: stop["departure_sequence"])
        for index, stop in enumerate(onward):
            if stop.get("stop_id") == dest_id and stop["departure_sequence"] > origin["departure_sequence"]:
                return onward[:index + 1]
    return []


def reaches_destination(pattern: dict, departure: dict, dest_id: int) -> bool:
    return bool(trip_stops(pattern, departure, dest_id))


def regional_route_variant(first, first_stops, second, second_stops):
    """Identify compatible V/Line route variants for the same boarding time.

    Coach itineraries can publish a sparse copy of their connecting train,
    with arrival rather than departure time at the interchange destination.
    Require matching common intermediate times and an ordered stop subset.
    """
    if first.get("route_id") == second.get("route_id"):
        return False
    if any(first.get(key) != second.get(key) for key in ("stop_id", "scheduled_departure_utc")):
        return False
    platforms = [str(d.get("platform_number") or "").strip() for d in (first, second)]
    if all(platforms) and platforms[0] != platforms[1]:
        return False
    shorter, longer = sorted((first_stops, second_stops), key=len)
    cursor = 0
    for index, stop in enumerate(shorter):
        while cursor < len(longer) and longer[cursor].get("stop_id") != stop.get("stop_id"):
            cursor += 1
        if cursor == len(longer):
            return False
        if index < len(shorter) - 1 and stop.get("scheduled_departure_utc") != longer[cursor].get("scheduled_departure_utc"):
            return False
        cursor += 1
    return True


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
            if not pattern.get("departures"):
                raise ValueError("Empty stopping pattern")
            self._cache[key] = (time.monotonic(), pattern)
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
            return pattern

    async def filter(self, departures, dest_id, route_type, limit=3):
        async def matches(departure):
            try:
                pattern = await self._pattern(route_type, departure)
                if not pattern.get("departures"):
                    raise ValueError("Empty stopping pattern")
                return trip_stops(pattern, departure, dest_id)
            except Exception as exc:
                # Do not log signed request URLs or guess from stale direction data.
                logger.warning("Stopping pattern unavailable for run %s (%s)",
                               departure.get("run_ref"), type(exc).__name__)
                return None

        selected = []
        paths = []
        failed = False
        for offset in range(0, len(departures), 4):
            batch = departures[offset:offset + 4]
            results = await asyncio.gather(*(matches(d) for d in batch))
            for departure, path in zip(batch, results):
                failed |= path is None
                if not path:
                    continue
                duplicate = any(
                    (str(departure.get("run_ref")) == str(prior.get("run_ref"))
                     and departure.get("scheduled_departure_utc") == prior.get("scheduled_departure_utc"))
                    or (route_type == 3 and regional_route_variant(prior, prior_path, departure, path))
                    for prior, prior_path in zip(selected, paths)
                )
                if not duplicate:
                    selected.append(departure)
                    paths.append(path)
            if len(selected) >= limit:
                break
        if not selected and failed:
            raise RuntimeError("Stopping patterns temporarily unavailable")
        return selected[:limit]
