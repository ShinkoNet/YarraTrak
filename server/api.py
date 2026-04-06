"""
PTV Notify API - FastAPI server for public transport queries.

Open-data architecture: departure/station endpoints are unauthenticated.
Agent (LLM) endpoints use Bring-Your-Own-Key (BYOK) via Anthropic API key.
"""

from collections import defaultdict, deque
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
import json
from pydantic import BaseModel
from datetime import datetime, timezone
import hashlib
import logging
import os
import asyncio
import time
import uuid

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
    HTTP_QUERY_RATE_LIMIT,
    MAX_FAVOURITE_BUTTONS,
    MAX_QUERY_LENGTH,
    MAX_WS_CONNECTIONS_PER_IP,
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

class StopHistory(BaseModel):
    stop_id: int
    stop_name: str
    route_type: int

class AgentRequest(BaseModel):
    query: str
    session_id: str | None = None
    query_history: list[StopHistory] | None = None
    llm_api_key: str | None = None

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
_ws_connections_by_ip: dict[str, int] = defaultdict(int)
_favourite_fetch_semaphore = asyncio.Semaphore(FAVOURITE_FETCH_LIMIT)
_metrics_window = {
    "departure_cache_hits": 0,
    "departure_cache_misses": 0,
    "broadcast_iterations": 0,
    "broadcast_duration_sum": 0.0,
    "broadcast_duration_max": 0.0,
    "last_unique_keys": 0,
    "last_subscribers": 0,
}


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


def _record_broadcast_metrics(duration_seconds: float, unique_keys: int, subscribers: int) -> None:
    _metrics_window["broadcast_iterations"] += 1
    _metrics_window["broadcast_duration_sum"] += duration_seconds
    _metrics_window["broadcast_duration_max"] = max(
        _metrics_window["broadcast_duration_max"], duration_seconds
    )
    _metrics_window["last_unique_keys"] = unique_keys
    _metrics_window["last_subscribers"] = subscribers


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


def _register_websocket_connection(client_ip: str) -> bool:
    current = _ws_connections_by_ip[client_ip]
    if current >= MAX_WS_CONNECTIONS_PER_IP:
        return False
    _ws_connections_by_ip[client_ip] = current + 1
    return True


def _release_websocket_connection(client_ip: str) -> None:
    current = _ws_connections_by_ip.get(client_ip, 0)
    if current <= 1:
        _ws_connections_by_ip.pop(client_ip, None)
    else:
        _ws_connections_by_ip[client_ip] = current - 1


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
            if websocket.client_state.value != 1:
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

        await asyncio.sleep(POSITION_BROADCAST_INTERVAL)


# --- Live Favourite Broadcast ---

async def log_metrics_loop():
    """Emit compact operational metrics for favourite refresh load."""
    interval = max(10.0, METRICS_LOG_INTERVAL_SECONDS)
    while True:
        await asyncio.sleep(interval)
        api_metrics = _snapshot_and_reset_metrics()
        ptv_metrics = ptv_client.snapshot_and_reset_metrics()
        cache_lookups = api_metrics["departure_cache_hits"] + api_metrics["departure_cache_misses"]
        cache_hit_rate = (
            (api_metrics["departure_cache_hits"] / cache_lookups) * 100.0
            if cache_lookups
            else 0.0
        )
        upstream_total = sum(ptv_metrics.values())
        avg_loop_ms = (
            (api_metrics["broadcast_duration_sum"] / api_metrics["broadcast_iterations"]) * 1000.0
            if api_metrics["broadcast_iterations"]
            else 0.0
        )
        logger.info(
            "Metrics: subscribers=%d unique_keys=%d loops=%d avg_loop_ms=%.1f max_loop_ms=%.1f "
            "cache_hit_rate=%.1f%% cache_hits=%d cache_misses=%d upstream_rps=%.2f "
            "ptv_departures=%d ptv_runs=%d ptv_directions=%d ptv_search=%d",
            api_metrics["last_subscribers"],
            api_metrics["last_unique_keys"],
            api_metrics["broadcast_iterations"],
            avg_loop_ms,
            api_metrics["broadcast_duration_max"] * 1000.0,
            cache_hit_rate,
            api_metrics["departure_cache_hits"],
            api_metrics["departure_cache_misses"],
            upstream_total / interval,
            ptv_metrics.get("departures", 0),
            ptv_metrics.get("runs", 0),
            ptv_metrics.get("directions", 0),
            ptv_metrics.get("search", 0),
        )


