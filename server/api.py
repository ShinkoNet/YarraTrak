"""
PTV Notify API - FastAPI server for public transport queries.

Open-data architecture: departure/station endpoints are unauthenticated.
Agent (LLM) endpoints use Bring-Your-Own-Key (BYOK) via Anthropic API key.
"""

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
import json
from pydantic import BaseModel
from datetime import datetime, timezone
import os
import asyncio
import time
import uuid

from .enums import RouteType
from . import agent_engine
from . import tools
from .ptv_client import PTVClient
from . import route_geometry

app = FastAPI(title="PTV Notify", version="1.0.0")

# GZip compression for large responses (station databases)
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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

# --- Button Configurations (for MCP/agent setup) ---
# This allows the agent to configure favourite buttons via set_button WebSocket message
_button_configs: dict[str, dict] = {}

# --- Live Favourite Updates ---
# Subscription registry: websocket -> list of button configs with start/destination info
_favourite_subscriptions: dict[WebSocket, list[dict]] = {}

# Shared departure cache: (stop_id, route_type, direction_id, dest_id) -> {departures: [...], fetched_at}
# With multi-departure caching, we can use longer TTL - client switches between cached departures
_departure_cache: dict[tuple, dict] = {}
FAVOURITE_CACHE_TTL = 30.0  # seconds
FAVOURITE_BROADCAST_INTERVAL = 15.0  # seconds

# Background task reference
_broadcast_task: asyncio.Task | None = None

# Run position cache
_run_position_cache: dict[tuple, dict] = {}
RUN_POSITION_TTL = 8.0  # seconds
POSITION_BROADCAST_INTERVAL = 5.0  # seconds

# Watch position tasks (per websocket)
_watch_tasks: dict[WebSocket, asyncio.Task] = {}


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
        print(f"Run position fetch error: {e}")
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
            print(f"Position update error: {e}")

        await asyncio.sleep(POSITION_BROADCAST_INTERVAL)


# --- Live Favourite Broadcast ---

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
        return cached
    
    # Fetch from PTV API
    try:
        data = await ptv_client.get_departures(route_type, stop_id, max_results=10, expand=["Direction", "Run"])
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
        print(f"Favourite fetch error: {e}")
        return {"departures": [], "fetched_at": now}


async def broadcast_favourite_updates():
    """
    Background task that broadcasts departure updates to all subscribed clients every 15 seconds.
    """
    while True:
        await asyncio.sleep(FAVOURITE_BROADCAST_INTERVAL)
        
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
            continue
        
        # Fetch all unique stops
        fetch_results: dict[tuple, dict] = {}
        for cache_key in all_buttons.keys():
            stop_id, route_type, direction_id, dest_id = cache_key
            fetch_results[cache_key] = await fetch_departure_for_button(stop_id, route_type, direction_id, dest_id)
        
        # Build per-client update messages
        client_updates: dict[WebSocket, list[dict]] = {}
        for cache_key, clients in all_buttons.items():
            result = fetch_results[cache_key]
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
                await ws.send_json({
                    "type": "favourite_update",
                    "updates": updates
                })
            except Exception as e:
                print(f"Broadcast error: {e}")
                disconnected.append(ws)
        
        # Clean up disconnected clients
        for ws in disconnected:
            if ws in _favourite_subscriptions:
                del _favourite_subscriptions[ws]
                print("Removed disconnected client from favourite subscriptions")


def start_broadcast_task():
    """Start the background broadcast task if not already running."""
    global _broadcast_task
    if _broadcast_task is None or _broadcast_task.done():
        _broadcast_task = asyncio.create_task(broadcast_favourite_updates())
        print("Favourite broadcast task started")


def stop_broadcast_task():
    """Stop the background broadcast task."""
    global _broadcast_task
    if _broadcast_task and not _broadcast_task.done():
        _broadcast_task.cancel()
        _broadcast_task = None
        print("Favourite broadcast task stopped")


# --- Helper: resolve LLM API key ---

def _resolve_llm_key(provided_key: str | None) -> str | None:
    """Return the user-provided key, or None if not provided (BYOK only)."""
    return provided_key if provided_key else None


# --- Endpoints ---


@app.on_event("startup")
async def startup_start_broadcast():
    """Start the broadcast task on server startup to keep registry buttons warm."""
    start_broadcast_task()
    print("Broadcast task started on startup")
    route_geometry.load_train_routes()


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
                print(f"Error reading {filename}: {e}")
    
    return {"stations": all_stops}


@app.post("/api/v1/favourite")
async def favourite_departure(req: FavouriteRequest):
    """
    Quick departure check for pre-configured buttons. Open access.
    Returns next departure info.
    """
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
        print(f"Favourite error: {e}")
        return {"vibration": [500, 100, 500], "message": "Error"}


