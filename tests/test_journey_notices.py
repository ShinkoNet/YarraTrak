"""Regressions captured while comparing 26 journeys with the official planner."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault('PTV_DEV_ID', 'test')
os.environ.setdefault('PTV_API_KEY', 'test')
from server import api
from server.trip_filter import TrainTripFilter, regional_route_variant, trip_stops

FIXTURES = Path(__file__).parent / 'fixtures'
VLINE = json.loads((FIXTURES / 'vline.json').read_text())
WORKS = json.loads((FIXTURES / 'nightly_works.json').read_text())
BY_REF = {d['run_ref']: d for d in VLINE['departures']}


class RegionalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = AsyncMock()
        self.client.get_pattern.side_effect = lambda mode, ref, date: VLINE['patterns'][str(ref)]
        self.filter = TrainTripFilter(self.client)

    async def test_variants_do_not_fill_three_departure_slots(self):
        departures = sorted(VLINE['departures'], key=lambda d: d['scheduled_departure_utc'])
        result = await self.filter.filter(departures, 1527, 3)
        self.assertEqual([d['run_ref'] for d in result], ['17988', '17990'])

    async def test_geelong_does_not_select_deer_park_short_run(self):
        with patch.object(api, 'datetime', wraps=datetime) as clock, patch.object(api, '_train_trip_filter', self.filter):
            clock.now.return_value = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
            result = await api._filter_favourite_departures(VLINE['departures'], 1181, 1527, 3, None)
        self.assertEqual([d['run_ref'] for d in result], ['17990'])

    async def test_short_run_remains_valid_for_its_destination(self):
        result = await self.filter.filter([BY_REF['17989']], 1052, 3)
        self.assertEqual([d['run_ref'] for d in result], ['17989'])

    def test_compatible_variants_and_distinct_services(self):
        first, second = BY_REF['17988'], BY_REF['64080']
        a, b = [trip_stops(VLINE['patterns'][d['run_ref']], d, 1527) for d in (first, second)]
        self.assertTrue(regional_route_variant(first, a, second, b))
        for modified in (dict(second, route_id=first['route_id']),
                         dict(second, scheduled_departure_utc='2026-09-30T10:35:00Z'),
                         dict(second, platform_number='3')):
            self.assertFalse(regional_route_variant(dict(first, platform_number='2'), a, modified, b))
        other_path = deepcopy(b)
        other_path[1]['stop_id'] = 999999
        self.assertFalse(regional_route_variant(first, a, second, other_path))
        other_path = deepcopy(b)
        other_path[1]['scheduled_departure_utc'] = '2026-09-30T10:09:00Z'
        self.assertFalse(regional_route_variant(first, a, second, other_path))

    async def test_empty_pattern_can_be_retried(self):
        self.client.get_pattern.side_effect = None
        self.client.get_pattern.return_value = {}
        with self.assertLogs('server.trip_filter', level='WARNING'), self.assertRaises(RuntimeError):
            await self.filter.filter([BY_REF['17988']], 1527, 3)
        self.client.get_pattern.return_value = VLINE['patterns']['17988']
        self.assertEqual(len(await self.filter.filter([BY_REF['17988']], 1527, 3)), 1)


class NoticeTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(api, 'datetime', wraps=datetime)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.clock.now.return_value = datetime(2026, 9, 30, 9, 40, tzinfo=timezone.utc)

    def test_current_nightly_works_keep_start_time_and_unknown_scope(self):
        expected = {368764: 'Line works 11:50pm', 369522: 'Line works 8:30pm'}
        for disruption in WORKS:
            route = disruption['routes'][0]['route_id']
            dep = {'route_id': route, 'direction_id': 0, 'disruption_ids': [disruption['disruption_id']]}
            with patch.object(api, '_extract_disruption_station_range', return_value=None):
                label = api._resolve_disruption_label(disruption, [dep], 1162, 1126, 0)
            self.assertEqual(label, expected[disruption['disruption_id']])
            self.assertGreater(api._label_priority(label), api._label_priority('Minor Delays 10m'))

    def test_expired_notices_are_hidden_even_if_marked_current(self):
        disruption = dict(WORKS[0], to_date='2026-09-30T09:00:00Z')
        self.assertIsNone(api._resolve_disruption_label(disruption, [], 1, 2, 0))

    def test_future_nightly_notice_is_not_presented_as_tonight(self):
        disruption = dict(WORKS[0], from_date='2026-10-04T10:00:00Z')
        self.assertIsNone(api._planned_timed_disruption_label('Line works', disruption))

    def test_known_works_outside_trip_are_hidden(self):
        disruption = WORKS[0]
        dep = {'route_id': 17, 'direction_id': 0, 'disruption_ids': [disruption['disruption_id']]}
        with patch.object(api, '_extract_disruption_station_range', return_value=(1, 3)), patch.object(api, '_journey_disruption_scope', return_value=None):
            self.assertIsNone(api._resolve_disruption_label(disruption, [dep], 1, 2, 0))


class EmptyResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_direct_service_and_api_failure_have_different_messages(self):
        api._departure_cache.clear()
        self.addCleanup(api._departure_cache.clear)
        for error, expected in ((None, 'No direct departures'), (RuntimeError(), 'Unavailable')):
            api._departure_cache.clear()
            with patch.object(api.ptv_client, 'get_departures', AsyncMock(return_value={'departures': [BY_REF['17988']]})), patch.object(api, '_filter_favourite_departures', AsyncMock(return_value=[], side_effect=error)):
                result = await api.fetch_departure_for_button(1181, 3, None, 1527)
            self.assertEqual(result['message'], expected)
            self.assertEqual(result['departures'], [])

    def test_websocket_push_forwards_empty_message(self):
        from fastapi.testclient import TestClient
        with patch.object(api, 'fetch_departure_for_button', AsyncMock(return_value={'departures': [], 'message': 'No direct departures'})):
            with TestClient(api.app).websocket_connect('/ws?client_id=test-notice') as ws:
                self.assertEqual(ws.receive_json()['type'], 'connected')
                ws.send_json({'type': 'subscribe_favourites', 'buttons': [{'button_id': 1, 'stop_id': 1181, 'dest_id': 1527, 'route_type': 3}]})
                for _ in range(3):
                    result = ws.receive_json()
                    if result['type'] == 'favourite_update':
                        self.assertEqual(result['updates'][0]['message'], 'No direct departures')
                        break
                else:
                    self.fail('No favourite update received')
