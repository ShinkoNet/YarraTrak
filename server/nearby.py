"""Bounded nearby departure lookup for the watch's Quick Actions menu."""
import asyncio
from datetime import datetime, timezone
import math


def nearby_stops(data):
    stops = {}
    for stop in data.get('stops', []):
        distance = stop.get('stop_distance')
        mode = stop.get('route_type')
        if mode not in (0, 1, 3) or not isinstance(distance, (int, float)):
            continue
        if not math.isfinite(distance) or not 0 <= distance <= 500:
            continue
        key = (mode, stop.get('stop_id'))
        if key[1] is not None and (key not in stops or distance < stops[key]['stop_distance']):
            stops[key] = stop
    return sorted(stops.values(), key=lambda s: (s['stop_distance'], s['route_type'], s['stop_id']))[:16]


async def find_departures(client, latitude, longitude):
    stops = nearby_stops(await client.get_nearby_stops(latitude, longitude))
    semaphore = asyncio.Semaphore(4)
    now = datetime.now(timezone.utc)

    async def fetch(stop):
        async with semaphore:
            data = await client.get_departures(stop['route_type'], stop['stop_id'], max_results=3, expand=['Run', 'Direction', 'Route'])
        rows = []
        seen = set()
        for departure in data.get('departures', []):
            value = departure.get('estimated_departure_utc') or departure.get('scheduled_departure_utc')
            if not value:
                continue
            try:
                when = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if when <= now:
                    continue
            except (ValueError, TypeError):
                continue
            ref = str(departure.get('run_ref', ''))
            key = (ref, value)
            if key in seen:
                continue
            seen.add(key)
            run = data.get('runs', {}).get(ref, {})
            direction = data.get('directions', {}).get(str(departure.get('direction_id')), {})
            route = data.get('routes', {}).get(str(departure.get('route_id')), {})
            destination = run.get('destination_name') or direction.get('direction_name') or route.get('route_name') or 'Departure'
            rows.append({
                'stop_id': stop['stop_id'], 'stop_name': stop.get('stop_name', 'Stop').strip(),
                'route_type': stop['route_type'], 'distance_m': stop['stop_distance'],
                'route_id': departure.get('route_id'), 'direction_id': departure.get('direction_id'),
                'destination': destination, 'route_number': route.get('route_number') or '',
                'departure_time': when.isoformat(), 'run_ref': ref,
                'platform': str(departure.get('platform_number') or ''),
            })
        return rows

    results = await asyncio.gather(*(fetch(s) for s in stops), return_exceptions=True)
    failures = sum(isinstance(result, Exception) for result in results)
    if stops and failures == len(stops):
        raise RuntimeError('Nearby departures unavailable')
    rows = [row for result in results if not isinstance(result, Exception) for row in result]
    rows.sort(key=lambda row: (row['distance_m'], row['departure_time'], row['stop_id'], row['run_ref']))
    services = {}
    for row in rows:
        key = (row['route_type'], row['route_id'], row['direction_id'])
        if None in key:
            continue
        # Rows are ordered by distance then time: keep the next service at
        # the nearest boarding stop, not more runs or stops of the same line.
        if key in services:
            continue
        services[key] = row
        row['distance_m'] = round(row['distance_m'])
    return {'departures': list(services.values())[:16], 'partial': bool(failures), 'radius_m': 500}
