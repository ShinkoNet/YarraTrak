from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import WebSocketDisconnect

from server import api


REQUIRED_METRIC_FIELDS = {
    "timestamp",
    "subscribers",
    "unique_keys",
    "active_clients",
    "known_clients",
    "broadcast_loops",
    "avg_loop_ms",
    "max_loop_ms",
    "cache_hits",
    "cache_misses",
    "cache_hit_rate",
    "upstream_rps",
    "ptv_departures",
    "ptv_runs",
    "ptv_directions",
    "ptv_search",
}


def _metric_point(index: int) -> dict[str, int | float | str]:
    point = api._empty_metrics_snapshot(
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    )
    point.update(
        subscribers=index,
        unique_keys=index + 1,
        active_clients=max(0, index - 1),
        known_clients=index + 2,
        broadcast_loops=index + 2,
        avg_loop_ms=round(10.0 + index / 10.0, 2),
        max_loop_ms=round(15.0 + index / 5.0, 2),
        cache_hits=index * 2,
        cache_misses=index,
        cache_hit_rate=round(66.6, 2),
        upstream_rps=round(index / 60.0, 3),
        ptv_departures=index,
        ptv_runs=index + 3,
        ptv_directions=index + 4,
        ptv_search=index + 5,
    )
    return point


@pytest.fixture
def reset_state():
    api._favourite_subscriptions.clear()
    api._departure_cache.clear()
    api._watch_tasks.clear()
    api._ws_connections_by_scope.clear()
    api._ws_client_ips.clear()
    api._ws_connection_scopes.clear()
    api._ws_client_ids.clear()
    api._client_activity.clear()
    api._ws_query_limiters.clear()
    api._metrics_history.clear()
    api._latest_metrics_snapshot = api._empty_metrics_snapshot()
    api._metrics_window["last_subscribers"] = 0
    api._metrics_window["last_unique_keys"] = 0
    api._metrics_window["last_active_clients"] = 0
    yield


async def _request(path: str, host: str, follow_redirects: bool = True):
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=follow_redirects,
    ) as client:
        return await client.get(path, headers={"host": host})


async def _post(path: str, host: str, payload: dict):
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await client.post(path, headers={"host": host}, json=payload)


class FakeWebSocket:
    def __init__(self, host: str = "127.0.0.1"):
        self.client = SimpleNamespace(host=host)
        self.client_state = SimpleNamespace(value=1)
        self.application_state = SimpleNamespace(value=1)
        self.sent = []
        self.close_calls = []

    async def accept(self):
        return None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000):
        self.close_calls.append(code)
        self.client_state.value = 2
        self.application_state.value = 2
        return None

    async def receive_text(self):
        raise WebSocketDisconnect()


def _future_iso(minutes: int = 5) -> str:
    value = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_internal_root_serves_dashboard(reset_state):
    response = await _request("/", api.INTERNAL_DASHBOARD_HOST)

    assert response.status_code == 200
    assert "PTV Internal Dashboard" in response.text
    assert "/internal/metrics" in response.text


