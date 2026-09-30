import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault('PTV_DEV_ID', 'test')
os.environ.setdefault('PTV_API_KEY', 'test')
from fastapi.testclient import TestClient
from server import api
from server.ws_models import validate_message, validate_buttons


class ValidationTests(unittest.TestCase):
    def setUp(self):
        for store in (api._ws_connect_limiters, api._ws_message_limiters):
            store.clear()

    def test_reconnecting_with_url_buttons_obeys_request_limit(self):
        with patch.dict(api._http_favourite_limiters, {}, clear=True), patch.object(
                api, 'HTTP_FAVOURITE_RATE_LIMIT', 1), patch.object(
                api, 'fetch_departure_for_button', AsyncMock(return_value={'departures': []})) as fetch:
            client = TestClient(api.app)
            with client.websocket_connect('/ws?client_id=url-first&buttons=1:1:0') as ws:
                self.assertEqual(ws.receive_json()['type'], 'connected')
                self.assertEqual(ws.receive_json()['type'], 'favourite_update')
            with client.websocket_connect('/ws?client_id=url-second&buttons=1:2:0') as ws:
                self.assertEqual(ws.receive_json()['type'], 'connected')
                self.assertEqual(ws.receive_json()['type'], 'error')
            self.assertEqual(fetch.await_count, 1)

    def test_invalid_message_flood_is_disconnected(self):
        from starlette.websockets import WebSocketDisconnect
        with patch.object(api, 'WS_MESSAGE_RATE_LIMIT', 2):
            with TestClient(api.app).websocket_connect('/ws?client_id=flood') as ws:
                ws.receive_json()
                for _ in range(2):
                    ws.send_json([])
                    self.assertEqual(ws.receive_json()['type'], 'error')
                ws.send_json([])
                with self.assertRaises(WebSocketDisconnect) as exc: ws.receive_json()
                self.assertEqual(exc.exception.code, 1008)

    def test_rate_tables_are_bounded_without_resetting_live_quotas(self):
        from collections import defaultdict, deque
        store = defaultdict(deque)
        with patch.object(api, 'MAX_RATE_LIMIT_KEYS', 2), patch.object(api.time, 'time', return_value=100):
            self.assertTrue(api._check_rate_limit(store, 'a', 1))
            self.assertTrue(api._check_rate_limit(store, 'b', 1))
            self.assertFalse(api._check_rate_limit(store, 'c', 1))
            self.assertFalse(api._check_rate_limit(store, 'a', 1))
        with patch.object(api, 'MAX_RATE_LIMIT_KEYS', 2), patch.object(api.time, 'time', return_value=161):
            self.assertTrue(api._check_rate_limit(store, 'c', 1))
        self.assertLessEqual(len(store), 2)

    def test_runtime_caches_are_bounded(self):
        store = {}
        with patch.object(api, 'MAX_RUNTIME_CACHE_KEYS', 2):
            for i in range(4): api._bounded_cache_put(store, i, i)
        self.assertEqual(store, {2: 2, 3: 3})

    def test_client_id_cannot_evict_another_source_ip(self):
        self.assertNotEqual(api._client_scope_key('192.0.2.1', 'same-id'),
                            api._client_scope_key('192.0.2.2', 'same-id'))

    def test_reconnect_attempts_are_limited_across_client_ids(self):
        from starlette.websockets import WebSocketDisconnect
        with patch.object(api, 'WS_CONNECT_RATE_LIMIT', 1):
            client = TestClient(api.app)
            with client.websocket_connect('/ws?client_id=connect-first') as ws:
                self.assertEqual(ws.receive_json()['type'], 'connected')
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/ws?client_id=connect-second'): pass

    def test_malformed_buttons_rejected(self):
        for value in ({'x': 1}, [1], '123', True, -1, 2**31):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_buttons([{'button_id': 1, 'stop_id': value}])
        for rows in ([{'button_id': 1, 'stop_id': 1}] * 2, [{}] * 11, None):
            with self.assertRaises(ValueError): validate_buttons(rows)

    def test_valid_watch_and_actual_phone_query_history(self):
        msg = validate_message({'type': 'watch_start', 'run_ref': '123', 'stop_id': 1,
                                'route_type': 0, 'route_id': '0', 'direction_id': 0})
        self.assertEqual(msg['route_id'], 0)
        self.assertEqual(msg['run_ref'], 123)
        msg = validate_message({'type': 'query', 'text': 'next tram', 'query_history': [
            {'text': 'next train', 'at': 1000},
            {'text': 'tram', 'at': 1001, 'stop_id': 1, 'stop_name': 'Stop', 'route_type': 1}]})
        self.assertEqual(len(msg['query_history']), 1)

    def test_invalid_message_keeps_connection_usable(self):
        with TestClient(api.app).websocket_connect('/ws?client_id=validation-test') as ws:
            self.assertEqual(ws.receive_json()['type'], 'connected')
            for payload in ([], {'type': 'subscribe_favourites', 'buttons': [{'button_id': 1, 'stop_id': {'bad': 1}}]},
                            {'type': 'watch_start', 'run_ref': '1', 'stop_id': [1]}):
                ws.send_json(payload)
                self.assertEqual(ws.receive_json()['type'], 'error')
                ws.send_json({'type': 'ping', 'id': 'health'})
                self.assertEqual(ws.receive_json()['type'], 'pong')

    def test_request_limit_survives_a_different_client_id(self):
        with patch.dict(api._http_favourite_limiters, {}, clear=True), patch.object(
                api, 'HTTP_FAVOURITE_RATE_LIMIT', 1), patch.object(
                api.ptv_client, 'get_departures', AsyncMock(return_value={'departures': []})) as fetch:
            client = TestClient(api.app)
            for client_id, expected in [('limit-first', 'favourite_result'), ('limit-second', 'error')]:
                with client.websocket_connect('/ws?client_id=' + client_id) as ws:
                    ws.receive_json()
                    ws.send_json({'type': 'favourite', 'stop_id': 1})
                    self.assertEqual(ws.receive_json()['type'], expected)
            self.assertEqual(fetch.await_count, 1)

    def test_empty_subscription_removes_previous_favourites(self):
        with patch.object(api, 'fetch_departure_for_button', AsyncMock(return_value={'departures': []})):
            with TestClient(api.app).websocket_connect('/ws?client_id=unsubscribe-test') as ws:
                ws.receive_json()
                ws.send_json({'type': 'subscribe_favourites', 'buttons': [{'button_id': 1, 'stop_id': 1}]})
                while ws.receive_json()['type'] != 'favourites_subscribed': pass
                ws.send_json({'type': 'subscribe_favourites', 'buttons': []})
                while True:
                    reply = ws.receive_json()
                    if reply['type'] == 'favourites_subscribed': break
                self.assertEqual(reply['buttons'], 0)
                self.assertFalse(any(api._ws_client_ids.get(sock) == 'unsubscribe-test'
                                     for sock in api._favourite_subscriptions))


