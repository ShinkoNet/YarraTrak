import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("PTV_DEV_ID", "test")
os.environ.setdefault("PTV_API_KEY", "test")

from server.trip_filter import TrainTripFilter, reaches_destination
from server.ptv_client import PTVClient
from server import api

FIXTURE = json.loads((Path(__file__).parent / "fixtures/parkville.json").read_text())
DEPARTURES = {d['run_ref']: d for d in FIXTURE['departures']}


class StoppingPatternTests(unittest.TestCase):
    def test_live_east_pakenham_continuation(self):
        self.assertTrue(reaches_destination(FIXTURE['patterns']['990116'], DEPARTURES['990116'], 1139))

    def test_live_cranbourne_does_not_reach_narre_warren(self):
        self.assertFalse(reaches_destination(FIXTURE['patterns']['990114'], DEPARTURES['990114'], 1139))

    def test_live_sunbury_already_passed_narre_warren(self):
        self.assertFalse(reaches_destination(FIXTURE['patterns']['990115'], DEPARTURES['990115'], 1139))

    def test_live_watergardens_does_not_reach_narre_warren(self):
        self.assertFalse(reaches_destination(FIXTURE['patterns']['990507'], DEPARTURES['990507'], 1139))

    def test_same_stop_is_not_a_trip(self):
        self.assertFalse(reaches_destination(FIXTURE['patterns']['990116'], DEPARTURES['990116'], 1233))

    def test_reverse_trip_reaches_parkville(self):
        pattern = FIXTURE['patterns']['990115']
        narre = next(d for d in pattern['departures'] if d['stop_id'] == 1139)
        self.assertTrue(reaches_destination(pattern, narre, 1233))

    def test_wrong_service_date_is_rejected(self):
        departure = dict(DEPARTURES['990116'], scheduled_departure_utc='2026-10-01T09:28:00Z')
        self.assertFalse(reaches_destination(FIXTURE['patterns']['990116'], departure, 1139))

    def test_pattern_order_does_not_depend_on_array_order(self):
        pattern = {'departures': list(reversed(FIXTURE['patterns']['990116']['departures']))}
        self.assertTrue(reaches_destination(pattern, DEPARTURES['990116'], 1139))

    def test_short_running_or_express_train_is_excluded(self):
        pattern = deepcopy(FIXTURE['patterns']['990116'])
        pattern['departures'] = [d for d in pattern['departures'] if d['stop_id'] != 1139]
        self.assertFalse(reaches_destination(pattern, DEPARTURES['990116'], 1139))


class FilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock = patch.object(api, 'datetime', wraps=datetime)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.clock.now.return_value = datetime(2026, 9, 30, 9, 10, tzinfo=timezone.utc)
        async def pattern(route_type, ref, date):
            return FIXTURE['patterns'][ref]
        self.client = AsyncMock()
        self.client.get_pattern.side_effect = pattern
        self.filter = TrainTripFilter(self.client)

    async def test_live_departures_only_select_platform_two_pakenham(self):
        result = await self.filter.filter(FIXTURE['departures'], 1139, 0)
        self.assertEqual([d['run_ref'] for d in result], ['990116'])
        self.assertEqual(result[0]['platform_number'], '2')

    async def test_pattern_cache_shared_between_destinations(self):
        await self.filter.filter(FIXTURE['departures'], 1139, 0)
        count = self.client.get_pattern.call_count
        await self.filter.filter(FIXTURE['departures'], 1045, 0)
        self.assertEqual(self.client.get_pattern.call_count, count)

    async def test_concurrent_requests_share_pattern(self):
        await asyncio.gather(*(self.filter.filter([DEPARTURES['990116']], 1139, 0) for _ in range(5)))
        self.assertEqual(self.client.get_pattern.call_count, 1)

    async def test_error_does_not_fall_back_to_wrong_direction_or_poison_cache(self):
        original = self.client.get_pattern.side_effect
        self.client.get_pattern.side_effect = TimeoutError()
        with self.assertLogs('server.trip_filter', level='WARNING'):
            with self.assertRaises(RuntimeError):
                await self.filter.filter([DEPARTURES['990116']], 1139, 0)
        self.client.get_pattern.side_effect = original
        self.assertEqual(len(await self.filter.filter([DEPARTURES['990116']], 1139, 0)), 1)

    async def test_cache_expiry_and_size_bound(self):
        self.filter = TrainTripFilter(self.client, ttl=0, max_entries=2)
        await self.filter.filter(FIXTURE['departures'], 1139, 0)
        self.assertLessEqual(len(self.filter._cache), 2)
        count = self.client.get_pattern.call_count
        await self.filter.filter([DEPARTURES['990116']], 1139, 0)
        self.assertEqual(self.client.get_pattern.call_count, count + 1)

    async def test_existing_wrong_saved_direction_cannot_override_destination(self):
        with patch.object(api, '_train_trip_filter', self.filter):
            result = await api._filter_favourite_departures(FIXTURE['departures'], 1233, 1139, 0, 14)
        self.assertEqual([d['run_ref'] for d in result], ['990116'])

    async def test_empty_static_routes_still_allow_live_through_service(self):
        with patch.object(api, '_train_trip_filter', self.filter), patch.object(api.tools, 'resolve_trip_patterns', return_value=[]):
            result = await api._filter_favourite_departures(FIXTURE['departures'], 1233, 1139, 0, 14)
        self.assertEqual([d['run_ref'] for d in result], ['990116'])

    async def test_expired_departures_do_not_fill_result_limit(self):
        departures = [DEPARTURES['990116']]
        self.clock.now.return_value = datetime(2026, 9, 30, 9, 30, tzinfo=timezone.utc)
        with patch.object(api, '_train_trip_filter', self.filter):
            result = await api._filter_favourite_departures(departures, 1233, 1139, 0, 14)
        self.assertEqual(result, [])
        self.client.get_pattern.assert_not_called()

    async def test_countdown_uses_actual_continuation_with_old_saved_direction(self):
        api._departure_cache.clear()
        with patch.object(api, '_train_trip_filter', self.filter), patch.object(api.ptv_client, 'get_departures', AsyncMock(return_value={'departures': FIXTURE['departures']})):
            result = await api.fetch_departure_for_button(1233, 0, 14, 1139)
        self.assertEqual([d['run_ref'] for d in result['departures']], ['990116'])
        self.assertEqual(result['departures'][0]['minutes'], 18)
        api._departure_cache.clear()

    async def test_http_favourite_uses_same_continuation(self):
        from starlette.requests import Request
        request = Request({'type': 'http', 'headers': [], 'client': ('127.0.0.1', 1234)})
        with patch.object(api, '_train_trip_filter', self.filter), patch.object(api.ptv_client, 'get_departures', AsyncMock(return_value={'departures': FIXTURE['departures']})):
            result = await api.favourite_departure(api.FavouriteRequest(button_id=1, stop_id=1233, dest_id=1139, direction_id=14, client_id='test-trip-filter'), request)
        self.assertEqual(result['message'], 'Next train in 18 min')

    async def test_direction_only_favourite_keeps_existing_behavior(self):
        result = await api._filter_favourite_departures(FIXTURE['departures'], 1233, None, 0, 14)
        self.assertTrue(result)
        self.assertTrue(all(d['direction_id'] == 14 for d in result))
        self.client.get_pattern.assert_not_called()

    async def test_tram_still_uses_route_and_direction(self):
        departures = [{'route_id': 1, 'direction_id': 0}, {'route_id': 2, 'direction_id': 0}]
        with patch.object(api, '_resolve_allowed_trip_pairs', return_value={(1, 0)}):
            result = await api._filter_favourite_departures(departures, 1, 2, 1, 0)
        self.assertEqual(result, departures[:1])

    async def test_pattern_request_includes_advertised_continuation(self):
        client = PTVClient()
        client._request_json = AsyncMock(return_value={})
        await client.get_pattern(0, '990116', '2026-09-30T09:28:00Z')
        query = parse_qs(urlsplit(client._request_json.call_args.args[0]).query)
        self.assertEqual(query['include_advertised_interchange'], ['true'])
        self.assertEqual(query['expand'], ['Run'])
        self.assertEqual(query['include_skipped_stops'], ['false'])
        self.assertEqual(query['date_utc'], ['2026-09-30T09:28:00Z'])


class WebSocketTests(unittest.TestCase):
    def test_quick_check_uses_same_continuation(self):
        from fastapi.testclient import TestClient
        client = AsyncMock()
        client.get_pattern.side_effect = lambda route_type, ref, date: FIXTURE['patterns'][ref]
        with patch.object(api, '_train_trip_filter', TrainTripFilter(client)), patch.object(api.ptv_client, 'get_departures', AsyncMock(return_value={'departures': FIXTURE['departures']})), patch.object(api, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 30, 9, 10, tzinfo=timezone.utc)
            with TestClient(api.app).websocket_connect('/ws?client_id=test-websocket-trip') as ws:
                self.assertEqual(ws.receive_json()['type'], 'connected')
                ws.send_json({'type': 'favourite', 'id': 'test', 'stop_id': 1233, 'dest_id': 1139, 'direction_id': 14, 'route_type': 0})
                result = ws.receive_json()
                self.assertEqual(result['type'], 'favourite_result')
                self.assertEqual(result['message'], '18 min • P2')


if __name__ == '__main__':
    unittest.main()
