import asyncio
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault('PTV_DEV_ID', 'test')
os.environ.setdefault('PTV_API_KEY', 'test')
from server.nearby import nearby_stops, find_departures, city_bound
from server.ptv_client import PTVClient
from server import api


class NearbyTests(unittest.IsolatedAsyncioTestCase):
    def test_city_bound_includes_tunnel_continuations_without_global_direction_ids(self):
        self.assertTrue(city_bound({'stop_id': 1233, 'route_type': 0},
            {'route_id': 14, 'direction_id': 1}, 'East Pakenham', {}))
        self.assertFalse(city_bound({'stop_id': 1233, 'route_type': 0},
            {'route_id': 14, 'direction_id': 14}, 'Sunbury', {}))
        self.assertFalse(city_bound({'stop_id': 1233, 'route_type': 1},
            {'route_id': 14, 'direction_id': 1}, 'Suburb', {}))
        for name in ('Flinders St', 'Town Hall Station', 'Southern Cross'):
            self.assertTrue(city_bound({'stop_id': 1, 'route_type': 0}, {}, name, {}))

    async def test_city_services_promoted_before_limit_with_proximity_preserved(self):
        client = AsyncMock()
        client.get_nearby_stops.return_value = {'stops': [
            {'stop_id': 99999, 'route_type': 0, 'stop_distance': 50}]}
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        client.get_departures.return_value = {
            'departures': [{'route_id': i, 'direction_id': i, 'run_ref': str(i),
                            'scheduled_departure_utc': future} for i in range(18)],
            'runs': {str(i): {'destination_name': 'Town Hall' if i == 17 else 'Suburb ' + str(i)}
                     for i in range(18)}}
        rows = (await find_departures(client, 0, 0))['departures']
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows[0]['destination'], 'Town Hall')
        self.assertTrue(all('_city_bound' not in row for row in rows))

    def test_modes_radius_duplicates_and_limit(self):
        stops = [{'stop_id': i, 'route_type': 0, 'stop_distance': i * 10} for i in range(30)]
        stops += [dict(stops[0]), {'stop_id': 40, 'route_type': 2, 'stop_distance': 1},
                  {'stop_id': 41, 'route_type': 1, 'stop_distance': 501},
                  {'stop_id': 42, 'route_type': 3, 'stop_distance': float('nan')}]
        result = nearby_stops({'stops': list(reversed(stops))})
        self.assertEqual([s['stop_id'] for s in result], list(range(16)))
        self.assertEqual(len(nearby_stops({'stops': [{'stop_id': 1, 'route_type': 3, 'stop_distance': 500}]})), 1)

    async def test_request_filters_all_three_modes_and_500_metres(self):
        client = PTVClient()
        client._request_json = AsyncMock(return_value={})
        await client.get_nearby_stops(-37.81, 144.96)
        query = parse_qs(urlsplit(client._request_json.call_args.args[0]).query)
        self.assertEqual(query['route_types'], ['0', '1', '3'])
        self.assertEqual(query['max_distance'], ['500'])
        self.assertEqual(query['max_results'], ['16'])

    async def test_distance_precedes_time_and_real_time_precedes_schedule(self):
        client = AsyncMock()
        client.get_nearby_stops.return_value = {'stops': [
            {'stop_id': 1, 'stop_name': 'Further', 'route_type': 0, 'stop_distance': 200},
            {'stop_id': 2, 'stop_name': 'Nearer', 'route_type': 1, 'stop_distance': 100}]}
        now = datetime.now(timezone.utc)
        later = (now + timedelta(minutes=10)).isoformat()
        soon = (now + timedelta(minutes=1)).isoformat()
        past = (now - timedelta(minutes=1)).isoformat()
        async def departures(mode, stop_id, **kwargs):
            d = {'route_id': 1, 'direction_id': 0, 'run_ref': '1', 'scheduled_departure_utc': past, 'estimated_departure_utc': later if stop_id == 2 else soon}
            return {'departures': [d, d, {'run_ref': 'expired', 'scheduled_departure_utc': past}],
                    'runs': {'1': {'destination_name': 'Destination'}}}
        client.get_departures.side_effect = departures
        result = await find_departures(client, -37.81, 144.96)
        self.assertEqual([d['stop_id'] for d in result['departures']], [2, 1])
        self.assertEqual(result['departures'][0]['departure_time'], later)
        self.assertEqual(result['departures'][0]['destination'], 'Destination')

    async def test_tracker_request_filters_route_and_zero_direction(self):
        client = PTVClient()
        client._request_json = AsyncMock(return_value={})
        await client.get_departures(0, 123, route_id='0', direction_id=0)
        url = urlsplit(client._request_json.call_args.args[0])
        self.assertEqual(url.path, '/v3/departures/route_type/0/stop/123/route/0')
        self.assertEqual(parse_qs(url.query)['direction_id'], ['0'])

    async def test_one_next_service_per_line_direction_at_nearest_stop(self):
        client = AsyncMock()
        client.get_nearby_stops.return_value = {'stops': [
            {'stop_id': i, 'route_type': 0, 'stop_distance': i * 100} for i in (1, 2)]}
        now = datetime.now(timezone.utc)
        async def departures(mode, stop_id, **kwargs):
            return {'departures': [
                {'route_id': route, 'direction_id': direction, 'run_ref': str(minutes),
                 'scheduled_departure_utc': (now + timedelta(minutes=minutes - stop_id)).isoformat()}
                for route, direction, minutes in [(1, 0, 10), (1, 0, 20), (1, 1, 15), (2, 0, 25)]]}
        client.get_departures.side_effect = departures
        rows = (await find_departures(client, 0, 0))['departures']
        self.assertEqual(len(rows), 3)
        self.assertEqual([r['stop_id'] for r in rows], [1, 1, 1])
        self.assertEqual(rows[0]['run_ref'], '10')
        self.assertEqual({(r['route_id'], r['direction_id']) for r in rows}, {(1, 0), (1, 1), (2, 0)})

    async def test_partial_failure_and_total_failure(self):
        client = AsyncMock()
        client.get_nearby_stops.return_value = {'stops': [{'stop_id': i, 'route_type': 0, 'stop_distance': i} for i in (1, 2)]}
        client.get_departures.side_effect = [TimeoutError(), {'departures': []}]
        result = await find_departures(client, 0, 0)
        self.assertTrue(result['partial'])
        client.get_departures.side_effect = TimeoutError()
        with self.assertRaises(RuntimeError):
            await find_departures(client, 0, 0)

    async def test_unique_destinations_across_lines_use_next_realtime_departure(self):
        client = AsyncMock()
        client.get_nearby_stops.return_value = {'stops': [
            {'stop_id': 1, 'stop_name': 'Parliament', 'route_type': 0, 'stop_distance': 50},
            {'stop_id': 2, 'stop_name': 'Further station', 'route_type': 0, 'stop_distance': 200}]}
        now = datetime.now(timezone.utc)
        async def departures(mode, stop_id, **kwargs):
            return {'departures': [
                {'route_id': i, 'direction_id': i, 'run_ref': str(i),
                 'scheduled_departure_utc': (now + timedelta(minutes=i)).isoformat(),
                 'estimated_departure_utc': (now + timedelta(minutes=minutes if stop_id == 1 else 1)).isoformat()}
                for i, minutes in [(1, 10), (2, 3), (3, 5), (4, 7)]],
                'runs': {'1': {'destination_name': 'Flinders Street'},
                         '2': {'destination_name': '  FLINDERS   STREET '},
                         '3': {'destination_name': 'Flinders Street'},
                         '4': {'destination_name': 'Sunbury'}}}
        client.get_departures.side_effect = departures
        rows = (await find_departures(client, 0, 0))['departures']
        self.assertEqual([(r['stop_id'], r['run_ref']) for r in rows], [(1, '2'), (1, '4')])
        self.assertEqual((rows[0]['route_id'], rows[0]['direction_id']), (2, 2))

    async def test_large_results_capped_at_16_and_concurrency_at_four(self):
        client = AsyncMock()
        client.get_nearby_stops.return_value = {'stops': [{'stop_id': i, 'route_type': 1, 'stop_distance': i} for i in range(16)]}
        active = peak = 0
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        async def departures(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return {'departures': [{'route_id': i, 'direction_id': 0, 'run_ref': str(i), 'scheduled_departure_utc': future} for i in range(20)]}
        client.get_departures.side_effect = departures
        self.assertEqual(len((await find_departures(client, 0, 0))['departures']), 16)
        self.assertLessEqual(peak, 4)


class NearbyEndpointTests(unittest.TestCase):
    def test_tracker_filters_route_and_direction_and_keeps_three(self):
        from fastapi.testclient import TestClient
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        departures = [{'route_id': route, 'direction_id': direction, 'run_ref': str(i),
            'scheduled_departure_utc': (future + timedelta(minutes=i)).isoformat()}
            for i, (route, direction) in enumerate([(1, 0), (2, 0), (1, 1), (1, 0), (1, 0), (1, 0)])]
        with patch.object(api.ptv_client, 'get_departures', AsyncMock(return_value={'departures': departures})) as fetch:
            result = TestClient(api.app).post('/api/v1/nearby/tracker', json={
                'stop_id': 1, 'route_type': 0, 'route_id': 1, 'direction_id': 0, 'client_id': 'test-tracker'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual([d['run_ref'] for d in result.json()['departures']], ['0', '3', '4'])
        self.assertEqual(fetch.call_args.kwargs['direction_id'], 0)
        self.assertEqual(fetch.call_args.kwargs['route_id'], '1')

    def test_location_validation_and_response(self):
        from fastapi.testclient import TestClient
        client = TestClient(api.app)
        result = client.post('/api/v1/nearby', json={'latitude': 91, 'longitude': 0, 'client_id': 'test-nearby'})
        self.assertEqual(result.status_code, 422)
        with patch.object(api, 'find_departures', AsyncMock(return_value={'departures': [], 'partial': False, 'radius_m': 500})):
            result = client.post('/api/v1/nearby', json={'latitude': -37.81, 'longitude': 144.96, 'client_id': 'test-nearby'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['departures'], [])