class Socket:
    client_state = api.WebSocketState.CONNECTED
    application_state = api.WebSocketState.CONNECTED
    async def send_json(self, data): pass


class ServerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_quota_is_shared_by_source_ip(self):
        created = []
        try:
            with patch.object(api, 'MAX_WS_CONNECTIONS_PER_IP', 2):
                for i, ip, expected in [(1, '192.0.2.1', True), (2, '192.0.2.1', True),
                                        (3, '192.0.2.1', False), (4, '192.0.2.2', True)]:
                    ws = Socket(); created.append(ws)
                    result = api._register_websocket_connection(ws, ip, f'test-{i}', f'client:test-{i}')
                    self.assertEqual(result, expected)
                api._cleanup_websocket_state(created[0])
                ws = Socket(); created.append(ws)
                self.assertTrue(api._register_websocket_connection(ws, '192.0.2.1', 'test-5', 'client:test-5'))
        finally:
            for ws in created: api._cleanup_websocket_state(ws)

    async def test_poisoned_registry_does_not_stop_other_subscribers(self):
        bad, good = Socket(), Socket()
        received = asyncio.Event()
        async def send(ws, updates):
            if ws is good: received.set()
        with patch.dict(api._favourite_subscriptions, {
                bad: [{'button_id': 1, 'stop_id': {'bad': 1}}],
                good: [{'button_id': 1, 'stop_id': 1}]}, clear=True), patch.object(
                api, 'FAVOURITE_BROADCAST_INTERVAL', .001), patch.object(
                api, 'fetch_departure_for_button', AsyncMock(return_value={'departures': []})), patch.object(
                api, '_send_favourite_updates', side_effect=send):
            task = asyncio.create_task(api.broadcast_favourite_updates())
            try:
                await asyncio.wait_for(received.wait(), 1)
                self.assertFalse(task.done())
                self.assertNotIn(bad, api._favourite_subscriptions)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