@app.post("/api/v1/query")
async def text_query(request: AgentRequest):
    """
    Text-only agent endpoint. Requires LLM API key (BYOK) or server fallback.
    """
    llm_key = _resolve_llm_key(request.llm_api_key)
    if not llm_key:
        raise HTTPException(status_code=401, detail="Anthropic API key required. Provide llm_api_key in request body.")

    session_id = request.session_id or str(uuid.uuid4())

    # Convert query_history from Pydantic models to dicts
    history_list = [h.model_dump() for h in request.query_history] if request.query_history else []

    # Run speculative fetch with client-provided history
    prefetched = await tools.speculative_fetch(history_list)
    prefetched_context = tools.format_speculative_context(prefetched)

    result = await agent_engine.run_agent(request.query, session_id, prefetched_context, llm_api_key=llm_key)

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
async def websocket_endpoint(websocket: WebSocket, buttons: str = Query(None), llm_api_key: str = Query(None)):
    """
    WebSocket endpoint for real-time communication. Open access for departure data.
    
    Buttons: Pass as query param ?buttons=1:STOP_ID:ROUTE_TYPE:DIR_ID:DEST_ID,...
    If provided, server immediately fetches and pushes departure data on connection.
    
    llm_api_key: Optional Anthropic API key for agent queries (BYOK).
    
    Message Protocol:
    
    Client -> Server:
    {
        "type": "query",
        "id": "1",                    // Correlation ID
        "text": "next train from richmond",
        "session_id": "abc123",       // Optional
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
    await websocket.accept()
    
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
                # Register subscription for connected client
                _favourite_subscriptions[websocket] = parsed_buttons
                
                start_broadcast_task()
                print(f"Client connected with {len(parsed_buttons)} buttons in URL")
                
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
                        
                        await websocket.send_json({
                            "type": "favourite_update",
                            "updates": initial_updates
                        })
                    except Exception as e:
                        print(f"Error pushing initial favourite data: {e}")
                
                asyncio.create_task(push_initial_data())
        except Exception as e:
            print(f"Error parsing buttons: {e}")
    
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
                query_text = message.get("text", "").strip()
                session_id = message.get("session_id") or str(uuid.uuid4())
                query_history = message.get("query_history", [])
                
                # Resolve LLM key: message field > WS query param > server fallback
                msg_llm_key = message.get("llm_api_key") or llm_api_key
                resolved_key = _resolve_llm_key(msg_llm_key)
                
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
                    
                    # Run agent with resolved key
                    result = await agent_engine.run_agent(query_text, session_id, prefetched_context, llm_api_key=resolved_key)
                    
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
                        print(f"WebSocket closed before response could be sent (state: {websocket.client_state})")
                    
                except Exception as e:
                    print(f"WebSocket query error: {e}")
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
            
            elif msg_type == "get_buttons":
                # Return button configurations stored on server
                await websocket.send_json({
                    "type": "buttons",
                    "id": msg_id,
                    "buttons": _button_configs
                })
            
            elif msg_type == "set_button":
                # Agent can set button config (for MCP use)
                button_id = message.get("button_id")
                if button_id is None or button_id < 1 or button_id > 10:
                    await websocket.send_json({
                        "type": "error",
                        "id": msg_id,
                        "error": "button_id must be 1-10"
                    })
                    continue
                
                _button_configs[str(button_id)] = {
                    "name": message.get("name", f"Button {button_id}"),
                    "stop_id": message.get("stop_id"),
                    "stop_name": message.get("stop_name"),
                    "dest_id": message.get("dest_id"),
                    "dest_name": message.get("dest_name"),
                    "route_type": message.get("route_type", RouteType.TRAIN),
                    "direction_id": message.get("direction_id"),
                    "direction_name": message.get("direction_name")
                }
                
                await websocket.send_json({
                    "type": "button_set",
                    "id": msg_id,
                    "button_id": button_id,
                    "config": _button_configs[str(button_id)]
                })
            
            elif msg_type == "subscribe_favourites":
                # Subscribe to live favourite updates
                buttons = message.get("buttons", [])
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
                    print(f"Client subscribed to {len(valid_buttons)} favourite buttons")
                    
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
                            
                            await websocket.send_json({
                                "type": "favourite_update",
                                "updates": initial_updates
                            })
                        except Exception as e:
                            print(f"Error pushing subscribe favourite data: {e}")
                    
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
        print("WebSocket client disconnected")
    except Exception as e:
        # Clean up subscription on error
        if websocket in _favourite_subscriptions:
            del _favourite_subscriptions[websocket]
        _cancel_watch_task(websocket)
        print(f"WebSocket error: {e}")


# Serve Pebble config from pebble/config/settings.html (single source of truth)
pebble_config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pebble", "config", "settings.html")

@app.get("/pebble-config.html")
async def pebble_config():
    """Serve the Pebble configuration page."""
    if os.path.exists(pebble_config_path):
        with open(pebble_config_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="text/html; charset=utf-8")
    raise HTTPException(status_code=404, detail="Pebble config not found")
