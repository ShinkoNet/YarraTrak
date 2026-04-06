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
    api._watch_tasks.clear()
    api._ws_connections_by_ip.clear()
    api._ws_client_ips.clear()
    api._ws_query_limiters.clear()
    api._metrics_history.clear()
    api._latest_metrics_snapshot = api._empty_metrics_snapshot()
    api._metrics_window["last_subscribers"] = 0
    api._metrics_window["last_unique_keys"] = 0
    yield


async def _request(path: str, host: str, follow_redirects: bool = True):
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=follow_redirects,
    ) as client:
        return await client.get(path, headers={"host": host})


class FakeWebSocket:
    def __init__(self, host: str = "127.0.0.1"):
        self.client = SimpleNamespace(host=host)
        self.client_state = SimpleNamespace(value=1)
        self.application_state = SimpleNamespace(value=1)
        self.sent = []

    async def accept(self):
        return None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000):
        self.client_state.value = 2
        self.application_state.value = 2
        return None

    async def receive_text(self):
        raise WebSocketDisconnect()


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
    for index in range(api.METRICS_HISTORY_MAX_POINTS + 25):
        api._metrics_history.append(_metric_point(index))
    api._latest_metrics_snapshot = dict(api._metrics_history[-1])
    api._metrics_window["last_subscribers"] = 99
    api._metrics_window["last_unique_keys"] = 88

    snapshot_response = await _request("/internal/metrics", api.INTERNAL_DASHBOARD_HOST)
    history_response = await _request("/internal/metrics/history", api.INTERNAL_DASHBOARD_HOST)

    snapshot = snapshot_response.json()
    history = history_response.json()["points"]

    assert snapshot_response.status_code == 200
    assert REQUIRED_METRIC_FIELDS.issubset(snapshot.keys())
    assert snapshot["subscribers"] == 99
    assert snapshot["unique_keys"] == 88
    assert history_response.status_code == 200
    assert len(history) == api.METRICS_HISTORY_MAX_POINTS
    assert REQUIRED_METRIC_FIELDS.issubset(history[0].keys())
    assert history[0]["timestamp"] < history[-1]["timestamp"]


@pytest.mark.asyncio
async def test_public_station_api_and_websocket_still_work(reset_state):
    stations_response = await _request("/api/v1/stations", api.PUBLIC_BASE_HOST)
    config_response = await _request("/pebble-config.html", api.PUBLIC_BASE_HOST)

    assert stations_response.status_code == 200
    assert "stations" in stations_response.json()
    assert config_response.status_code == 200

    fake_websocket = FakeWebSocket()
    await api.websocket_endpoint(fake_websocket)
    assert fake_websocket.sent
    assert fake_websocket.sent[0]["type"] == "connected"


@pytest.mark.asyncio
async def test_stale_websocket_is_pruned_before_connection_limit_check(reset_state):
    client_ip = "10.0.0.5"
    stale_websocket = FakeWebSocket(host=client_ip)
    stale_websocket.client_state.value = 2
    stale_websocket.application_state.value = 2

    api._ws_connections_by_ip[client_ip].add(stale_websocket)
    api._ws_client_ips[stale_websocket] = client_ip
    api._favourite_subscriptions[stale_websocket] = [
        {"button_id": 1, "stop_id": 123, "route_type": 0, "direction_id": None, "dest_id": None}
    ]

    fresh_websocket = FakeWebSocket(host=client_ip)
    await api.websocket_endpoint(fresh_websocket)

    assert fresh_websocket.sent
    assert fresh_websocket.sent[0]["type"] == "connected"
    assert stale_websocket not in api._favourite_subscriptions
    assert client_ip not in api._ws_connections_by_ip