async def fetch_departure_for_button(
    stop_id: int,
    route_type: int,
    direction_id: int | None,
    dest_id: int | None = None,
    max_departures: int = 3
) -> dict:
    """
    Fetch next departures for a stop/destination selection, using cache if fresh.
    Returns {departures: [{minutes, platform, departure_time}, ...], fetched_at} or error dict.
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
                expand=["Direction", "Run"],
            )
        departures = data.get("departures", [])
        
        if not departures:
            result = {"departures": [], "fetched_at": now}
            _departure_cache[cache_key] = result
            return result
        
        now_utc = datetime.now(timezone.utc)
        collected = []
        allowed_trip_pairs = _resolve_allowed_trip_pairs(stop_id, dest_id, route_type)

        if dest_id is not None and not allowed_trip_pairs:
            result = {"departures": [], "fetched_at": now}
            _departure_cache[cache_key] = result
            return result
        
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
        
        result = {"departures": collected, "fetched_at": now}
        _departure_cache[cache_key] = result
        return result
        
    except Exception as e:
        logger.warning("Favourite fetch error: %s", e)
        return {"departures": [], "fetched_at": now}


async def broadcast_favourite_updates():
    """
    Background task that broadcasts departure updates to all subscribed clients every 15 seconds.
    """
    while True:
        await asyncio.sleep(FAVOURITE_BROADCAST_INTERVAL)
        loop_started = time.perf_counter()
        
        # Collect all unique stop/direction combos from connected clients
        all_buttons: dict[tuple, list[tuple[WebSocket, int]]] = {}  # cache_key -> [(ws, button_id), ...]
        
        # Connected clients
        for ws, buttons in list(_favourite_subscriptions.items()):
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
            _record_broadcast_metrics(0.0, 0, len(_favourite_subscriptions))
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
            for ws, button_id in clients:
                if ws not in client_updates:
                    client_updates[ws] = []
                client_updates[ws].append({
                    "button_id": button_id,
                    "departures": departures  # Array of {minutes, platform, departure_time}
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
            if ws in _favourite_subscriptions:
                del _favourite_subscriptions[ws]
                logger.info("Removed disconnected client from favourite subscriptions")

        _record_broadcast_metrics(
            time.perf_counter() - loop_started,
            len(all_buttons),
            len(_favourite_subscriptions),
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
    """Start periodic metrics logging if not already running."""
    global _metrics_task
    if _metrics_task is None or _metrics_task.done():
        _metrics_task = asyncio.create_task(log_metrics_loop())
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
    scoped_session_id = _scoped_session_id(session_id, client_ip, llm_key)

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
async def websocket_endpoint(websocket: WebSocket, buttons: str = Query(None)):
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

    if not _register_websocket_connection(client_ip):
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
                                result = {"departures": []}
                            initial_updates.append({
                                "button_id": btn["button_id"],
                                "departures": result.get("departures", [])
                            })
                        
                        await _send_favourite_updates(websocket, initial_updates)
                    except Exception as e:
                        logger.warning("Error pushing initial favourite data: %s", e)
                
                asyncio.create_task(push_initial_data())
        except Exception as e:
            logger.warning("Error parsing buttons: %s", e)
    
    try:
        while True:
            # Receive message
            raw_message = await websocket.receive_text()
            
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
                    f"{client_ip}:{id(websocket)}",
                    WS_QUERY_RATE_LIMIT,
                ):
                    await websocket.send_json({
                        "type": "error",
                        "id": msg_id,
                        "error": "Too many AI queries on this connection. Please wait a moment."
                    })
                    continue

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
                    
                    if websocket.client_state.value == 1:  # CONNECTED
                        await websocket.send_json(response)
                    else:
                        logger.info("WebSocket closed before response could be sent (state: %s)", websocket.client_state)
                    
                except Exception as e:
                    logger.warning("WebSocket query error: %s", e)
                    try:
                        if websocket.client_state.value == 1:
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
                                    result = {"departures": []}
                                initial_updates.append({
                                    "button_id": btn["button_id"],
                                    "departures": result.get("departures", [])
                                })
                            
                            await _send_favourite_updates(websocket, initial_updates)
                        except Exception as e:
                            logger.warning("Error pushing subscribe favourite data: %s", e)
                    
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
        # Clean up subscription on disconnect
        if websocket in _favourite_subscriptions:
            del _favourite_subscriptions[websocket]
        _cancel_watch_task(websocket)
        logger.info("WebSocket client disconnected")
    except Exception as e:
        # Clean up subscription on error
        if websocket in _favourite_subscriptions:
            del _favourite_subscriptions[websocket]
        _cancel_watch_task(websocket)
        logger.warning("WebSocket error: %s", e)
    finally:
        _release_websocket_connection(client_ip)


# Serve Pebble config from pebble/config/settings.html (single source of truth)
pebble_config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pebble", "config", "settings.html")

@app.get("/pebble-config.html")
async def pebble_config():
    """Serve the Pebble configuration page."""
    if os.path.exists(pebble_config_path):
        with open(pebble_config_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="text/html; charset=utf-8")
    raise HTTPException(status_code=404, detail="Pebble config not found")
