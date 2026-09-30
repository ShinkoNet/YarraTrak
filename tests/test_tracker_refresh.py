import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault('PTV_DEV_ID', 'test')
os.environ.setdefault('PTV_API_KEY', 'test')
from server import api


class TrackerRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_trams_are_sorted_by_estimate(self):
        now = datetime.now(timezone.utc)
        departures = [{'run_ref': ref, 'route_id': 1, 'direction_id': 0,
            'scheduled_departure_utc': (now + timedelta(minutes=scheduled)).isoformat(),
            'estimated_departure_utc': (now + timedelta(minutes=estimated)).isoformat()}
            for ref, scheduled, estimated in [('A', 5, 20), ('B', 10, 10)]]
        with patch.object(api, '_resolve_allowed_trip_pairs', return_value={(1, 0)}):
            rows = await api._filter_favourite_departures(departures, 1, 2, 1, 0)
        self.assertEqual([d['run_ref'] for d in rows], ['B', 'A'])

    async def test_passed_service_refills_buffer_before_cache_ttl(self):
        now = datetime.now(timezone.utc)
        key = (1, 1, 0, None, 3)
        cached = {'fetched_at': now.timestamp(), 'departures': [
            {'departure_time': (now - timedelta(seconds=1)).isoformat()}]}
        fresh = [{'run_ref': str(i), 'route_id': 1, 'direction_id': 0,
            'scheduled_departure_utc': (now + timedelta(minutes=i)).isoformat()} for i in (1, 2, 3)]
        with patch.dict(api._departure_cache, {key: cached}, clear=True), patch.object(
                api.ptv_client, 'get_departures', AsyncMock(return_value={'departures': fresh})) as fetch:
            result = await api.fetch_departure_for_button(1, 1, 0)
            self.assertEqual([d['run_ref'] for d in result['departures']], ['1', '2', '3'])
            self.assertEqual(fetch.await_count, 1)
            await api.fetch_departure_for_button(1, 1, 0)
            self.assertEqual(fetch.await_count, 1)
