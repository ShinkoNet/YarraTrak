"""
PTV Notify API - FastAPI server for public transport queries.

Open-data architecture: departure/station endpoints are unauthenticated.
Agent (LLM) endpoints use Bring-Your-Own-Key (BYOK) via Anthropic API key.
"""

from collections import defaultdict, deque
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
import json
from pydantic import BaseModel
from datetime import datetime, timezone
import hashlib
import logging
import os
import asyncio
import re
import time
import uuid
from starlette.websockets import WebSocketState

from .enums import RouteType
from . import agent_engine
from . import tools
from .ptv_client import PTVClient
from . import route_geometry
from .config import (
    ALLOWED_ORIGINS,
    FAVOURITE_CACHE_TTL_SECONDS,
    FAVOURITE_FETCH_CONCURRENCY,
    HTTP_FAVOURITE_RATE_LIMIT,
    METRICS_LOG_INTERVAL_SECONDS,
    METRICS_HISTORY_INTERVAL_SECONDS,
    METRICS_HISTORY_MAX_POINTS,
    HTTP_QUERY_RATE_LIMIT,
    INTERNAL_DASHBOARD_HOST,
    MAX_FAVOURITE_BUTTONS,
    MAX_QUERY_LENGTH,
    MAX_WS_CONNECTIONS_PER_IP,
    PUBLIC_APPSTORE_URL,
    PUBLIC_BASE_HOST,
    WS_QUERY_RATE_LIMIT,
)

app = FastAPI(title="PTV Notify", version="1.0.0")
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# GZip compression for large responses (station databases)
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Request Models ---

class FavouriteRequest(BaseModel):
    button_id: int
    stop_id: int
    stop_name: str | None = None
    dest_id: int | None = None
    direction_id: int | None = None
    direction_name: str | None = None
    route_type: int = RouteType.TRAIN
    client_id: str | None = None

class StopHistory(BaseModel):
    stop_id: int
    stop_name: str
    route_type: int

class AgentRequest(BaseModel):
    query: str
    session_id: str | None = None
    query_history: list[StopHistory] | None = None
    llm_api_key: str | None = None
    client_id: str | None = None

# --- Initialization ---

ptv_client = PTVClient()

# --- Live Favourite Updates ---
# Subscription registry: websocket -> list of button configs with start/destination info
_favourite_subscriptions: dict[WebSocket, list[dict]] = {}

# Shared departure cache: (stop_id, route_type, direction_id, dest_id) -> {departures: [...], fetched_at}
# With multi-departure caching, we can use longer TTL - client switches between cached departures
_departure_cache: dict[tuple, dict] = {}
FAVOURITE_CACHE_TTL = max(15.0, FAVOURITE_CACHE_TTL_SECONDS)  # seconds
FAVOURITE_BROADCAST_INTERVAL = 15.0  # seconds
FAVOURITE_FETCH_LIMIT = max(1, FAVOURITE_FETCH_CONCURRENCY)
CLIENT_ACTIVITY_WINDOW_SECONDS = 3600.0
CLIENT_LEADERBOARD_LIMIT = 8
WS_IDLE_TIMEOUT_SECONDS = 90.0

# Background task reference
_broadcast_task: asyncio.Task | None = None
_metrics_task: asyncio.Task | None = None

# Run position cache
_run_position_cache: dict[tuple, dict] = {}
RUN_POSITION_TTL = 8.0  # seconds
POSITION_BROADCAST_INTERVAL = 5.0  # seconds

# Watch position tasks (per websocket)
_watch_tasks: dict[WebSocket, asyncio.Task] = {}