@pytest.mark.asyncio
async def test_public_root_redirects_to_app_store(reset_state):
    response = await _request("/", api.PUBLIC_BASE_HOST, follow_redirects=False)
    index_response = await _request("/index.html", api.PUBLIC_BASE_HOST, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == api.PUBLIC_APPSTORE_URL
    assert index_response.status_code == 302
    assert index_response.headers["location"] == api.PUBLIC_APPSTORE_URL


@pytest.mark.asyncio
async def test_public_host_cannot_reach_internal_metrics(reset_state):
    response = await _request("/internal/metrics", api.PUBLIC_BASE_HOST)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_internal_metrics_snapshot_and_history_are_capped_and_ordered(reset_state):
    now = api.time.time()
    for index in range(api.METRICS_HISTORY_MAX_POINTS + 25):
        api._metrics_history.append(_metric_point(index))
    api._latest_metrics_snapshot = dict(api._metrics_history[-1])
    api._metrics_window["last_subscribers"] = 99
    api._metrics_window["last_unique_keys"] = 88
    api._metrics_window["last_active_clients"] = 77
    api._client_activity["client:test"] = {
        "label": "test-client",
        "scope_kind": "client_id",
        "client_id": "test-client",
        "ip_fingerprint": "deadbeef00",
        "connections_opened": 4,
        "ws_queries": 3,
        "active_connections": 1,
        "active_buttons": 2,
        "active_subscriber": True,
        "last_seen": "2026-01-01T00:00:00Z",
        "connection_timestamps": deque([now - 20.0, now - 10.0]),
        "query_timestamps": deque([now - 30.0, now - 15.0, now - 5.0]),
    }

    snapshot_response = await _request("/internal/metrics", api.INTERNAL_DASHBOARD_HOST)
    history_response = await _request("/internal/metrics/history", api.INTERNAL_DASHBOARD_HOST)

    snapshot = snapshot_response.json()
    history = history_response.json()["points"]

    assert snapshot_response.status_code == 200
    assert REQUIRED_METRIC_FIELDS.issubset(snapshot.keys())
    assert snapshot["subscribers"] == 99
    assert snapshot["unique_keys"] == 88
    assert snapshot["active_clients"] == 77
    assert snapshot["known_clients"] == 1
    assert snapshot["client_activity"][0]["label"] == "test-client"
    assert snapshot["client_activity"][0]["queries_last_hour"] == 3
    assert snapshot["client_activity"][0]["reconnects_last_hour"] == 2
    assert snapshot["client_leaderboard"][0]["label"] == "test-client"
    assert history_response.status_code == 200
    assert len(history) == api.METRICS_HISTORY_MAX_POINTS
    assert REQUIRED_METRIC_FIELDS.issubset(history[0].keys())
    assert history[0]["timestamp"] < history[-1]["timestamp"]


@pytest.mark.asyncio
async def test_public_station_api_and_websocket_still_work(reset_state):
    stations_response = await _request("/api/v1/stations", api.PUBLIC_BASE_HOST)
    health_response = await _request("/api/v1/health", api.PUBLIC_BASE_HOST)
    config_response = await _request("/pebble-config.html", api.PUBLIC_BASE_HOST)

    assert stations_response.status_code == 200
    assert "stations" in stations_response.json()
    assert health_response.status_code == 200
    assert health_response.json()["ok"] is True
    assert config_response.status_code == 200

    fake_websocket = FakeWebSocket()
    await api.websocket_endpoint(fake_websocket, client_id="watch-alpha")
    assert fake_websocket.sent
    assert fake_websocket.sent[0]["type"] == "connected"


@pytest.mark.asyncio
async def test_websocket_rejects_missing_client_id(reset_state):
    fake_websocket = FakeWebSocket()

    await api.websocket_endpoint(fake_websocket)

    assert fake_websocket.sent
    assert fake_websocket.sent[0]["type"] == "error"
    assert fake_websocket.sent[0]["error"] == "client_id is required"
    assert fake_websocket.close_calls == [1008]


@pytest.mark.asyncio
async def test_http_query_requires_client_id(reset_state):
    response = await _post(
        "/api/v1/query",
        api.PUBLIC_BASE_HOST,
        {"query": "next train", "llm_api_key": "dummy"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "client_id is required"


@pytest.mark.asyncio
async def test_http_favourite_requires_client_id(reset_state):
    response = await _post(
        "/api/v1/favourite",
        api.PUBLIC_BASE_HOST,
        {"button_id": 1, "stop_id": 1071, "route_type": 0},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "client_id is required"


@pytest.mark.asyncio
async def test_stale_websocket_is_pruned_before_connection_limit_check(reset_state):
    client_ip = "10.0.0.5"
    client_id = "alpha-client"
    scope_key = api._client_scope_key(client_ip, client_id)
    stale_websocket = FakeWebSocket(host=client_ip)
    stale_websocket.client_state.value = 2
    stale_websocket.application_state.value = 2

    api._ws_connections_by_scope[scope_key].add(stale_websocket)
    api._ws_client_ips[stale_websocket] = client_ip
    api._ws_connection_scopes[stale_websocket] = scope_key
    api._ws_client_ids[stale_websocket] = client_id
    api._favourite_subscriptions[stale_websocket] = [
        {"button_id": 1, "stop_id": 123, "route_type": 0, "direction_id": None, "dest_id": None}
    ]

    fresh_websocket = FakeWebSocket(host=client_ip)
    await api.websocket_endpoint(fresh_websocket, client_id=client_id)

    assert fresh_websocket.sent
    assert fresh_websocket.sent[0]["type"] == "connected"
    assert stale_websocket not in api._favourite_subscriptions
    assert scope_key not in api._ws_connections_by_scope


@pytest.mark.asyncio
async def test_new_websocket_evicts_existing_same_ip_connections(reset_state):
    client_ip = "10.0.0.5"
    client_id = "alpha-client"
    scope_key = api._client_scope_key(client_ip, client_id)
    existing_websocket = FakeWebSocket(host=client_ip)

    api._ws_connections_by_scope[scope_key].add(existing_websocket)
    api._ws_client_ips[existing_websocket] = client_ip
    api._ws_connection_scopes[existing_websocket] = scope_key
    api._ws_client_ids[existing_websocket] = client_id
    api._favourite_subscriptions[existing_websocket] = [
        {"button_id": 1, "stop_id": 123, "route_type": 0, "direction_id": None, "dest_id": None}
    ]

    fresh_websocket = FakeWebSocket(host=client_ip)
    await api.websocket_endpoint(fresh_websocket, client_id=client_id)

    assert existing_websocket.close_calls == [1012]
    assert existing_websocket not in api._favourite_subscriptions
    assert fresh_websocket.sent
    assert fresh_websocket.sent[0]["type"] == "connected"


@pytest.mark.asyncio
async def test_same_ip_different_client_ids_can_connect_without_eviction(reset_state):
    shared_ip = "10.0.0.5"
    first_scope = api._client_scope_key(shared_ip, "alpha-client")
    first_websocket = FakeWebSocket(host=shared_ip)
    api._ws_connections_by_scope[first_scope].add(first_websocket)
    api._ws_client_ips[first_websocket] = shared_ip
    api._ws_connection_scopes[first_websocket] = first_scope
    api._ws_client_ids[first_websocket] = "alpha-client"

    second_websocket = FakeWebSocket(host=shared_ip)
    await api.websocket_endpoint(second_websocket, client_id="beta-client")

    assert first_websocket.close_calls == []
    assert second_websocket.sent[0]["type"] == "connected"
    assert first_scope in api._ws_connections_by_scope


def test_summarize_favourite_disruption_respects_priority_order(reset_state):
    departures = [
        {"route_id": 11, "disruption_ids": [101, 102, 103]},
    ]
    disruptions = {
        "101": {
            "disruption_id": 101,
            "disruption_status": "Current",
            "disruption_type": "Minor Delays",
            "title": "Minor delays on the line",
            "description": "",
            "routes": [{"route_id": 11}],
        },
        "102": {
            "disruption_id": 102,
            "disruption_status": "Current",
            "disruption_type": "Major Delays",
            "title": "Major delays on the line",
            "description": "",
            "routes": [{"route_id": 11}],
        },
        "103": {
            "disruption_id": 103,
            "disruption_status": "Current",
            "disruption_type": "Part Suspended",
            "title": "Buses replacing trains today",
            "description": "Replacement buses are operating",
            "routes": [{"route_id": 11}],
        },
    }

    assert api._summarize_favourite_disruption(departures, disruptions) == "Bus Replacements"


def test_summarize_favourite_disruption_distinguishes_major_and_minor(reset_state):
    departures = [{"route_id": 11, "disruption_ids": [201, 202]}]
    disruptions = {
        "201": {
            "disruption_id": 201,
            "disruption_status": "Current",
            "disruption_type": "Major Delays",
            "title": "Congestion on the line",
            "description": "",
            "routes": [{"route_id": 11}],
        },
        "202": {
            "disruption_id": 202,
            "disruption_status": "Current",
            "disruption_type": "Minor Delays",
            "title": "Minor delays on the line",
            "description": "",
            "routes": [{"route_id": 11}],
        },
    }

    assert api._classify_disruption_label(disruptions["201"]) == "Major Delays"
    assert api._classify_disruption_label(disruptions["202"]) == "Minor Delays"


def test_summarize_favourite_disruption_ignores_planned_and_unrelated(reset_state):
    departures = [{"route_id": 11, "disruption_ids": [301]}]
    disruptions = {
        "301": {
            "disruption_id": 301,
            "disruption_status": "Planned",
            "disruption_type": "Planned Works",
            "title": "Buses replacing trains later tonight",
            "description": "",
            "routes": [{"route_id": 11}],
        },
        "302": {
            "disruption_id": 302,
            "disruption_status": "Current",
            "disruption_type": "Major Delays",
            "title": "Major delays elsewhere",
            "description": "",
            "routes": [{"route_id": 14}],
        },
    }

    assert api._summarize_favourite_disruption(departures, disruptions) is None


@pytest.mark.asyncio
async def test_fetch_departure_for_button_returns_disruption_label(reset_state, monkeypatch):
    async def fake_get_departures(route_type, stop_id, max_results=10, expand=None):
        return {
            "departures": [
                {
                    "route_id": 11,
                    "direction_id": 1,
                    "run_ref": "955602",
                    "disruption_ids": [401],
                    "estimated_departure_utc": _future_iso(6),
                    "platform_number": "1",
                }
            ],
            "disruptions": {
                "401": {
                    "disruption_id": 401,
                    "disruption_status": "Current",
                    "disruption_type": "Part Suspended",
                    "title": "Cranbourne and Pakenham lines: Buses replacing trains",
                    "description": "Passengers should use replacement buses.",
                    "routes": [{"route_id": 11}],
                }
            },
        }

    monkeypatch.setattr(api.ptv_client, "get_departures", fake_get_departures)

    result = await api.fetch_departure_for_button(1034, 0, 1)

    assert result["departures"]
    assert result["disruption_label"] == "Bus Replacements"


@pytest.mark.asyncio
async def test_fetch_departure_for_button_allows_through_routed_train_match(reset_state, monkeypatch):
    async def fake_get_departures(route_type, stop_id, max_results=10, expand=None):
        return {
            "departures": [
                {
                    "route_id": 11,
                    "direction_id": 1,
                    "run_ref": "955603",
                    "disruption_ids": [501],
                    "estimated_departure_utc": _future_iso(8),
                    "platform_number": "2",
                    "departure_note": "via Metro Tunnel",
                }
            ],
            "disruptions": {
                "501": {
                    "disruption_id": 501,
                    "disruption_status": "Current",
                    "disruption_type": "Part Suspended",
                    "title": "Cranbourne and Pakenham lines: Buses replacing trains",
                    "description": "Passengers should use replacement buses.",
                    "routes": [{"route_id": 11}],
                }
            },
        }

    monkeypatch.setattr(api.ptv_client, "get_departures", fake_get_departures)
    monkeypatch.setattr(
        api.tools,
        "resolve_trip_patterns",
        lambda start_stop_id, dest_stop_id, route_type=0: [
            {"route_id": 14, "direction_id": 1, "route_name": "Sunbury", "direction_name": "City"}
        ],
    )

    result = await api.fetch_departure_for_button(1230, 0, None, dest_id=1232)

    assert result["departures"]
    assert result["departures"][0]["route_id"] == 11
    assert result["disruption_label"] == "Bus Replacements"


@pytest.mark.asyncio
async def test_send_favourite_updates_includes_disruption_label(reset_state):
    fake_websocket = FakeWebSocket()

    await api._send_favourite_updates(
        fake_websocket,
        [
            {
                "button_id": 1,
                "departures": [{"minutes": 5}],
                "disruption_label": "Major Delays",
            }
        ],
    )

    assert fake_websocket.sent == [
        {
            "type": "favourite_update",
            "updates": [
                {
                    "button_id": 1,
                    "departures": [{"minutes": 5}],
                    "disruption_label": "Major Delays",
                }
            ],
        }
    ]