# Basic in-memory protections for public deployments
RATE_LIMIT_WINDOW_SECONDS = 60.0
_http_query_limiters: dict[str, deque[float]] = defaultdict(deque)
_http_favourite_limiters: dict[str, deque[float]] = defaultdict(deque)
_ws_query_limiters: dict[str, deque[float]] = defaultdict(deque)
_ws_connections_by_scope: dict[str, set[WebSocket]] = defaultdict(set)
_ws_client_ips: dict[WebSocket, str] = {}
_ws_connection_scopes: dict[WebSocket, str] = {}
_ws_client_ids: dict[WebSocket, str | None] = {}
_client_activity: dict[str, dict[str, object]] = {}
_favourite_fetch_semaphore = asyncio.Semaphore(FAVOURITE_FETCH_LIMIT)
_metrics_window = {
    "departure_cache_hits": 0,
    "departure_cache_misses": 0,
    "broadcast_iterations": 0,
    "broadcast_duration_sum": 0.0,
    "broadcast_duration_max": 0.0,
    "last_unique_keys": 0,
    "last_subscribers": 0,
    "last_active_clients": 0,
}
_metrics_history: deque[dict[str, int | float | str]] = deque(
    maxlen=max(1, METRICS_HISTORY_MAX_POINTS)
)
_dashboard_local_hosts = {"localhost", "127.0.0.1", "[::1]", "::1"}
_BUS_REPLACEMENT_PHRASES = (
    "buses replacing trains",
    "buses replace trains",
    "buses are replacing",
    "bus replacements",
    "replacement buses",
)
_BUS_REPLACEMENT_RANGE_PATTERNS = (
    re.compile(r"\bbetween\s+(?P<start>.+?)\s+and\s+(?P<end>.+?)(?:\bstation(?:s)?\b|[.,;:]|$)", re.IGNORECASE),
    re.compile(r"\bfrom\s+(?P<start>.+?)\s+to\s+(?P<end>.+?)(?:\bstation(?:s)?\b|[.,;:]|$)", re.IGNORECASE),
    re.compile(r"\bbetween\s+(?P<start>.+?)\s*-\s*(?P<end>.+?)(?:\bstation(?:s)?\b|[.,;:]|$)", re.IGNORECASE),
)
_DISRUPTION_PRIORITY = {
    "Bus Replacements": 0,
    "Bus Replacements Here": 0,
    "Bus Replacements Ahead": 0,
    "Major Delays": 1,
    "Minor Delays": 2,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_isoformat(value: datetime | None = None) -> str:
    current = value or _utc_now()
    return current.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_host(raw_host: str | None) -> str:
    if not raw_host:
        return ""
    host = raw_host.split(",", 1)[0].strip().lower()
    if host.startswith("[") and "]" in host:
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


def _request_host(request: Request) -> str:
    return _normalize_host(request.headers.get("host"))


def _is_internal_dashboard_host(host: str) -> bool:
    return host == INTERNAL_DASHBOARD_HOST or host in _dashboard_local_hosts


def _require_internal_dashboard_request(request: Request) -> None:
    if not _is_internal_dashboard_host(_request_host(request)):
        raise HTTPException(status_code=404, detail="Not found")


def _empty_metrics_snapshot(timestamp: datetime | None = None) -> dict[str, int | float | str]:
    return {
        "timestamp": _utc_isoformat(timestamp),
        "subscribers": 0,
        "unique_keys": 0,
        "active_clients": 0,
        "known_clients": 0,
        "broadcast_loops": 0,
        "avg_loop_ms": 0.0,
        "max_loop_ms": 0.0,
        "cache_hits": 0,
        "cache_misses": 0,
        "cache_hit_rate": 0.0,
        "upstream_rps": 0.0,
        "ptv_departures": 0,
        "ptv_runs": 0,
        "ptv_directions": 0,
        "ptv_search": 0,
    }


def _classify_disruption_label(disruption: dict) -> str | None:
    disruption_type = (disruption.get("disruption_type") or "").strip()
    searchable_text = " ".join(
        part.strip().lower()
        for part in (
            disruption.get("title") or "",
            disruption.get("description") or "",
            disruption_type,
        )
        if part
    )

    if any(phrase in searchable_text for phrase in _BUS_REPLACEMENT_PHRASES):
        return "Bus Replacements"
    if disruption_type == "Major Delays" or "major delays" in searchable_text or "major delay" in searchable_text:
        return "Major Delays"
    if disruption_type == "Minor Delays" or "minor delays" in searchable_text or "minor delay" in searchable_text:
        return "Minor Delays"
    return None


def _normalize_station_reference(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    normalized = re.sub(r"\b(railway|train)\s+station\b", "", normalized)
    normalized = re.sub(r"\bstations?\b", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _station_aliases(stop_name: str) -> set[str]:
    aliases = set()
    normalized = _normalize_station_reference(stop_name)
    if normalized:
        aliases.add(normalized)
    base = normalized.removesuffix(" station").strip()
    if base:
        aliases.add(base)
    return aliases


def _match_station_sequence(fragment: str, route_stops: list[dict]) -> int | None:
    fragment_normalized = _normalize_station_reference(fragment)
    if not fragment_normalized:
        return None

    best_match: tuple[int, int] | None = None
    for stop in route_stops:
        for alias in _station_aliases(stop.get("stop_name", "")):
            if not alias:
                continue
            if fragment_normalized == alias or alias in fragment_normalized or fragment_normalized in alias:
                score = len(alias)
                if best_match is None or score > best_match[0]:
                    best_match = (score, stop["seq"])
    return best_match[1] if best_match else None


def _extract_disruption_station_range(
    disruption: dict,
    route_id: int | None,
    direction_id: int | None,
    route_type: int,
) -> tuple[int, int] | None:
    if route_id is None or direction_id is None:
        return None

    route_stops = tools.get_route_direction_stops(route_id, direction_id, route_type)
    if not route_stops:
        return None

    seq_by_stop_id = {stop["stop_id"]: stop["seq"] for stop in route_stops if stop.get("stop_id") is not None}
    stop_seqs = [
        seq_by_stop_id.get(stop.get("stop_id"))
        for stop in disruption.get("stops", [])
        if stop.get("stop_id") in seq_by_stop_id
    ]
    stop_seqs = [seq for seq in stop_seqs if seq is not None]
    if len(stop_seqs) >= 2:
        return min(stop_seqs), max(stop_seqs)

    text = " ".join(
        part.strip()
        for part in (disruption.get("title") or "", disruption.get("description") or "")
        if part
    )
    if not text:
        return None

    for pattern in _BUS_REPLACEMENT_RANGE_PATTERNS:
        for match in pattern.finditer(text):
            start_seq = _match_station_sequence(match.group("start"), route_stops)
            end_seq = _match_station_sequence(match.group("end"), route_stops)
            if start_seq is None or end_seq is None or start_seq == end_seq:
                continue
            return min(start_seq, end_seq), max(start_seq, end_seq)

    return None


def _journey_disruption_scope(
    start_stop_id: int,
    dest_id: int | None,
    route_id: int | None,
    direction_id: int | None,
    route_type: int,
    affected_range: tuple[int, int],
) -> str | None:
    if route_id is None or direction_id is None:
        return None

    route_stops = tools.get_route_direction_stops(route_id, direction_id, route_type)
    if not route_stops:
        return None

    seq_by_stop_id = {stop["stop_id"]: stop["seq"] for stop in route_stops if stop.get("stop_id") is not None}
    start_seq = seq_by_stop_id.get(start_stop_id)
    if start_seq is None:
        return None

    if dest_id is None:
        trip_start, trip_end = start_seq, start_seq
    else:
        dest_seq = seq_by_stop_id.get(dest_id)
        if dest_seq is None:
            dest_seq = route_stops[-1]["seq"] if start_seq <= route_stops[-1]["seq"] else route_stops[0]["seq"]
        trip_start, trip_end = sorted((start_seq, dest_seq))

    affected_start, affected_end = affected_range
    if trip_end < affected_start or trip_start > affected_end:
        return None
    if affected_start <= start_seq <= affected_end:
        return "Here"
    return "Ahead"


def _bus_replacement_label_for_trip(
    disruption: dict,
    departures: list[dict],
    stop_id: int,
    dest_id: int | None,
    route_type: int,
) -> str | None:
    disruption_id = disruption.get("disruption_id")
    candidate_departures = [
        departure
        for departure in departures
        if disruption_id in (departure.get("disruption_ids") or [])
    ] or departures

    parsed_any_range = False
    for departure in candidate_departures:
        affected_range = _extract_disruption_station_range(
            disruption,
            departure.get("route_id"),
            departure.get("direction_id"),
            route_type,
        )
        if affected_range is None:
            continue
        parsed_any_range = True
        scope = _journey_disruption_scope(
            stop_id,
            dest_id,
            departure.get("route_id"),
            departure.get("direction_id"),
            route_type,
            affected_range,
        )
        if scope == "Here":
            return "Bus Replacements Here"
        if scope == "Ahead":
            return "Bus Replacements Ahead"

    if not parsed_any_range:
        return "Bus Replacements"
    return None


def _summarize_favourite_disruption(
    departures: list[dict],
    disruptions: dict,
    stop_id: int,
    dest_id: int | None,
    route_type: int,
    allowed_trip_pairs: set[tuple[int | None, int | None]] | None = None,
) -> str | None:
    if not disruptions:
        return None

    relevant_route_ids = {
        departure.get("route_id")
        for departure in departures
        if departure.get("route_id") is not None
    }
    if allowed_trip_pairs:
        relevant_route_ids.update(
            route_id for route_id, _direction_id in allowed_trip_pairs if route_id is not None
        )

    relevant_disruption_ids = {
        disruption_id
        for departure in departures
        for disruption_id in (departure.get("disruption_ids") or [])
        if disruption_id is not None
    }

    best_label: str | None = None
    best_priority = len(_DISRUPTION_PRIORITY)

    for disruption_key, disruption in disruptions.items():
        if disruption.get("disruption_status") != "Current":
            continue

        disruption_id = disruption.get("disruption_id")
        affected_route_ids = {
            route.get("route_id")
            for route in disruption.get("routes", [])
            if route.get("route_id") is not None
        }
        if (
            disruption_id not in relevant_disruption_ids
            and not affected_route_ids.intersection(relevant_route_ids)
        ):
            continue

        label = _classify_disruption_label(disruption)
        if not label:
            continue
        if label == "Bus Replacements":
            label = _bus_replacement_label_for_trip(
                disruption,
                departures,
                stop_id,
                dest_id,
                route_type,
            )
            if not label:
                continue

        priority = _DISRUPTION_PRIORITY[label]
        if priority < best_priority:
            best_label = label
            best_priority = priority
            if priority == 0:
                break

    return best_label


_latest_metrics_snapshot: dict[str, int | float | str] = _empty_metrics_snapshot()


def _dashboard_tuning() -> dict[str, int | float]:
    history_interval = max(10.0, METRICS_HISTORY_INTERVAL_SECONDS)
    return {
        "favourite_cache_ttl_seconds": round(FAVOURITE_CACHE_TTL, 1),
        "favourite_fetch_concurrency": FAVOURITE_FETCH_LIMIT,
        "broadcast_interval_seconds": round(FAVOURITE_BROADCAST_INTERVAL, 1),
        "metrics_interval_seconds": round(max(10.0, METRICS_LOG_INTERVAL_SECONDS), 1),
        "history_interval_seconds": round(history_interval, 1),
        "history_points": max(1, METRICS_HISTORY_MAX_POINTS),
        "history_retention_hours": round((history_interval * max(1, METRICS_HISTORY_MAX_POINTS)) / 3600.0, 1),
    }


def _client_ip_fingerprint(client_ip: str) -> str:
    return hashlib.sha256(client_ip.encode("utf-8")).hexdigest()[:10]


def _normalize_client_id(client_id: str | None) -> str | None:
    if client_id is None:
        return None
    normalized = "".join(
        ch for ch in client_id.strip().lower()
        if ch.isalnum() or ch in {"-", "_"}
    )
    return normalized[:64] or None


def _require_client_id(client_id: str | None) -> str:
    normalized = _normalize_client_id(client_id)
    if not normalized:
        raise HTTPException(status_code=400, detail="client_id is required")
    return normalized


def _client_scope_key(client_ip: str, client_id: str | None) -> str:
    if client_id:
        return f"client:{client_id}"
    return f"ip:{client_ip}"


def _client_scope_label(client_ip: str, client_id: str | None) -> str:
    if client_id:
        return client_id[:12]
    return f"ip-{_client_ip_fingerprint(client_ip)}"


def _prune_client_activity_window(activity: dict[str, object], now: float | None = None) -> None:
    current = now if now is not None else time.time()
    cutoff = current - CLIENT_ACTIVITY_WINDOW_SECONDS
    for key in ("connection_timestamps", "query_timestamps"):
        bucket = activity.get(key)
        if not isinstance(bucket, deque):
            continue
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()


def _touch_client_activity(scope_key: str, client_ip: str, client_id: str | None) -> dict[str, object]:
    activity = _client_activity.get(scope_key)
    if activity is None:
        activity = {
            "label": _client_scope_label(client_ip, client_id),
            "scope_kind": "client_id" if client_id else "ip_fallback",
            "client_id": client_id,
            "ip_fingerprint": _client_ip_fingerprint(client_ip),
            "connections_opened": 0,
            "ws_queries": 0,
            "active_connections": 0,
            "active_buttons": 0,
            "active_subscriber": False,
            "last_seen": _utc_isoformat(),
            "connection_timestamps": deque(),
            "query_timestamps": deque(),
        }
        _client_activity[scope_key] = activity
    else:
        activity["last_seen"] = _utc_isoformat()
        if client_id:
            activity["client_id"] = client_id
            activity["label"] = _client_scope_label(client_ip, client_id)
            activity["scope_kind"] = "client_id"
        activity["ip_fingerprint"] = _client_ip_fingerprint(client_ip)
    _prune_client_activity_window(activity)
    return activity


def _record_client_activity_event(
    scope_key: str,
    client_ip: str,
    client_id: str | None,
    event_key: str,
) -> dict[str, object]:
    activity = _touch_client_activity(scope_key, client_ip, client_id)
    bucket = activity.get(event_key)
    if not isinstance(bucket, deque):
        bucket = deque()
        activity[event_key] = bucket
    now = time.time()
    bucket.append(now)
    _prune_client_activity_window(activity, now)
    return activity


def _client_activity_rows(limit: int = 12) -> list[dict[str, object]]:
    active_buttons_by_scope: dict[str, int] = defaultdict(int)
    active_subscribers: set[str] = set()
    for websocket, buttons in _active_favourite_subscriptions():
        scope_key = _ws_connection_scopes.get(websocket)
        if scope_key is None:
            continue
        active_subscribers.add(scope_key)
        active_buttons_by_scope[scope_key] += len(buttons)

    active_client_count = 0
    rows: list[dict[str, object]] = []
    for scope_key, activity in _client_activity.items():
        _prune_client_activity_window(activity)
        active_connections = len(_ws_connections_by_scope.get(scope_key, set()))
        if active_connections > 0:
            active_client_count += 1
        activity["active_connections"] = active_connections
        activity["active_buttons"] = active_buttons_by_scope.get(scope_key, 0)
        activity["active_subscriber"] = scope_key in active_subscribers
        query_timestamps = activity.get("query_timestamps")
        connection_timestamps = activity.get("connection_timestamps")
        rows.append({
            "label": activity["label"],
            "scope_kind": activity["scope_kind"],
            "ip_fingerprint": activity["ip_fingerprint"],
            "connections_opened": int(activity["connections_opened"]),
            "ws_queries": int(activity["ws_queries"]),
            "queries_last_hour": len(query_timestamps) if isinstance(query_timestamps, deque) else 0,
            "reconnects_last_hour": len(connection_timestamps) if isinstance(connection_timestamps, deque) else 0,
            "active_connections": active_connections,
            "active_buttons": int(activity["active_buttons"]),
            "active_subscriber": bool(activity["active_subscriber"]),
            "last_seen": activity["last_seen"],
        })

    rows.sort(
        key=lambda row: (
            1 if row["active_subscriber"] else 0,
            int(row["active_connections"]),
            int(row["ws_queries"]),
            int(row["connections_opened"]),
            str(row["last_seen"]),
        ),
        reverse=True,
    )
    return rows[:limit]


def _client_leaderboard_rows(limit: int = CLIENT_LEADERBOARD_LIMIT) -> list[dict[str, object]]:
    leaderboard = [
        row for row in _client_activity_rows(limit=max(limit * 3, 12))
        if row["queries_last_hour"] or row["reconnects_last_hour"] or row["active_connections"]
    ]
    leaderboard.sort(
        key=lambda row: (
            int(row["queries_last_hour"]),
            int(row["reconnects_last_hour"]),
            int(row["active_connections"]),
            int(row["connections_opened"]),
            str(row["last_seen"]),
        ),
        reverse=True,
    )
    return leaderboard[:limit]


def _metrics_snapshot_payload() -> dict[str, object]:
    payload = dict(_latest_metrics_snapshot)
    payload["subscribers"] = int(_metrics_window.get("last_subscribers", payload["subscribers"]))
    payload["unique_keys"] = int(_metrics_window.get("last_unique_keys", payload["unique_keys"]))
    payload["active_clients"] = int(_metrics_window.get("last_active_clients", payload["active_clients"]))
    payload["known_clients"] = len(_client_activity)
    payload["client_activity"] = _client_activity_rows()
    payload["client_leaderboard"] = _client_leaderboard_rows()
    payload["tuning"] = _dashboard_tuning()
    payload["history_points"] = len(_metrics_history)
    payload["history_capacity"] = max(1, METRICS_HISTORY_MAX_POINTS)
    payload["internal_dashboard_host"] = INTERNAL_DASHBOARD_HOST
    return payload


def _metrics_history_payload() -> dict[str, object]:
    return {
        "points": list(_metrics_history),
        "count": len(_metrics_history),
        "max_points": max(1, METRICS_HISTORY_MAX_POINTS),
    }


def _normalize_metrics_snapshot(
    api_metrics: dict[str, float], ptv_metrics: dict[str, int], interval_seconds: float
) -> dict[str, int | float | str]:
    safe_interval = max(1.0, interval_seconds)
    cache_hits = int(api_metrics.get("departure_cache_hits", 0))
    cache_misses = int(api_metrics.get("departure_cache_misses", 0))
    cache_lookups = cache_hits + cache_misses
    broadcast_loops = int(api_metrics.get("broadcast_iterations", 0))
    avg_loop_ms = (
        (float(api_metrics.get("broadcast_duration_sum", 0.0)) / broadcast_loops) * 1000.0
        if broadcast_loops
        else 0.0
    )
    upstream_total = sum(int(value) for value in ptv_metrics.values())
    return {
        "timestamp": _utc_isoformat(),
        "subscribers": int(api_metrics.get("last_subscribers", 0)),
        "unique_keys": int(api_metrics.get("last_unique_keys", 0)),
        "active_clients": int(api_metrics.get("last_active_clients", 0)),
        "known_clients": len(_client_activity),
        "broadcast_loops": broadcast_loops,
        "avg_loop_ms": round(avg_loop_ms, 2),
        "max_loop_ms": round(float(api_metrics.get("broadcast_duration_max", 0.0)) * 1000.0, 2),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "cache_hit_rate": round(((cache_hits / cache_lookups) * 100.0) if cache_lookups else 0.0, 2),
        "upstream_rps": round(upstream_total / safe_interval, 3),
        "ptv_departures": int(ptv_metrics.get("departures", 0)),
        "ptv_runs": int(ptv_metrics.get("runs", 0)),
        "ptv_directions": int(ptv_metrics.get("directions", 0)),
        "ptv_search": int(ptv_metrics.get("search", 0)),
    }


def _capture_metrics_snapshot(interval_seconds: float) -> dict[str, int | float | str]:
    global _latest_metrics_snapshot
    snapshot = _normalize_metrics_snapshot(
        _snapshot_and_reset_metrics(),
        ptv_client.snapshot_and_reset_metrics(),
        interval_seconds,
    )
    _latest_metrics_snapshot = snapshot
    _metrics_history.append(dict(snapshot))
    return snapshot


def _render_dashboard_html(snapshot: dict[str, object], history_payload: dict[str, object]) -> str:
    bootstrap = json.dumps({"snapshot": snapshot, "history": history_payload}).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PTV Internal Dashboard</title>
  <style>
    :root {{
      --navy: #08243d;
      --navy-deep: #031421;
      --teal: #0aa4a6;
      --teal-soft: #76ddd7;
      --amber: #ffb000;
      --amber-soft: #ffd36a;
      --rose: #ff6b57;
      --ink: #dce9f4;
      --muted: #8da5bb;
      --panel: rgba(7, 26, 42, 0.9);
      --panel-border: rgba(118, 221, 215, 0.16);
      --grid: rgba(255, 255, 255, 0.08);
      --shadow: 0 24px 60px rgba(0, 0, 0, 0.32);
      font-family: "Avenir Next", Avenir, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(10, 164, 166, 0.34), transparent 30%),
        radial-gradient(circle at top right, rgba(255, 176, 0, 0.18), transparent 26%),
        linear-gradient(165deg, #03111b 0%, #08243d 52%, #04121f 100%);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(255, 255, 255, 0.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255, 255, 255, 0.025) 1px, transparent 1px);
      background-size: 40px 40px;
      mask-image: linear-gradient(180deg, rgba(0, 0, 0, 0.55), transparent);
    }}
    .shell {{
      width: min(1180px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 28px 0 40px;
    }}
    .hero {{
      margin-bottom: 20px;
      padding: 24px 28px;
      border: 1px solid var(--panel-border);
      border-radius: 24px;
      background: linear-gradient(180deg, rgba(9, 35, 57, 0.98), rgba(3, 20, 33, 0.92));
      box-shadow: var(--shadow);
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
      color: var(--teal-soft);
      font-size: 0.85rem;
      font-weight: 700;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }}
    .eyebrow::before {{
      content: "";
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--teal);
      box-shadow: 0 0 18px rgba(10, 164, 166, 0.85);
    }}
    h1 {{
      margin: 0;
      font-size: clamp(2rem, 4vw, 3.4rem);
      line-height: 0.98;
      letter-spacing: -0.04em;
    }}
    .hero p {{
      max-width: 700px;
      margin: 10px 0 0;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1.6;
    }}
    .hero-note {{
      margin-top: 14px;
      color: var(--teal-soft);
      font-size: 0.95rem;
      font-weight: 600;
      letter-spacing: 0.01em;
    }}
    .panel, .stat-card {{
      border: 1px solid var(--panel-border);
      border-radius: 22px;
      background: var(--panel);
      box-shadow: var(--shadow);
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }}
    .stat-card {{
      padding: 18px;
      overflow: hidden;
      position: relative;
    }}
    .stat-card::after {{
      content: "";
      position: absolute;
      inset: auto -24px -24px auto;
      width: 90px;
      height: 90px;
      border-radius: 999px;
      background: radial-gradient(circle, rgba(255, 176, 0, 0.18), transparent 70%);
    }}
    .stat-label {{
      color: var(--muted);
      font-size: 0.76rem;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}
    .stat-value {{
      margin-top: 10px;
      font-size: clamp(1.6rem, 3vw, 2.4rem);
      font-weight: 800;
      letter-spacing: -0.04em;
    }}
    .stat-subvalue {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 0.95rem;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 18px;
    }}
    .panel {{
      padding: 18px 20px 20px;
    }}
    .panel h2 {{
      margin: 0;
      font-size: 1rem;
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }}
    .panel-head {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .panel-meta {{
      color: var(--muted);
      font-size: 0.88rem;
    }}
    .chart-panel {{
      grid-column: span 4;
      min-width: 0;
    }}
    .info-panel {{
      grid-column: span 6;
    }}
    .chart-wrap {{
      min-height: 210px;
    }}
    .chart-placeholder {{
      height: 190px;
      display: grid;
      place-items: center;
      border-radius: 18px;
      border: 1px dashed rgba(255, 255, 255, 0.12);
      color: var(--muted);
      font-size: 0.95rem;
    }}
    .chart-frame {{
      width: 100%;
      height: 190px;
      display: block;
    }}
    .legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      margin-top: 12px;
      color: var(--muted);
      font-size: 0.86rem;
    }}
    .legend-item {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .legend-swatch {{
      width: 12px;
      height: 12px;
      border-radius: 999px;
    }}
    .chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .chip {{
      padding: 10px 12px;
      border-radius: 999px;
      border: 1px solid rgba(255, 255, 255, 0.08);
      background: rgba(255, 255, 255, 0.04);
      color: var(--ink);
      font-size: 0.92rem;
    }}
    .kv-list {{
      display: grid;
      gap: 10px;
    }}
    .kv-row {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      color: var(--muted);
    }}
    .kv-row strong {{
      color: var(--ink);
      font-weight: 700;
    }}
    .client-list {{
      display: grid;
      gap: 12px;
    }}
    .client-row {{
      display: grid;
      gap: 8px;
      padding: 14px 16px;
      border-radius: 16px;
      border: 1px solid rgba(255, 255, 255, 0.08);
      background: rgba(255, 255, 255, 0.03);
    }}
    .client-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
    }}
    .client-label {{
      font-weight: 700;
      color: var(--ink);
    }}
    .client-kind {{
      color: var(--muted);
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .client-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .leaderboard-list {{
      display: grid;
      gap: 10px;
    }}
    .leaderboard-row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid rgba(255, 255, 255, 0.08);
      background: rgba(255, 255, 255, 0.03);
    }}
    .leaderboard-rank {{
      color: var(--amber-soft);
      min-width: 2ch;
      font-weight: 700;
    }}
    .leaderboard-main {{
      flex: 1;
      min-width: 0;
    }}
    .leaderboard-label {{
      color: var(--ink);
      font-weight: 700;
    }}
    .leaderboard-sub {{
      color: var(--muted);
      font-size: 0.86rem;
    }}
    .leaderboard-stats {{
      text-align: right;
      color: var(--muted);
      font-size: 0.9rem;
      white-space: nowrap;
    }}
    @media (max-width: 960px) {{
      .chart-panel,
      .info-panel {{
        grid-column: 1 / -1;
      }}
    }}
    @media (max-width: 640px) {{
      .shell {{
        width: min(100vw - 20px, 1180px);
        padding-top: 18px;
      }}
      .hero,
      .panel,
      .stat-card {{
        border-radius: 18px;
      }}
      .hero {{
        padding: 20px;
      }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="eyebrow">PTV Notify Internal Ops</div>
      <h1>PTV Notify Realtime Metrics</h1>
      <p>The dashboard polls the in-process metrics snapshot directly from FastAPI, refreshes automatically every 15 seconds, and keeps a rolling in-memory history buffer that caps at one week.</p>
      <div class="hero-note">Auto-refresh is always on. History resets when the service restarts.</div>
    </section>

    <section class="stats">
      <article class="stat-card"><div class="stat-label">Subscribers</div><div class="stat-value" id="stat-subscribers">0</div></article>
      <article class="stat-card"><div class="stat-label">Active Clients</div><div class="stat-value" id="stat-active-clients">0</div><div class="stat-subvalue" id="stat-known-clients">0 seen since restart</div></article>
      <article class="stat-card"><div class="stat-label">Unique Keys</div><div class="stat-value" id="stat-unique-keys">0</div></article>
      <article class="stat-card"><div class="stat-label">Cache Hit Rate</div><div class="stat-value" id="stat-cache-hit-rate">0%</div><div class="stat-subvalue" id="stat-cache-breakdown">0 hits / 0 misses</div></article>
      <article class="stat-card"><div class="stat-label">Upstream RPS</div><div class="stat-value" id="stat-upstream-rps">0.00</div><div class="stat-subvalue" id="stat-upstream-total">0 requests / sample</div></article>
      <article class="stat-card"><div class="stat-label">Avg Loop</div><div class="stat-value" id="stat-avg-loop">0 ms</div><div class="stat-subvalue" id="stat-broadcast-loops">0 loops / sample</div></article>
      <article class="stat-card"><div class="stat-label">Max Loop</div><div class="stat-value" id="stat-max-loop">0 ms</div><div class="stat-subvalue" id="stat-ptv-split">departures 0, runs 0</div></article>
    </section>

    <section class="grid">
      <article class="panel chart-panel">
        <div class="panel-head">
          <h2>Cache Hit Rate</h2>
          <div class="panel-meta" id="cache-hit-meta">Rolling history</div>
        </div>
        <div class="chart-wrap" id="chart-cache-hit-rate"></div>
      </article>
      <article class="panel chart-panel">
        <div class="panel-head">
          <h2>Upstream Request Rate</h2>
          <div class="panel-meta" id="upstream-rps-meta">Rolling history</div>
        </div>
        <div class="chart-wrap" id="chart-upstream-rps"></div>
      </article>
      <article class="panel chart-panel">
        <div class="panel-head">
          <h2>Keys vs Subscribers</h2>
          <div class="panel-meta" id="keys-subs-meta">Rolling history</div>
        </div>
        <div class="chart-wrap" id="chart-keys-subs"></div>
      </article>

      <article class="panel info-panel">
        <div class="panel-head">
          <h2>Current Tuning</h2>
          <div class="panel-meta">Runtime values from process config</div>
        </div>
        <div class="chips" id="tuning-values"></div>
      </article>
      <article class="panel info-panel">
        <div class="panel-head">
          <h2>Sample Breakdown</h2>
          <div class="panel-meta">Latest normalized counters</div>
        </div>
        <div class="kv-list" id="snapshot-breakdown"></div>
      </article>
      <article class="panel info-panel">
        <div class="panel-head">
          <h2>Client Activity</h2>
          <div class="panel-meta">Anonymous subscriber IDs and per-client websocket query counts</div>
        </div>
        <div class="client-list" id="client-activity"></div>
      </article>
      <article class="panel info-panel">
        <div class="panel-head">
          <h2>Rolling Leaderboard</h2>
          <div class="panel-meta">Top anonymous clients over the last hour</div>
        </div>
        <div class="leaderboard-list" id="client-leaderboard"></div>
      </article>
    </section>
  </main>

  <script id="dashboard-bootstrap" type="application/json">{bootstrap}</script>
  <script>
    const bootstrap = JSON.parse(document.getElementById("dashboard-bootstrap").textContent);
    const state = {{
      snapshot: bootstrap.snapshot || {{}},
      history: (bootstrap.history && bootstrap.history.points) || [],
    }};

    const POLL_MS = 15000;
    const HISTORY_MS = 60000;

    function formatNumber(value, digits = 0) {{
      return Number(value || 0).toLocaleString(undefined, {{
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      }});
    }}

    function formatPercent(value) {{
      return `${{formatNumber(value, 1)}}%`;
    }}

    function formatMs(value) {{
      return `${{formatNumber(value, value >= 100 ? 0 : 1)}} ms`;
    }}

    function formatChartStamp(points) {{
      if (!points.length) return "No retained samples yet";
      const first = new Date(points[0].timestamp);
      const last = new Date(points[points.length - 1].timestamp);
      return `${{points.length}} samples from ${{first.toLocaleTimeString([], {{ hour: "2-digit", minute: "2-digit" }})}} to ${{last.toLocaleTimeString([], {{ hour: "2-digit", minute: "2-digit" }})}}`;
    }}

    function setText(id, value) {{
      const node = document.getElementById(id);
      if (node) node.textContent = value;
    }}

    function renderStats() {{
      const snap = state.snapshot || {{}};
      setText("stat-subscribers", formatNumber(snap.subscribers));
      setText("stat-active-clients", formatNumber(snap.active_clients));
      setText("stat-known-clients", `${{formatNumber(snap.known_clients || 0)}} seen since restart`);
      setText("stat-unique-keys", formatNumber(snap.unique_keys));
      setText("stat-cache-hit-rate", formatPercent(snap.cache_hit_rate));
      setText("stat-cache-breakdown", `${{formatNumber(snap.cache_hits)}} hits / ${{formatNumber(snap.cache_misses)}} misses`);
      setText("stat-upstream-rps", formatNumber(snap.upstream_rps, 2));
      setText("stat-upstream-total", `${{formatNumber((snap.ptv_departures || 0) + (snap.ptv_runs || 0) + (snap.ptv_directions || 0) + (snap.ptv_search || 0))}} requests / sample`);
      setText("stat-avg-loop", formatMs(snap.avg_loop_ms));
      setText("stat-broadcast-loops", `${{formatNumber(snap.broadcast_loops)}} loops / sample`);
      setText("stat-max-loop", formatMs(snap.max_loop_ms));
      setText("stat-ptv-split", `departures ${{formatNumber(snap.ptv_departures)}}, runs ${{formatNumber(snap.ptv_runs)}}`);
    }}

    function renderTuning() {{
      const target = document.getElementById("tuning-values");
      const tuning = (state.snapshot && state.snapshot.tuning) || {{}};
      const entries = [
        ["Cache TTL", `${{formatNumber(tuning.favourite_cache_ttl_seconds, 1)}}s`],
        ["Fetch Concurrency", formatNumber(tuning.favourite_fetch_concurrency)],
        ["Broadcast Interval", `${{formatNumber(tuning.broadcast_interval_seconds, 1)}}s`],
        ["Metrics Interval", `${{formatNumber(tuning.metrics_interval_seconds, 1)}}s`],
        ["History Interval", `${{formatNumber(tuning.history_interval_seconds, 1)}}s`],
        ["Retention", `${{formatNumber(tuning.history_points)}} points / ${{formatNumber(tuning.history_retention_hours, 1)}}h`],
      ];
      target.innerHTML = entries.map(([label, value]) => `<span class="chip"><strong>${{label}}:</strong> ${{value}}</span>`).join("");
    }}

    function renderBreakdown() {{
      const target = document.getElementById("snapshot-breakdown");
      const snap = state.snapshot || {{}};
      const rows = [
        ["Known clients", formatNumber(snap.known_clients)],
        ["PTV departures", formatNumber(snap.ptv_departures)],
        ["PTV runs", formatNumber(snap.ptv_runs)],
        ["PTV directions", formatNumber(snap.ptv_directions)],
        ["PTV search", formatNumber(snap.ptv_search)],
        ["History samples", `${{formatNumber(snap.history_points || 0)}} / ${{formatNumber(snap.history_capacity || 0)}}`],
        ["Dashboard host", snap.internal_dashboard_host || "internal"],
      ];
      target.innerHTML = rows.map(([label, value]) => `<div class="kv-row"><span>${{label}}</span><strong>${{value}}</strong></div>`).join("");
    }}

    function renderClientActivity() {{
      const target = document.getElementById("client-activity");
      const clients = (state.snapshot && state.snapshot.client_activity) || [];
      if (!clients.length) {{
        target.innerHTML = '<div class="chart-placeholder">No anonymous clients seen yet</div>';
        return;
      }}
      target.innerHTML = clients.map((client) => {{
        const status = client.active_subscriber ? "subscriber" : (client.active_connections > 0 ? "connected" : "idle");
        return `
          <div class="client-row">
            <div class="client-head">
              <div class="client-label">${{client.label}}</div>
              <div class="client-kind">${{client.scope_kind === "client_id" ? "client id" : "ip fallback"}}</div>
            </div>
            <div class="client-meta">
              <span>Status: ${{status}}</span>
              <span>Sockets: ${{formatNumber(client.active_connections)}}</span>
              <span>Buttons: ${{formatNumber(client.active_buttons)}}</span>
              <span>WS queries: ${{formatNumber(client.ws_queries)}}</span>
              <span>Reconnects: ${{formatNumber(client.connections_opened)}}</span>
              <span>Hour queries: ${{formatNumber(client.queries_last_hour)}}</span>
              <span>Hour reconnects: ${{formatNumber(client.reconnects_last_hour)}}</span>
              <span>IP hash: ${{client.ip_fingerprint}}</span>
            </div>
          </div>
        `;
      }}).join("");
    }}

    function renderClientLeaderboard() {{
      const target = document.getElementById("client-leaderboard");
      const rows = (state.snapshot && state.snapshot.client_leaderboard) || [];
      if (!rows.length) {{
        target.innerHTML = '<div class="chart-placeholder">No client activity recorded in the last hour</div>';
        return;
      }}
      target.innerHTML = rows.map((client, index) => `
        <div class="leaderboard-row">
          <div class="leaderboard-rank">#${{index + 1}}</div>
          <div class="leaderboard-main">
            <div class="leaderboard-label">${{client.label}}</div>
            <div class="leaderboard-sub">${{client.scope_kind === "client_id" ? "client id" : "ip fallback"}} • ${{client.ip_fingerprint}}</div>
          </div>
          <div class="leaderboard-stats">
            <div>${{formatNumber(client.queries_last_hour)}} queries / hr</div>
            <div>${{formatNumber(client.reconnects_last_hour)}} reconnects / hr</div>
          </div>
        </div>
      `).join("");
    }}

    function linePath(points, width, height, minValue, maxValue, key) {{
      if (!points.length) return "";
      const range = maxValue - minValue || 1;
      return points.map((point, index) => {{
        const x = points.length === 1 ? width / 2 : (index / (points.length - 1)) * width;
        const raw = Number(point[key] || 0);
        const y = height - ((raw - minValue) / range) * height;
        return `${{index === 0 ? "M" : "L"}}${{x.toFixed(2)}},${{y.toFixed(2)}}`;
      }}).join(" ");
    }}

    function renderChart(containerId, options) {{
      const target = document.getElementById(containerId);
      const points = state.history || [];
      if (!points.length) {{
        target.innerHTML = '<div class="chart-placeholder">Awaiting first retained sample</div>';
        return;
      }}

      const width = 520;
      const height = 170;
      const paddingTop = 10;
      const allValues = options.series.flatMap((series) => points.map((point) => Number(point[series.key] || 0)));
      const maxValue = Math.max(...allValues, options.maxFloor || 0);
      const minValue = options.forceZero ? 0 : Math.min(...allValues, 0);
      const rangeTop = maxValue === minValue ? maxValue + 1 : maxValue;
      const grid = [0.25, 0.5, 0.75].map((ratio) => {{
        const y = paddingTop + ratio * height;
        return `<line x1="0" y1="${{y}}" x2="${{width}}" y2="${{y}}" stroke="var(--grid)" stroke-width="1" />`;
      }}).join("");

      const paths = options.series.map((series) => {{
        const d = linePath(points, width, height, minValue, rangeTop, series.key);
        const last = Number(points[points.length - 1][series.key] || 0);
        const lastX = points.length === 1 ? width / 2 : width;
        const safeRange = rangeTop - minValue || 1;
        const lastY = paddingTop + height - ((last - minValue) / safeRange) * height;
        return `
          <path d="${{d}}" fill="none" stroke="${{series.color}}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"></path>
          <circle cx="${{lastX}}" cy="${{lastY.toFixed(2)}}" r="4.5" fill="${{series.color}}"></circle>
        `;
      }}).join("");

      target.innerHTML = `
        <svg class="chart-frame" viewBox="0 0 ${{width}} ${{height + paddingTop + 8}}" preserveAspectRatio="none" aria-hidden="true">
          <g transform="translate(0, ${{paddingTop}})">
            ${{grid}}
            ${{paths}}
          </g>
        </svg>
        <div class="legend">
          ${{options.series.map((series) => `<span class="legend-item"><span class="legend-swatch" style="background:${{series.color}}"></span>${{series.label}}</span>`).join("")}}
        </div>
      `;
    }}

    function renderCharts() {{
      const stamp = formatChartStamp(state.history || []);
      setText("cache-hit-meta", stamp);
      setText("upstream-rps-meta", stamp);
      setText("keys-subs-meta", stamp);
      renderChart("chart-cache-hit-rate", {{
        forceZero: true,
        maxFloor: 100,
        series: [
          {{ key: "cache_hit_rate", label: "Cache hit %", color: "var(--amber)" }},
        ],
      }});
      renderChart("chart-upstream-rps", {{
        forceZero: true,
        maxFloor: 1,
        series: [
          {{ key: "upstream_rps", label: "Upstream RPS", color: "var(--teal)" }},
        ],
      }});
      renderChart("chart-keys-subs", {{
        forceZero: true,
        maxFloor: 1,
        series: [
          {{ key: "unique_keys", label: "Unique keys", color: "var(--amber-soft)" }},
          {{ key: "subscribers", label: "Subscribers", color: "var(--rose)" }},
        ],
      }});
    }}

    async function refreshSnapshot() {{
      const response = await fetch("/internal/metrics", {{ cache: "no-store" }});
      if (!response.ok) throw new Error(`snapshot ${{response.status}}`);
      state.snapshot = await response.json();
      render();
    }}

    async function refreshHistory() {{
      const response = await fetch("/internal/metrics/history", {{ cache: "no-store" }});
      if (!response.ok) throw new Error(`history ${{response.status}}`);
      const payload = await response.json();
      state.history = payload.points || [];
      render();
    }}

    function render() {{
      renderStats();
      renderTuning();
      renderBreakdown();
      renderClientActivity();
      renderClientLeaderboard();
      renderCharts();
    }}

    render();
    window.setInterval(() => refreshSnapshot().catch((error) => console.error("dashboard snapshot refresh failed", error)), POLL_MS);
    window.setInterval(() => refreshHistory().catch((error) => console.error("dashboard history refresh failed", error)), HISTORY_MS);
  </script>
</body>
</html>"""


def _cleanup_rate_bucket(bucket: deque[float], now: float) -> None:
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()


def _check_rate_limit(store: dict[str, deque[float]], key: str, limit: int) -> bool:
    if limit <= 0:
        return True
    now = time.time()
    bucket = store[key]
    _cleanup_rate_bucket(bucket, now)
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def _record_metric(name: str, amount: int = 1) -> None:
    _metrics_window[name] += amount


def _record_broadcast_metrics(
    duration_seconds: float,
    unique_keys: int,
    subscribers: int,
    active_clients: int,
) -> None:
    _metrics_window["broadcast_iterations"] += 1
    _metrics_window["broadcast_duration_sum"] += duration_seconds
    _metrics_window["broadcast_duration_max"] = max(
        _metrics_window["broadcast_duration_max"], duration_seconds
    )
    _metrics_window["last_unique_keys"] = unique_keys
    _metrics_window["last_subscribers"] = subscribers
    _metrics_window["last_active_clients"] = active_clients
    _latest_metrics_snapshot["unique_keys"] = unique_keys
    _latest_metrics_snapshot["subscribers"] = subscribers
    _latest_metrics_snapshot["active_clients"] = active_clients


def _snapshot_and_reset_metrics() -> dict[str, float]:
    snapshot = dict(_metrics_window)
    _metrics_window["departure_cache_hits"] = 0
    _metrics_window["departure_cache_misses"] = 0
    _metrics_window["broadcast_iterations"] = 0
    _metrics_window["broadcast_duration_sum"] = 0.0
    _metrics_window["broadcast_duration_max"] = 0.0
    return snapshot


def _client_ip_from_request(request: Request) -> str:
    return request.client.host if request.client and request.client.host else "unknown"


def _client_ip_from_websocket(websocket: WebSocket) -> str:
    return websocket.client.host if websocket.client and websocket.client.host else "unknown"


def _key_fingerprint(llm_key: str | None) -> str:
    if not llm_key:
        return "anonymous"
    return hashlib.sha256(llm_key.encode("utf-8")).hexdigest()[:16]


def _scoped_session_id(session_id: str, client_scope: str, llm_key: str | None) -> str:
    return f"{client_scope}:{_key_fingerprint(llm_key)}:{session_id}"


def _state_is_connected(state: object | None) -> bool:
    if state is None:
        return True
    if state == WebSocketState.CONNECTED:
        return True
    value = getattr(state, "value", None)
    if value is not None:
        return value == WebSocketState.CONNECTED.value
    return getattr(state, "name", None) == "CONNECTED"


def _is_websocket_active(websocket: WebSocket) -> bool:
    return _state_is_connected(getattr(websocket, "client_state", None)) and _state_is_connected(
        getattr(websocket, "application_state", None)
    )


def _release_websocket_connection(
    websocket: WebSocket,
    client_ip: str | None = None,
    scope_key: str | None = None,
) -> None:
    resolved_ip = _ws_client_ips.pop(websocket, None) or client_ip
    resolved_scope = _ws_connection_scopes.pop(websocket, None) or scope_key
    _ws_client_ids.pop(websocket, None)
    if resolved_ip is None or resolved_scope is None:
        return

    _ws_query_limiters.pop(resolved_scope, None)
    sockets = _ws_connections_by_scope.get(resolved_scope)
    if not sockets or websocket not in sockets:
        return

    sockets.discard(websocket)
    active_count = len(sockets)
    if active_count == 0:
        _ws_connections_by_scope.pop(resolved_scope, None)

    activity = _touch_client_activity(resolved_scope, resolved_ip, None)
    activity["active_connections"] = active_count
    logger.info(
        "Released websocket for %s (%d/%d active)",
        activity["label"],
        active_count,
        MAX_WS_CONNECTIONS_PER_IP,
    )


def _cleanup_websocket_state(
    websocket: WebSocket,
    client_ip: str | None = None,
    scope_key: str | None = None,
    *,
    log_reason: str | None = None,
) -> None:
    if websocket in _favourite_subscriptions:
        del _favourite_subscriptions[websocket]
    _cancel_watch_task(websocket)
    _release_websocket_connection(websocket, client_ip, scope_key)
    if log_reason:
        logger.info(log_reason)


def _prune_stale_websocket_connections(scope_key: str) -> None:
    sockets = list(_ws_connections_by_scope.get(scope_key, set()))
    stale = [websocket for websocket in sockets if not _is_websocket_active(websocket)]
    for websocket in stale:
        _cleanup_websocket_state(
            websocket,
            scope_key=scope_key,
            log_reason=f"Pruned stale websocket for {scope_key}",
        )


async def _evict_existing_websocket_connections(scope_key: str) -> None:
    sockets = list(_ws_connections_by_scope.get(scope_key, set()))
    if not sockets:
        return

    for websocket in sockets:
        try:
            if _is_websocket_active(websocket):
                await websocket.close(code=1012)
        except Exception as exc:
            logger.warning("Error closing superseded websocket for %s: %s", scope_key, exc)
        finally:
            _cleanup_websocket_state(
                websocket,
                scope_key=scope_key,
                log_reason=f"Superseded websocket for {scope_key} with a newer connection",
            )


def _register_websocket_connection(
    websocket: WebSocket,
    client_ip: str,
    client_id: str | None,
    scope_key: str,
) -> bool:
    _prune_stale_websocket_connections(scope_key)
    sockets = _ws_connections_by_scope[scope_key]
    current = len(sockets)
    if current >= MAX_WS_CONNECTIONS_PER_IP:
        activity = _touch_client_activity(scope_key, client_ip, client_id)
        logger.warning(
            "Rejecting websocket for %s (%d/%d active)",
            activity["label"],
            current,
            MAX_WS_CONNECTIONS_PER_IP,
        )
        return False
    sockets.add(websocket)
    _ws_client_ips[websocket] = client_ip
    _ws_connection_scopes[websocket] = scope_key
    _ws_client_ids[websocket] = client_id
    activity = _record_client_activity_event(scope_key, client_ip, client_id, "connection_timestamps")
    activity["connections_opened"] = int(activity["connections_opened"]) + 1
    activity["active_connections"] = len(sockets)
    logger.info(
        "Registered websocket for %s (%d/%d active)",
        activity["label"],
        len(sockets),
        MAX_WS_CONNECTIONS_PER_IP,
    )
    return True


def _active_favourite_subscriptions() -> list[tuple[WebSocket, list[dict]]]:
    active: list[tuple[WebSocket, list[dict]]] = []
    for websocket, buttons in list(_favourite_subscriptions.items()):
        if _is_websocket_active(websocket):
            active.append((websocket, buttons))
        else:
            _cleanup_websocket_state(
                websocket,
                log_reason="Removed inactive client from favourite subscriptions",
            )
    return active


async def _send_favourite_updates(websocket: WebSocket, updates: list[dict], chunk_size: int = 1) -> None:
    """Send favourite updates in small chunks for Pebble-friendly startup."""
    if not updates:
        await websocket.send_json({
            "type": "favourite_update",
            "updates": [],
        })
        return

    for i in range(0, len(updates), max(1, chunk_size)):
        await websocket.send_json({
            "type": "favourite_update",
            "updates": updates[i:i + max(1, chunk_size)],
        })


async def _send_favourite_updates_if_active(websocket: WebSocket, updates: list[dict]) -> bool:
    if not _is_websocket_active(websocket):
        return False
    try:
        await _send_favourite_updates(websocket, updates)
        return True
    except Exception:
        if not _is_websocket_active(websocket):
            return False
        raise


def _validate_query_text(query_text: str) -> str:
    query_text = query_text.strip()
    if not query_text:
        raise ValueError("Empty query")
    if len(query_text) > MAX_QUERY_LENGTH:
        raise ValueError(f"Query too long (max {MAX_QUERY_LENGTH} characters)")
    return query_text


def _clamp_button_configs(buttons: list[dict]) -> list[dict]:
    if not isinstance(buttons, list):
        return []
    return buttons[:MAX_FAVOURITE_BUTTONS]


def _resolve_allowed_trip_pairs(stop_id: int, dest_id: int | None, route_type: int) -> set[tuple[int, int]] | None:
    """Resolve all valid (route_id, direction_id) pairs for a saved start/end selection."""
    if dest_id is None:
        return None

    patterns = tools.resolve_trip_patterns(stop_id, dest_id, route_type)
    return set((p["route_id"], p["direction_id"]) for p in patterns)


def _fallback_direction_match(
    departure: dict,
    allowed_trip_pairs: set[tuple[int | None, int | None]] | None,
    route_type: int,
) -> bool:
    """Allow through-routed metro services when the live route_id differs from the saved destination route."""
    if route_type != RouteType.TRAIN or not allowed_trip_pairs:
        return False

    direction_id = departure.get("direction_id")
    if direction_id is None:
        return False

    allowed_direction_ids = {allowed_direction_id for _route_id, allowed_direction_id in allowed_trip_pairs}
    return direction_id in allowed_direction_ids


def _extract_vehicle_position(run_data: dict) -> tuple[float, float] | None:
    """Extract vehicle position from PTV run response."""
    if not isinstance(run_data, dict):
        return None

    candidates = []
    if "runs" in run_data and isinstance(run_data.get("runs"), list) and run_data["runs"]:
        candidates.append(run_data["runs"][0].get("vehicle_position"))
    if "vehicle_position" in run_data:
        candidates.append(run_data.get("vehicle_position"))
    if "run" in run_data and isinstance(run_data.get("run"), dict):
        candidates.append(run_data["run"].get("vehicle_position"))

    for vp in candidates:
        if not isinstance(vp, dict):
            continue
        lat = vp.get("latitude") if vp.get("latitude") is not None else vp.get("lat")
        lon = vp.get("longitude") if vp.get("longitude") is not None else vp.get("lon")
        if lon is None and vp.get("lng") is not None:
            lon = vp.get("lng")
        if lat is not None and lon is not None:
            try:
                return float(lat), float(lon)
            except Exception:
                return None
    return None


def _extract_vehicle_desc(run_data: dict) -> str | None:
    """Extract vehicle descriptor from PTV run response."""
    if not isinstance(run_data, dict):
        return None

    candidates = []
    if "runs" in run_data and isinstance(run_data.get("runs"), list) and run_data["runs"]:
        candidates.append(run_data["runs"][0].get("vehicle_descriptor"))
    if "vehicle_descriptor" in run_data:
        candidates.append(run_data.get("vehicle_descriptor"))
    if "run" in run_data and isinstance(run_data.get("run"), dict):
        candidates.append(run_data["run"].get("vehicle_descriptor"))

    for vd in candidates:
        if not isinstance(vd, dict):
            continue
        desc = vd.get("description") or vd.get("name") or vd.get("vehicle_description")
        if desc:
            return _normalize_vehicle_desc(str(desc))
    return None


def _normalize_vehicle_desc(desc: str) -> str:
    """Normalize known vehicle descriptions."""
    normalized = desc.strip()
    mapping = {
        "3 Car Silver Hitachi": "7 Car HCMT",
    }
    return mapping.get(normalized, normalized)


async def _get_run_position(run_ref: int, route_type: int) -> dict | None:
    """Fetch run position with short-lived cache."""
    now = time.time()
    cache_key = (run_ref, route_type)
    cached = _run_position_cache.get(cache_key)
    if cached and (now - cached["fetched_at"]) < RUN_POSITION_TTL:
        return cached

    try:
        data = await ptv_client.get_run(
            route_type,
            run_ref,
            expand=["VehiclePosition", "VehicleDescriptor"],
        )
        vehicle_pos = _extract_vehicle_position(data)
        vehicle_desc = _extract_vehicle_desc(data)
        result = {
            "vehicle_pos": vehicle_pos,
            "vehicle_desc": vehicle_desc,
            "fetched_at": now,
        }
        _run_position_cache[cache_key] = result
        return result
    except Exception as e:
        logger.warning("Run position fetch error: %s", e)
        return None


def _cancel_watch_task(websocket: WebSocket) -> None:
    task = _watch_tasks.pop(websocket, None)
    if task and not task.done():
        task.cancel()


async def _watch_position_loop(
    websocket: WebSocket,
    run_ref: int,
    route_type: int,
    route_id: int | None,
    direction_id: int | None,
    stop_id: int,
):
    while True:
        try:
            if not _is_websocket_active(websocket):
                break

            distance_km = None
            vehicle_desc = None

            if route_type == RouteType.TRAIN and run_ref:
                run_data = await _get_run_position(run_ref, route_type)
                if run_data:
                    vehicle_desc = run_data.get("vehicle_desc")
                    pos = run_data.get("vehicle_pos")
                    if pos:
                        lat, lon = pos
                        distance_km = route_geometry.distance_to_stop(
                            route_id, direction_id, lat, lon, stop_id
                        )

            await websocket.send_json({
                "type": "position_update",
                "distance_km": distance_km,
                "vehicle_desc": vehicle_desc,
            })
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Position update error: %s", e)
            _cleanup_websocket_state(websocket, log_reason="Stopped position watch after websocket error")
            break

        await asyncio.sleep(POSITION_BROADCAST_INTERVAL)


# --- Live Favourite Broadcast ---

async def capture_metrics_loop():
    """Capture normalized operational metrics for the internal dashboard."""
    interval = max(10.0, METRICS_LOG_INTERVAL_SECONDS)
    history_interval = max(10.0, METRICS_HISTORY_INTERVAL_SECONDS)
    if abs(history_interval - interval) > 0.001:
        logger.warning(
            "METRICS_HISTORY_INTERVAL_SECONDS=%.1f does not match METRICS_LOG_INTERVAL_SECONDS=%.1f; using %.1f second samples",
            history_interval,
            interval,
            interval,
        )
    while True:
        await asyncio.sleep(interval)
        _capture_metrics_snapshot(interval)


async def fetch_departure_for_button(
    stop_id: int,
    route_type: int,
    direction_id: int | None,
    dest_id: int | None = None,
    max_departures: int = 3
) -> dict:
    """
    Fetch next departures for a stop/destination selection, using cache if fresh.
    Returns {departures: [{minutes, platform, departure_time}, ...], disruption_label, fetched_at} or error dict.
    Client can switch between cached departures as trains pass, reducing API calls.
    """
    cache_key = (stop_id, route_type, direction_id, dest_id)
    now = time.time()
    
    # Check cache
    cached = _departure_cache.get(cache_key)
    if cached and (now - cached["fetched_at"]) < FAVOURITE_CACHE_TTL:
        _record_metric("departure_cache_hits")
        return cached
    _record_metric("departure_cache_misses")
    
    # Fetch from PTV API
    try:
        async with _favourite_fetch_semaphore:
            data = await ptv_client.get_departures(
                route_type,
                stop_id,
                max_results=10,
                expand=["Direction", "Run", "Disruption"],
            )
        departures = data.get("departures", [])
        disruptions = data.get("disruptions", {})
        
        if not departures:
            result = {"departures": [], "disruption_label": None, "fetched_at": now}
            _departure_cache[cache_key] = result
            return result
        
        now_utc = datetime.now(timezone.utc)
        collected = []
        filtered_departures = []
        fallback_departures = []
        allowed_trip_pairs = _resolve_allowed_trip_pairs(stop_id, dest_id, route_type)

        if dest_id is not None and not allowed_trip_pairs:
            result = {"departures": [], "disruption_label": None, "fetched_at": now}
            _departure_cache[cache_key] = result
            return result
        
        for d in departures:
            if allowed_trip_pairs is not None:
                dep_pair = (d.get("route_id"), d.get("direction_id"))
                if dep_pair in allowed_trip_pairs:
                    filtered_departures.append(d)
                elif _fallback_direction_match(d, allowed_trip_pairs, route_type):
                    fallback_departures.append(d)
                else:
                    continue
            elif direction_id is not None and d.get("direction_id") != direction_id:
                continue
            else:
                filtered_departures.append(d)
            
            dep_str = d.get("estimated_departure_utc") or d.get("scheduled_departure_utc")
            if not dep_str:
                continue
            
            dep_time = datetime.fromisoformat(dep_str.replace("Z", "+00:00"))
            if dep_time > now_utc:
                minutes = int((dep_time - now_utc).total_seconds() / 60)
                minutes = max(0, min(720, minutes))
                platform = d.get("platform_number")
                run_ref = d.get("run_ref") or d.get("run_id")
                
                collected.append({
                    "minutes": minutes,
                    "platform": str(platform) if platform else None,
                    "departure_time": dep_time.isoformat(),
                    "run_ref": run_ref,
                    "route_id": d.get("route_id"),
                    "direction_id": d.get("direction_id"),
                    "route_type": route_type,
                })
                
                if len(collected) >= max_departures:
                    break

        if allowed_trip_pairs is not None and not filtered_departures and fallback_departures:
            filtered_departures = fallback_departures
            collected = []

            for d in fallback_departures:
                dep_str = d.get("estimated_departure_utc") or d.get("scheduled_departure_utc")
                if not dep_str:
                    continue

                dep_time = datetime.fromisoformat(dep_str.replace("Z", "+00:00"))
                if dep_time > now_utc:
                    minutes = int((dep_time - now_utc).total_seconds() / 60)
                    minutes = max(0, min(720, minutes))
                    platform = d.get("platform_number")
                    run_ref = d.get("run_ref") or d.get("run_id")

                    collected.append({
                        "minutes": minutes,
                        "platform": str(platform) if platform else None,
                        "departure_time": dep_time.isoformat(),
                        "run_ref": run_ref,
                        "route_id": d.get("route_id"),
                        "direction_id": d.get("direction_id"),
                        "route_type": route_type,
                    })

                    if len(collected) >= max_departures:
                        break
        
        result = {
            "departures": collected,
            "disruption_label": _summarize_favourite_disruption(
                filtered_departures,
                disruptions,
                stop_id,
                dest_id,
                route_type,
                allowed_trip_pairs,
            ),
            "fetched_at": now,
        }
        _departure_cache[cache_key] = result
        return result
        
    except Exception as e:
        logger.warning("Favourite fetch error: %s", e)
        return {"departures": [], "disruption_label": None, "fetched_at": now}


async def broadcast_favourite_updates():
    """
    Background task that broadcasts departure updates to all subscribed clients every 15 seconds.
    """
    while True:
        await asyncio.sleep(FAVOURITE_BROADCAST_INTERVAL)
        loop_started = time.perf_counter()
        
        active_subscriptions = _active_favourite_subscriptions()
        active_subscriber_scopes = {
            _ws_connection_scopes[ws]
            for ws, _buttons in active_subscriptions
            if ws in _ws_connection_scopes
        }
        active_client_count = sum(
            1 for sockets in _ws_connections_by_scope.values() if any(_is_websocket_active(ws) for ws in sockets)
        )

        # Collect all unique stop/direction combos from connected clients
        all_buttons: dict[tuple, list[tuple[WebSocket, int]]] = {}  # cache_key -> [(ws, button_id), ...]

        # Connected clients
        for ws, buttons in active_subscriptions:
            for btn in buttons:
                cache_key = (
                    btn["stop_id"],
                    btn.get("route_type", 0),
                    btn.get("direction_id"),
                    btn.get("dest_id")
                )
                if cache_key not in all_buttons:
                    all_buttons[cache_key] = []
                all_buttons[cache_key].append((ws, btn["button_id"]))
        
        # Skip if nothing to do
        if not all_buttons:
            _record_broadcast_metrics(0.0, 0, len(active_subscriber_scopes), active_client_count)
            continue
        
        # Fetch all unique stops concurrently, with a shared semaphore.
        async def fetch_one(cache_key: tuple) -> tuple[tuple, dict]:
            stop_id, route_type, direction_id, dest_id = cache_key
            return cache_key, await fetch_departure_for_button(
                stop_id, route_type, direction_id, dest_id
            )

        fetch_results: dict[tuple, dict] = {}
        fetch_batches = await asyncio.gather(
            *(fetch_one(cache_key) for cache_key in all_buttons.keys()),
            return_exceptions=True,
        )
        for item in fetch_batches:
            if isinstance(item, Exception):
                logger.warning("Broadcast fetch batch error: %s", item)
                continue
            cache_key, result = item
            fetch_results[cache_key] = result
        
        # Build per-client update messages
        client_updates: dict[WebSocket, list[dict]] = {}
        for cache_key, clients in all_buttons.items():
            result = fetch_results.get(cache_key, {"departures": []})
            departures = result.get("departures", [])
            disruption_label = result.get("disruption_label")
            for ws, button_id in clients:
                if ws not in client_updates:
                    client_updates[ws] = []
                client_updates[ws].append({
                    "button_id": button_id,
                    "departures": departures,  # Array of {minutes, platform, departure_time}
                    "disruption_label": disruption_label,
                })
        
        # Broadcast to each connected client
        disconnected = []
        for ws, updates in client_updates.items():
            try:
                await _send_favourite_updates(ws, updates)
            except Exception as e:
                logger.warning("Broadcast error: %s", e)
                disconnected.append(ws)
        
        # Clean up disconnected clients
        for ws in disconnected:
            _cleanup_websocket_state(ws, log_reason="Removed disconnected client from favourite subscriptions")

        _record_broadcast_metrics(
            time.perf_counter() - loop_started,
            len(all_buttons),
            len({
                _ws_connection_scopes[ws]
                for ws, _buttons in _active_favourite_subscriptions()
                if ws in _ws_connection_scopes
            }),
            sum(1 for sockets in _ws_connections_by_scope.values() if any(_is_websocket_active(ws) for ws in sockets)),
        )


def start_broadcast_task():
    """Start the background broadcast task if not already running."""
    global _broadcast_task
    if _broadcast_task is None or _broadcast_task.done():
        _broadcast_task = asyncio.create_task(broadcast_favourite_updates())
        logger.info("Favourite broadcast task started")


def stop_broadcast_task():
    """Stop the background broadcast task."""
    global _broadcast_task
    if _broadcast_task and not _broadcast_task.done():
        _broadcast_task.cancel()
        _broadcast_task = None
        logger.info("Favourite broadcast task stopped")


def start_metrics_task():
    """Start periodic metrics capture if not already running."""
    global _metrics_task
    if _metrics_task is None or _metrics_task.done():
        _metrics_task = asyncio.create_task(capture_metrics_loop())
        logger.info("Metrics task started")


def stop_metrics_task():
    """Stop periodic metrics logging."""
    global _metrics_task
    if _metrics_task and not _metrics_task.done():
        _metrics_task.cancel()
        _metrics_task = None
        logger.info("Metrics task stopped")


# --- Helper: resolve LLM API key ---

def _resolve_llm_key(provided_key: str | None) -> str | None:
    """Return the user-provided key, or None if not provided (BYOK only)."""
    if not provided_key:
        return None
    provided_key = provided_key.strip()
    return provided_key if provided_key else None


# --- Endpoints ---


@app.on_event("startup")
async def startup_start_broadcast():
    """Start the broadcast task and preload route geometry."""
    global _latest_metrics_snapshot
    _metrics_history.clear()
    _latest_metrics_snapshot = _empty_metrics_snapshot()
    await ptv_client.startup()
    start_broadcast_task()
    start_metrics_task()
    logger.info("Broadcast task started on startup")
    route_geometry.load_train_routes()


@app.on_event("shutdown")
async def shutdown_background_tasks():
    """Stop background tasks and close shared clients."""
    stop_metrics_task()
    stop_broadcast_task()
    await ptv_client.shutdown()


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def host_root(request: Request):
    host = _request_host(request)
    if _is_internal_dashboard_host(host):
        return HTMLResponse(
            content=_render_dashboard_html(_metrics_snapshot_payload(), _metrics_history_payload()),
            headers={"Cache-Control": "no-store"},
        )
    if host == PUBLIC_BASE_HOST:
        return RedirectResponse(
            url=PUBLIC_APPSTORE_URL,
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/internal/metrics", include_in_schema=False)
async def internal_metrics(request: Request):
    _require_internal_dashboard_request(request)
    return JSONResponse(
        content=_metrics_snapshot_payload(),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/internal/metrics/history", include_in_schema=False)
async def internal_metrics_history(request: Request):
    _require_internal_dashboard_request(request)
    return JSONResponse(
        content=_metrics_history_payload(),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/v1/health")
async def public_health():
    return JSONResponse(
        content={
            "ok": True,
            "service": "ptv-notify",
            "timestamp": _utc_isoformat(),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/v1/stations")
async def get_stations(type: str = "train"):
    """
    Get stations from the database. Open access — no API key required.
    type: 'train' (metro), 'vline', 'tram', or 'all'
    Returns schema with route/direction data for filtering.
    """
    base_dir = os.path.dirname(__file__)
    
    # Map type to file(s)
    type_files = {
        "train": ["stations_train.json"],
        "vline": ["stations_vline.json"],
        "tram": ["stops_tram.json"],
        "all": ["stations_train.json", "stations_vline.json", "stops_tram.json"],
    }
    
    files = type_files.get(type, ["stations_train.json"])
    all_stops = []
    
    for filename in files:
        filepath = os.path.join(base_dir, filename)
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)
                    all_stops.extend(data.get("stops", []))
            except Exception as e:
                logger.warning("Error reading %s: %s", filename, e)
    
    return {"stations": all_stops}


@app.post("/api/v1/favourite")
async def favourite_departure(req: FavouriteRequest, request: Request):
    """
    Quick departure check for pre-configured buttons. Open access.
    Returns next departure info.
    """
    client_ip = _client_ip_from_request(request)
    normalized_client_id = _require_client_id(req.client_id)
    _touch_client_activity(_client_scope_key(client_ip, normalized_client_id), client_ip, normalized_client_id)
    if not _check_rate_limit(_http_favourite_limiters, client_ip, HTTP_FAVOURITE_RATE_LIMIT):
        raise HTTPException(status_code=429, detail="Too many favourite requests. Please slow down.")

    if not req.stop_id:
        return {"vibration": [100, 100], "message": "Not configured"}

    try:
        data = await ptv_client.get_departures(req.route_type, req.stop_id, max_results=10, expand=["Direction"])
        departures = data.get("departures", [])

        if not departures:
            return {"vibration": [200, 200], "message": "No services"}

        now_utc = datetime.now(timezone.utc)
        allowed_trip_pairs = _resolve_allowed_trip_pairs(req.stop_id, req.dest_id, req.route_type)

        if req.dest_id is not None and not allowed_trip_pairs:
            return {"vibration": [200, 200], "message": "No services"}

        for d in departures:
            if allowed_trip_pairs is not None:
                dep_pair = (d.get("route_id"), d.get("direction_id"))
                if dep_pair not in allowed_trip_pairs:
                    continue
            elif req.direction_id is not None and d.get("direction_id") != req.direction_id:
                continue

            dep_str = d.get("estimated_departure_utc") or d.get("scheduled_departure_utc")
            if not dep_str:
                continue

            dep_time = datetime.fromisoformat(dep_str.replace("Z", "+00:00"))
            if dep_time > now_utc:
                minutes = int((dep_time - now_utc).total_seconds() / 60)
                minutes = max(0, min(720, minutes))

                vehicle = {RouteType.TRAIN: "train", RouteType.TRAM: "tram", RouteType.BUS: "bus",
                           RouteType.VLINE: "train", RouteType.NIGHT_BUS: "bus"}.get(req.route_type, "service")

                return {
                    "message": "Arriving Now" if minutes == 0 else f"Next {vehicle} in {minutes} min"
                }

        return {"vibration": [200, 200, 200], "message": "No future services"}

    except Exception as e:
        logger.warning("Favourite error: %s", e)
        return {"vibration": [500, 100, 500], "message": "Error"}


@app.post("/api/v1/query")
async def text_query(agent_request: AgentRequest, request: Request):
    """
    Text-only agent endpoint. Requires a BYOK LLM API key.
    """
    client_ip = _client_ip_from_request(request)
    normalized_client_id = _require_client_id(agent_request.client_id)
    scope_key = _client_scope_key(client_ip, normalized_client_id)
    activity = _record_client_activity_event(scope_key, client_ip, normalized_client_id, "query_timestamps")
    activity["ws_queries"] = int(activity["ws_queries"]) + 1
    if not _check_rate_limit(_http_query_limiters, client_ip, HTTP_QUERY_RATE_LIMIT):
        raise HTTPException(status_code=429, detail="Too many AI queries. Please slow down.")

    llm_key = _resolve_llm_key(agent_request.llm_api_key)
    if not llm_key:
        raise HTTPException(status_code=401, detail="Anthropic API key required. Provide llm_api_key in request body.")

    try:
        query_text = _validate_query_text(agent_request.query)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    session_id = agent_request.session_id or str(uuid.uuid4())
    scoped_session_id = _scoped_session_id(session_id, scope_key, llm_key)

    # Convert query_history from Pydantic models to dicts
    history_list = [h.model_dump() for h in agent_request.query_history] if agent_request.query_history else []

    # Run speculative fetch with client-provided history
    prefetched = await tools.speculative_fetch(history_list)
    prefetched_context = tools.format_speculative_context(prefetched)

    result = await agent_engine.run_agent(query_text, scoped_session_id, prefetched_context, llm_api_key=llm_key)

    # Extract learned stop
    payload = result.get("payload", {})
    learned_stop = None
    if result.get("type") == "RESULT":
        departure = payload.get("departure")
        if departure:
            if hasattr(departure, "model_dump"):
                departure = departure.model_dump()

        if payload.get("_stop_info"):
            learned_stop = payload.pop("_stop_info")

    return {
        "status": "success",
        "session_id": session_id,
        "learned_stop": learned_stop,
        "data": result
    }


# --- WebSocket Endpoint ---

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    buttons: str | None = None,
    client_id: str | None = None,
):
    """
    WebSocket endpoint for real-time communication. Open access for departure data.
    
    Buttons: Pass as query param ?buttons=1:STOP_ID:ROUTE_TYPE:DIR_ID:DEST_ID,...
    If provided, server immediately fetches and pushes departure data on connection.
    
    Message Protocol:
    
    Client -> Server:
    {
        "type": "query",
        "id": "1",                    // Correlation ID
        "text": "next train from richmond",
        "session_id": "abc123",       // Optional
        "llm_api_key": "sk-ant-...",  // Required for agent queries
        "query_history": [...]        // Optional, for speculative fetch
    }
    
    Server -> Client:
    {
        "type": "result",             // or "clarification", "error"
        "id": "1",
        "data": { ... },              // Agent response payload
        "learned_stop": { ... }       // Optional
    }
    """
    client_ip = _client_ip_from_websocket(websocket)
    normalized_client_id = _normalize_client_id(client_id)
    if not normalized_client_id:
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "id": None,
            "error": "client_id is required"
        })
        await websocket.close(code=1008)
        return
    scope_key = _client_scope_key(client_ip, normalized_client_id)
    await _evict_existing_websocket_connections(scope_key)

    if not _register_websocket_connection(websocket, client_ip, normalized_client_id, scope_key):
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "id": None,
            "error": "Too many active connections from this client"
        })
        await websocket.close(code=1008)
        return

    await websocket.accept()
    await websocket.send_json({
        "type": "connected",
        "id": None,
    })
    
    # Parse buttons from query param and immediately push departure data
    # Format: "1:STOP_ID:ROUTE_TYPE:DIR_ID:DEST_ID,2:STOP_ID:ROUTE_TYPE:DIR_ID:DEST_ID"
    if buttons:
        try:
            parsed_buttons = []
            for btn_str in buttons.split(","):
                parts = btn_str.split(":")
                if len(parts) >= 2:
                    button_id = int(parts[0])
                    stop_id = int(parts[1])
                    route_type = int(parts[2]) if len(parts) > 2 else 0
                    direction_id = int(parts[3]) if len(parts) > 3 and parts[3] else None
                    dest_id = int(parts[4]) if len(parts) > 4 and parts[4] else None
                    parsed_buttons.append({
                        "button_id": button_id,
                        "stop_id": stop_id,
                        "route_type": route_type,
                        "direction_id": direction_id,
                        "dest_id": dest_id
                    })
            
            if parsed_buttons:
                parsed_buttons = _clamp_button_configs(parsed_buttons)
                # Register subscription for connected client
                _favourite_subscriptions[websocket] = parsed_buttons
                
                start_broadcast_task()
                logger.info("Client connected with %d buttons in URL", len(parsed_buttons))
                
                # Fetch and push initial data in background (non-blocking)
                # Fetch all buttons in PARALLEL for speed
                async def push_initial_data():
                    try:
                        fetch_tasks = [
                            fetch_departure_for_button(
                                btn["stop_id"], btn.get("route_type", 0), btn.get("direction_id"), btn.get("dest_id")
                            )
                            for btn in parsed_buttons
                        ]
                        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
                        
                        initial_updates = []
                        for btn, result in zip(parsed_buttons, results):
                            if isinstance(result, Exception):
                                result = {"departures": [], "disruption_label": None}
                            initial_updates.append({
                                "button_id": btn["button_id"],
                                "departures": result.get("departures", []),
                                "disruption_label": result.get("disruption_label"),
                            })
                        
                        sent = await _send_favourite_updates_if_active(websocket, initial_updates)
                        if not sent:
                            logger.info("Skipped initial favourite push for inactive websocket")
                    except Exception as e:
                        logger.warning("Error pushing initial favourite data: %s", e)
                        _cleanup_websocket_state(
                            websocket,
                            client_ip,
                            scope_key,
                            log_reason="Initial favourite push failed; cleaned up websocket state",
                        )
                
                asyncio.create_task(push_initial_data())
        except Exception as e:
            logger.warning("Error parsing buttons: %s", e)
    
    try:
        while True:
            # Receive message
            try:
                raw_message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=WS_IDLE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.info("WebSocket idle timeout for %s", _client_scope_label(client_ip, normalized_client_id))
                try:
                    await websocket.close(code=1001)
                except Exception:
                    pass
                break
            
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "id": None,
                    "error": "Invalid JSON"
                })
                continue
            
            msg_type = message.get("type")
            msg_id = message.get("id")
            
            if msg_type == "query":
                # Handle text query — requires LLM key
                try:
                    query_text = _validate_query_text(message.get("text", ""))
                except ValueError as e:
                    await websocket.send_json({
                        "type": "error",
                        "id": msg_id,
                        "error": str(e)
                    })
                    continue

                session_id = message.get("session_id") or str(uuid.uuid4())
                raw_query_history = message.get("query_history", [])
                query_history = raw_query_history[:5] if isinstance(raw_query_history, list) else []
                
                if not _check_rate_limit(
                    _ws_query_limiters,
                    scope_key,
                    WS_QUERY_RATE_LIMIT,
                ):
                    await websocket.send_json({
                        "type": "error",
                        "id": msg_id,
                        "error": "Too many AI queries on this connection. Please wait a moment."
                    })
                    continue

                activity = _record_client_activity_event(
                    scope_key,
                    client_ip,
                    normalized_client_id,
                    "query_timestamps",
                )
                activity["ws_queries"] = int(activity["ws_queries"]) + 1

                resolved_key = _resolve_llm_key(message.get("llm_api_key"))
                
                if not resolved_key:
                    await websocket.send_json({
                        "type": "error",
                        "id": msg_id,
                        "error": "Anthropic API key required for agent queries. Configure in settings."
                    })
                    continue
                
                if not query_text:
                    await websocket.send_json({
                        "type": "error",
                        "id": msg_id,
                        "error": "Empty query"
                    })
                    continue
                
                try:
                    # Run speculative fetch with client-provided history
                    prefetched = await tools.speculative_fetch(query_history)
                    prefetched_context = tools.format_speculative_context(prefetched)

                    scoped_session_id = _scoped_session_id(session_id, client_ip, resolved_key)
                    
                    # Run agent with resolved key
                    result = await agent_engine.run_agent(query_text, scoped_session_id, prefetched_context, llm_api_key=resolved_key)
                    
                    # Extract learned stop if present
                    learned_stop = None
                    button_config = None
                    payload = result.get("payload", {})
                    
                    if result.get("type") == "RESULT":
                        departure = payload.get("departure")
                        if departure:
                            if hasattr(departure, "model_dump"):
                                departure = departure.model_dump()
                        
                        if payload.get("_stop_info"):
                            learned_stop = payload.pop("_stop_info")
                        
                        # Extract button config
                        if result.get("_button_config"):
                            button_config = result.pop("_button_config")
                        elif payload.get("_button_config"):
                            button_config = payload.pop("_button_config")
                    
                    # Send response
                    response = {
                        "type": result.get("type", "RESULT").lower(),
                        "id": msg_id,
                        "session_id": session_id,
                        "data": result,
                        "learned_stop": learned_stop
                    }
                    if button_config:
                        response["button_config"] = button_config
                    
                    if _is_websocket_active(websocket):
                        await websocket.send_json(response)
                    else:
                        logger.info("WebSocket closed before response could be sent (state: %s)", websocket.client_state)
                    
                except Exception as e:
                    logger.warning("WebSocket query error: %s", e)
                    try:
                        if _is_websocket_active(websocket):
                            await websocket.send_json({
                                "type": "error",
                                "id": msg_id,
                                "error": str(e)
                            })
                    except Exception:
                        pass  # Connection already closed
            
            elif msg_type == "ping":
                # Health check
                await websocket.send_json({
                    "type": "pong",
                    "id": msg_id
                })
            
            elif msg_type == "favourite":
                # Direct departure fetch - no LLM, filtered by destination when available
                stop_id = message.get("stop_id")
                route_type = message.get("route_type", RouteType.TRAIN)
                direction_id = message.get("direction_id")
                dest_id = message.get("dest_id")
                
                if not stop_id:
                    await websocket.send_json({
                        "type": "favourite_result",
                        "id": msg_id,
                        "message": "Not configured"
                    })
                    continue
                
                try:
                    data = await ptv_client.get_departures(route_type, stop_id, max_results=10, expand=["Direction"])
                    departures = data.get("departures", [])
                    
                    if not departures:
                        await websocket.send_json({
                            "type": "favourite_result",
                            "id": msg_id,
                            "message": "No services"
                        })
                        continue
                    
                    now_utc = datetime.now(timezone.utc)
                    found = False
                    allowed_trip_pairs = _resolve_allowed_trip_pairs(stop_id, dest_id, route_type)

                    if dest_id is not None and not allowed_trip_pairs:
                        await websocket.send_json({
                            "type": "favourite_result",
                            "id": msg_id,
                            "message": "No future services"
                        })
                        continue
                    
                    for d in departures:
                        if allowed_trip_pairs is not None:
                            dep_pair = (d.get("route_id"), d.get("direction_id"))
                            if dep_pair not in allowed_trip_pairs:
                                continue
                        elif direction_id is not None and d.get("direction_id") != direction_id:
                            continue
                        
                        dep_str = d.get("estimated_departure_utc") or d.get("scheduled_departure_utc")
                        if not dep_str:
                            continue
                        
                        dep_time = datetime.fromisoformat(dep_str.replace("Z", "+00:00"))
                        if dep_time > now_utc:
                            minutes = int((dep_time - now_utc).total_seconds() / 60)
                            minutes = max(0, min(720, minutes))
                            platform = d.get("platform_number")
                            
                            vehicle = {RouteType.TRAIN: "train", RouteType.TRAM: "tram", RouteType.BUS: "bus",
                                       RouteType.VLINE: "train", RouteType.NIGHT_BUS: "bus"}.get(route_type, "service")
                            
                            if minutes == 0:
                                msg = "Now"
                            else:
                                msg = f"{minutes} min"
                            
                            if platform:
                                msg += f" • P{platform}"
                                if route_type == RouteType.TRAM:
                                    msg += " • Tram"
                            else:
                                if route_type == RouteType.TRAM:
                                    msg += " • Tram"
                                elif route_type == RouteType.VLINE:
                                    msg += " • V/Line"
                                else:
                                    msg += " • Train"
                            
                            await websocket.send_json({
                                "type": "favourite_result",
                                "id": msg_id,
                                "message": msg,
                                "minutes": minutes,
                                "platform": str(platform) if platform else None
                            })
                            found = True
                            break
                    
                    if not found:
                        await websocket.send_json({
                            "type": "favourite_result",
                            "id": msg_id,
                            "message": "No future services"
                        })
                        
                except Exception as e:
                    print(f"WebSocket favourite error: {e}")
                    await websocket.send_json({
                        "type": "favourite_result",
                        "id": msg_id,
                        "message": "Error"
                    })

            elif msg_type == "watch_start":
                run_ref = message.get("run_ref")
                stop_id = message.get("stop_id")
                route_type = message.get("route_type", RouteType.TRAIN)
                route_id = message.get("route_id")
                direction_id = message.get("direction_id")

                if not run_ref or stop_id is None:
                    await websocket.send_json({
                        "type": "error",
                        "id": msg_id,
                        "error": "watch_start requires run_ref and stop_id"
                    })
                    continue

                try:
                    run_ref = int(run_ref)
                    stop_id = int(stop_id)
                    route_type = int(route_type) if route_type is not None else RouteType.TRAIN
                    route_id = int(route_id) if route_id is not None else None
                    direction_id = int(direction_id) if direction_id is not None else None
                except Exception:
                    await websocket.send_json({
                        "type": "error",
                        "id": msg_id,
                        "error": "watch_start invalid numeric fields"
                    })
                    continue

                _cancel_watch_task(websocket)
                _watch_tasks[websocket] = asyncio.create_task(
                    _watch_position_loop(
                        websocket,
                        run_ref,
                        route_type,
                        route_id,
                        direction_id,
                        stop_id,
                    )
                )

            elif msg_type == "watch_stop":
                _cancel_watch_task(websocket)
            
            elif msg_type == "subscribe_favourites":
                # Subscribe to live favourite updates
                buttons = _clamp_button_configs(message.get("buttons", []))
                valid_buttons = []
                
                for btn in buttons:
                    if btn.get("stop_id"):
                        valid_buttons.append({
                            "button_id": btn.get("button_id"),
                            "stop_id": btn.get("stop_id"),
                            "route_type": btn.get("route_type", 0),
                            "direction_id": btn.get("direction_id"),
                            "dest_id": btn.get("dest_id")
                        })
                
                if valid_buttons:
                    _favourite_subscriptions[websocket] = valid_buttons
                    start_broadcast_task()
                    logger.info("Client subscribed to %d favourite buttons", len(valid_buttons))
                    
                    # Fetch and push initial data in background (non-blocking, parallel)
                    async def push_subscribe_data():
                        try:
                            fetch_tasks = [
                                fetch_departure_for_button(
                                    btn["stop_id"], btn.get("route_type", 0), btn.get("direction_id"), btn.get("dest_id")
                                )
                                for btn in valid_buttons
                            ]
                            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
                            
                            initial_updates = []
                            for btn, result in zip(valid_buttons, results):
                                if isinstance(result, Exception):
                                    result = {"departures": [], "disruption_label": None}
                                initial_updates.append({
                                    "button_id": btn["button_id"],
                                    "departures": result.get("departures", []),
                                    "disruption_label": result.get("disruption_label"),
                                })
                            
                            sent = await _send_favourite_updates_if_active(websocket, initial_updates)
                            if not sent:
                                logger.info("Skipped subscribed favourite push for inactive websocket")
                        except Exception as e:
                            logger.warning("Error pushing subscribe favourite data: %s", e)
                            _cleanup_websocket_state(
                                websocket,
                                client_ip,
                                scope_key,
                                log_reason="Favourite subscription push failed; cleaned up websocket state",
                            )
                    
                    asyncio.create_task(push_subscribe_data())
                
                await websocket.send_json({
                    "type": "favourites_subscribed",
                    "id": msg_id,
                    "buttons": len(valid_buttons)
                })
            
            else:
                await websocket.send_json({
                    "type": "error",
                    "id": msg_id,
                    "error": f"Unknown message type: {msg_type}"
                })
                
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.warning("WebSocket error: %s", e)
    finally:
        _cleanup_websocket_state(websocket, client_ip, scope_key)


# Serve Pebble config from pebble/config/settings.html (single source of truth)
pebble_config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pebble", "config", "settings.html")

@app.get("/pebble-config.html")
async def pebble_config():
    """Serve the Pebble configuration page."""
    if os.path.exists(pebble_config_path):
        with open(pebble_config_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="text/html; charset=utf-8")
    raise HTTPException(status_code=404, detail="Pebble config not found")
