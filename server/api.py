"""
PTV Notify API - FastAPI server for public transport queries.
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, WebSocket, WebSocketDisconnect, Depends, Header, Query
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response, StreamingResponse
import json
from pydantic import BaseModel
from datetime import datetime, timezone
import os
import asyncio
import time
import uuid
import html

from .enums import RouteType
from . import agent_engine
from . import config
from . import tools
from .ptv_client import PTVClient

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

# --- API Key Authentication ---

async def verify_api_key(x_api_key: str = Header(None, alias="X-API-Key")):
    """Verify API key from header. If no keys configured, allow all (dev mode)."""
    if not config.API_KEYS:
        return  # No keys configured = open access (dev mode)
    if x_api_key not in config.API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

# --- Request Models ---

class StealthRequest(BaseModel):
    button_id: int
    stop_id: int
    stop_name: str | None = None
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

# --- Initialization ---

ptv_client = PTVClient()

# Azure TTS (optional)
speech_config = None
try:
    import azure.cognitiveservices.speech as speechsdk
    if config.AZURE_SPEECH_KEY and config.AZURE_SPEECH_REGION:
        speech_config = speechsdk.SpeechConfig(
            subscription=config.AZURE_SPEECH_KEY,
            region=config.AZURE_SPEECH_REGION
        )
        speech_config.speech_synthesis_voice_name = "en-US-AshleyNeural"
        speech_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Riff48Khz16BitMonoPcm
        )
        print("Azure TTS initialized")
except Exception as e:
    print(f"Azure TTS not available: {e}")

# --- Audio Store (Claim Check Pattern) ---

_audio_store: dict[str, dict] = {}

# --- Button Configurations (for MCP/agent setup) ---
# This allows the agent to configure stealth buttons via set_button WebSocket message
_button_configs: dict[str, dict] = {}

# --- Live Stealth Updates ---
# Subscription registry: websocket -> list of button configs with stop/direction info
_stealth_subscriptions: dict[WebSocket, list[dict]] = {}

# Shared departure cache: (stop_id, route_type, direction_id) -> {minutes, platform, message, fetched_at}
# TTL slightly less than broadcast interval to ensure fresh data
_departure_cache: dict[tuple, dict] = {}
STEALTH_CACHE_TTL = 4.0  # seconds
STEALTH_BROADCAST_INTERVAL = 5.0  # seconds

# Background task reference
_broadcast_task: asyncio.Task | None = None


def _cleanup_audio_store():
    """Remove expired audio tickets (5 min TTL)."""
    now = time.time()
    expired = [k for k, v in _audio_store.items() if now - v["created_at"] > 300]
    for k in expired:
        del _audio_store[k]


async def _generate_audio(ticket_id: str, text: str):
    """Background TTS generation."""
    if not speech_config or not text:
        _audio_store[ticket_id] = {"status": "error", "data": None, "created_at": time.time()}
        return

    try:
        import azure.cognitiveservices.speech as speechsdk

        pull_stream = speechsdk.audio.PullAudioOutputStream()
        audio_config = speechsdk.audio.AudioOutputConfig(stream=pull_stream)
        synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)

        ssml = f"""
        <speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
            <voice name="{speech_config.speech_synthesis_voice_name}">
                <prosody pitch="+30%">{html.escape(text)}</prosody>
            </voice>
        </speak>
        """

        result = await asyncio.to_thread(lambda: synthesizer.speak_ssml_async(ssml).get())

        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            _audio_store[ticket_id] = {"status": "ready", "data": result.audio_data, "created_at": time.time()}
        else:
            _audio_store[ticket_id] = {"status": "error", "data": None, "created_at": time.time()}
    except Exception as e:
        print(f"TTS error: {e}")
        _audio_store[ticket_id] = {"status": "error", "data": None, "created_at": time.time()}

    _cleanup_audio_store()


# --- Vibration Encoding ---

def calculate_vibration(minutes: int) -> list[int]:
    """
    Encode minutes as haptic pattern.
    - Hours: 1000ms on, 400ms off
    - Tens: 500ms on, 300ms off
    - Ones: 150ms on, 150ms off
    """
    if minutes == 0:
        return [80, 120, 150, 250, 80, 120, 80, 120, 150, 250, 150, 650, 150, 250, 300]

    minutes = max(0, min(720, minutes))
    hours = minutes // 60
    tens = (minutes % 60) // 10
    ones = minutes % 10

    pattern = []
    for _ in range(hours):
        pattern.extend([1000, 400])
    if hours > 0 and (tens > 0 or ones > 0) and pattern:
        pattern[-1] += 200

    for _ in range(tens):
        pattern.extend([500, 300])
    if tens > 0 and ones > 0 and pattern:
        pattern[-1] += 100

    for _ in range(ones):
        pattern.extend([150, 150])

    return pattern


# --- Live Stealth Broadcast ---

async def fetch_departure_for_button(stop_id: int, route_type: int, direction_id: int | None) -> dict:
    """
    Fetch next departure for a stop/direction, using cache if fresh.
    Returns {minutes, platform, message} or error dict.
    """
    cache_key = (stop_id, route_type, direction_id)
    now = time.time()
    
    # Check cache
    cached = _departure_cache.get(cache_key)
    if cached and (now - cached["fetched_at"]) < STEALTH_CACHE_TTL:
        return cached
    
    # Fetch from PTV API
    try:
        data = await ptv_client.get_departures(route_type, stop_id, max_results=10, expand=["Direction"])
        departures = data.get("departures", [])
        
        if not departures:
            result = {"minutes": None, "platform": None, "message": "No services", "fetched_at": now}
            _departure_cache[cache_key] = result
            return result
        
        now_utc = datetime.now(timezone.utc)
        
        for d in departures:
            if direction_id is not None and d.get("direction_id") != direction_id:
                continue
            
            dep_str = d.get("estimated_departure_utc") or d.get("scheduled_departure_utc")
            if not dep_str:
                continue
            
            dep_time = datetime.fromisoformat(dep_str.replace("Z", "+00:00"))
            if dep_time > now_utc:
                minutes = int((dep_time - now_utc).total_seconds() / 60)
                minutes = max(0, min(720, minutes))
                platform = d.get("platform_number")
                
                # Format message with platform if available
                if minutes == 0:
                    msg = "Now"
                else:
                    msg = f"{minutes} min"
                if platform:
                    msg += f" • P{platform}"
                
                # Include departure_time for client-side seconds calculation
                result = {
                    "minutes": minutes, 
                    "platform": str(platform) if platform else None, 
                    "message": msg, 
                    "departure_time": dep_time.isoformat(),
                    "fetched_at": now
                }
                _departure_cache[cache_key] = result
                return result
        
        result = {"minutes": None, "platform": None, "message": "No future", "fetched_at": now}
        _departure_cache[cache_key] = result
        return result
        
    except Exception as e:
        print(f"Stealth fetch error: {e}")
        return {"minutes": None, "platform": None, "message": "Error", "fetched_at": now}


async def broadcast_stealth_updates():
    """
    Background task that broadcasts departure updates to all subscribed clients every 5 seconds.
    """
    while True:
        await asyncio.sleep(STEALTH_BROADCAST_INTERVAL)
        
        if not _stealth_subscriptions:
            continue
        
        # Collect all unique stop/direction combos across all clients
        all_buttons: dict[tuple, list[tuple[WebSocket, int]]] = {}  # cache_key -> [(ws, button_id), ...]
        
        for ws, buttons in list(_stealth_subscriptions.items()):
            for btn in buttons:
                cache_key = (btn["stop_id"], btn.get("route_type", 0), btn.get("direction_id"))
                if cache_key not in all_buttons:
                    all_buttons[cache_key] = []
                all_buttons[cache_key].append((ws, btn["button_id"]))
        
        # Fetch all unique stops (with cache deduplication)
        fetch_results: dict[tuple, dict] = {}
        for cache_key in all_buttons:
            stop_id, route_type, direction_id = cache_key
            fetch_results[cache_key] = await fetch_departure_for_button(stop_id, route_type, direction_id)
        
        # Build per-client update messages
        client_updates: dict[WebSocket, list[dict]] = {}
        for cache_key, clients in all_buttons.items():
            result = fetch_results[cache_key]
            for ws, button_id in clients:
                if ws not in client_updates:
                    client_updates[ws] = []
                client_updates[ws].append({
                    "button_id": button_id,
                    "minutes": result.get("minutes"),
                    "platform": result.get("platform"),
                    "message": result.get("message", "--"),
                    "departure_time": result.get("departure_time")
                })
        
        # Broadcast to each client
        disconnected = []
        for ws, updates in client_updates.items():
            try:
                await ws.send_json({
                    "type": "stealth_update",
                    "updates": updates
                })
            except Exception as e:
                print(f"Broadcast error: {e}")
                disconnected.append(ws)
        
        # Clean up disconnected clients
        for ws in disconnected:
            if ws in _stealth_subscriptions:
                del _stealth_subscriptions[ws]
                print("Removed disconnected client from stealth subscriptions")


def start_broadcast_task():
    """Start the background broadcast task if not already running."""
    global _broadcast_task
    if _broadcast_task is None or _broadcast_task.done():
        _broadcast_task = asyncio.create_task(broadcast_stealth_updates())
        print("Stealth broadcast task started")


def stop_broadcast_task():
    """Stop the background broadcast task."""
    global _broadcast_task
    if _broadcast_task and not _broadcast_task.done():
        _broadcast_task.cancel()
        _broadcast_task = None
        print("Stealth broadcast task stopped")


# --- Endpoints ---




@app.get("/api/v1/stations")
async def get_stations(type: str = "train", _: None = Depends(verify_api_key)):
    """
    Get stations from the database.
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


@app.post("/api/v1/stealth")
async def stealth_mode(req: StealthRequest, _: None = Depends(verify_api_key)):
    """
    Quick departure check for pre-configured buttons.
    Returns vibration pattern encoding minutes until next departure.
    """
    if not req.stop_id:
        return {"vibration": [100, 100], "message": "Not configured"}

    try:
        data = await ptv_client.get_departures(req.route_type, req.stop_id, max_results=10, expand=["Direction"])
        departures = data.get("departures", [])

        if not departures:
            return {"vibration": [200, 200], "message": "No services"}

        now_utc = datetime.now(timezone.utc)

        for d in departures:
            if req.direction_id is not None and d.get("direction_id") != req.direction_id:
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
                    "vibration": calculate_vibration(minutes),
                    "message": "Arriving Now" if minutes == 0 else f"Next {vehicle} in {minutes} min"
                }

        return {"vibration": [200, 200, 200], "message": "No future services"}

    except Exception as e:
        print(f"Stealth error: {e}")
        return {"vibration": [500, 100, 500], "message": "Error"}


@app.post("/api/v1/voice")
async def voice_query(
    file: UploadFile = File(...),
    session_id: str | None = None,
    query_history: str | None = None,  # JSON string of [{stop_id, stop_name, route_type}, ...]
    background_tasks: BackgroundTasks = None,
    _: None = Depends(verify_api_key)
):
    """
    Voice query endpoint with speculative execution.
    Runs ASR and speculative departure fetch in parallel for lower latency.

    Args:
        file: Audio file (webm/wav)
        session_id: Session ID for conversation context
        query_history: JSON array of previous stops for speculative fetch
    """
    from groq import AsyncGroq
    import json

    if not config.GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="Groq API key not configured")

    session_id = session_id or str(uuid.uuid4())
    content = await file.read()

    # Parse query_history from JSON string
    history_list = []
    if query_history:
        try:
            history_list = json.loads(query_history)
        except json.JSONDecodeError:
            pass

    async def run_asr():
        groq = AsyncGroq(api_key=config.GROQ_API_KEY)
        result = await groq.audio.transcriptions.create(
            file=(file.filename, content),
            model="whisper-large-v3",
            response_format="json",
            language="en",
            temperature=0.0
        )
        return result.text

    async def run_speculative():
        prefetched = await tools.speculative_fetch(history_list)
        return tools.format_speculative_context(prefetched)

    try:
        # Run ASR and speculative fetch in parallel
        transcript, prefetched_context = await asyncio.gather(
            run_asr(),
            run_speculative()
        )
    except Exception as e:
        print(f"Voice query error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Run agent with pre-fetched context
    result = await agent_engine.run_agent(transcript, session_id, prefetched_context)

    # Extract TTS text based on response type
    payload = result.get("payload", {})
    tts_text = payload.get("tts_text") or payload.get("question_text") or payload.get("message", "")

    # Start TTS generation in background
    audio_ticket = None
    if tts_text and speech_config:
        audio_ticket = f"aud_{uuid.uuid4().hex[:8]}"
        _audio_store[audio_ticket] = {"status": "pending", "created_at": time.time()}
        background_tasks.add_task(_generate_audio, audio_ticket, tts_text)

    # Add vibration pattern for results and trim to first departure only
    # Extract learned_stop if this was a successful query
    learned_stop = None
    if result.get("type") == "RESULT":
        departure = payload.get("departure")
        if departure:
            # Ensure it's a dict (Pydantic model dump)
            if hasattr(departure, "model_dump"):
                departure = departure.model_dump()
            
            if departure.get("minutes_to_depart") is not None:
                payload["vibration"] = calculate_vibration(departure["minutes_to_depart"])

        # Return learned stop for client to store
        if payload.get("_stop_info"):
            learned_stop = payload.pop("_stop_info")

    return {
        "status": "success",
        "session_id": session_id,
        "transcript": transcript,
        "audio_ticket": audio_ticket,
        "learned_stop": learned_stop,
        "data": result
    }


@app.post("/api/v1/query")
async def text_query(request: AgentRequest, background_tasks: BackgroundTasks, _: None = Depends(verify_api_key)):
    """
    Text-only agent endpoint for debugging. No ASR, just sends text to agent.
    Uses client-provided query_history for speculative fetch.
    """
    session_id = request.session_id or str(uuid.uuid4())

    # Convert query_history from Pydantic models to dicts
    history_list = [h.model_dump() for h in request.query_history] if request.query_history else []

    # Run speculative fetch with client-provided history
    prefetched = await tools.speculative_fetch(history_list)
    prefetched_context = tools.format_speculative_context(prefetched)

    result = await agent_engine.run_agent(request.query, session_id, prefetched_context)

    # Extract TTS text based on response type
    payload = result.get("payload", {})
    tts_text = payload.get("tts_text") or payload.get("question_text") or payload.get("message", "")

    # Start TTS generation in background
    audio_ticket = None
    if tts_text and speech_config:
        audio_ticket = f"aud_{uuid.uuid4().hex[:8]}"
        _audio_store[audio_ticket] = {"status": "pending", "created_at": time.time()}
        background_tasks.add_task(_generate_audio, audio_ticket, tts_text)

    # Add vibration pattern for results and trim to first departure only
    learned_stop = None
    if result.get("type") == "RESULT":
        departure = payload.get("departure")
        if departure:
            # Ensure it's a dict
            if hasattr(departure, "model_dump"):
                departure = departure.model_dump()

            if departure.get("minutes_to_depart") is not None:
                payload["vibration"] = calculate_vibration(departure["minutes_to_depart"])

        # Return learned stop for client to store
        if payload.get("_stop_info"):
            learned_stop = payload.pop("_stop_info")

    return {
        "status": "success",
        "session_id": session_id,
        "audio_ticket": audio_ticket,
        "learned_stop": learned_stop,
        "data": result
    }


@app.get("/api/v1/media/{ticket_id}")
async def get_media(ticket_id: str):
    """
    Retrieve generated audio. Long-polls for up to 2 seconds.
    Returns 202 if still processing, 200 with audio when ready.
    """
    timeout = 2.0
    start = time.time()

    while time.time() - start < timeout:
        item = _audio_store.get(ticket_id)

        if not item:
            raise HTTPException(status_code=404, detail="Ticket not found")

        if item["status"] == "ready":
            return StreamingResponse(iter([item["data"]]), media_type="audio/wav")

        if item["status"] == "error":
            raise HTTPException(status_code=500, detail="Audio generation failed")

        await asyncio.sleep(0.1)

    return Response(status_code=202)


# --- WebSocket Endpoint ---

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, api_key: str = Query(None), buttons: str = Query(None)):
    """
    WebSocket endpoint for real-time communication.
    
    API Key: Pass as query param ?api_key=YOUR_KEY
    If no API keys configured in server, all connections allowed (dev mode).
    
    Buttons: Pass as query param ?buttons=1:STOP_ID:ROUTE_TYPE:DIR_ID,2:STOP_ID:ROUTE_TYPE:DIR_ID
    If provided, server immediately fetches and pushes departure data on connection.
    
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
    # Verify API key before accepting connection
    if config.API_KEYS and api_key not in config.API_KEYS:
        await websocket.close(code=4001, reason="Invalid or missing API key")
        return
    
    await websocket.accept()
    
    # Parse buttons from query param and immediately push departure data
    # Format: "1:STOP_ID:ROUTE_TYPE:DIR_ID,2:STOP_ID:ROUTE_TYPE:DIR_ID"
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
                    parsed_buttons.append({
                        "button_id": button_id,
                        "stop_id": stop_id,
                        "route_type": route_type,
                        "direction_id": direction_id
                    })
            
            if parsed_buttons:
                # Register subscription
                _stealth_subscriptions[websocket] = parsed_buttons
                start_broadcast_task()
                print(f"Client connected with {len(parsed_buttons)} buttons in URL")
                
                # Fetch and push initial data in background (non-blocking)
                # Fetch all buttons in PARALLEL for speed
                async def push_initial_data():
                    try:
                        fetch_tasks = [
                            fetch_departure_for_button(
                                btn["stop_id"], btn.get("route_type", 0), btn.get("direction_id")
                            )
                            for btn in parsed_buttons
                        ]
                        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
                        
                        initial_updates = []
                        for btn, result in zip(parsed_buttons, results):
                            if isinstance(result, Exception):
                                result = {"minutes": None, "platform": None, "message": "Error"}
                            initial_updates.append({
                                "button_id": btn["button_id"],
                                "minutes": result.get("minutes"),
                                "platform": result.get("platform"),
                                "message": result.get("message", "--"),
                                "departure_time": result.get("departure_time")
                            })
                        
                        await websocket.send_json({
                            "type": "stealth_update",
                            "updates": initial_updates
                        })
                    except Exception as e:
                        print(f"Error pushing initial stealth data: {e}")
                
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
                # Handle text query
                query_text = message.get("text", "").strip()
                session_id = message.get("session_id") or str(uuid.uuid4())
                query_history = message.get("query_history", [])
                
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
                    
                    # Run agent
                    result = await agent_engine.run_agent(query_text, session_id, prefetched_context)
                    
                    # Extract learned stop if present
                    learned_stop = None
                    button_config = None
                    payload = result.get("payload", {})
                    
                    if result.get("type") == "RESULT":
                        departure = payload.get("departure")
                        if departure:
                            if hasattr(departure, "model_dump"):
                                departure = departure.model_dump()
                            if departure.get("minutes_to_depart") is not None:
                                payload["vibration"] = calculate_vibration(departure["minutes_to_depart"])
                        
                        if payload.get("_stop_info"):
                            learned_stop = payload.pop("_stop_info")
                        
                        # Extract button config (can be at result level from short-circuit or payload level)
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
                    
                    await websocket.send_json(response)
                    
                except Exception as e:
                    print(f"WebSocket query error: {e}")
                    await websocket.send_json({
                        "type": "error",
                        "id": msg_id,
                        "error": str(e)
                    })
            
            elif msg_type == "ping":
                # Health check
                await websocket.send_json({
                    "type": "pong",
                    "id": msg_id
                })
            
            elif msg_type == "stealth":
                # Direct departure fetch - no LLM, just stop_id/route_type/direction_id
                stop_id = message.get("stop_id")
                route_type = message.get("route_type", RouteType.TRAIN)
                direction_id = message.get("direction_id")
                
                if not stop_id:
                    await websocket.send_json({
                        "type": "stealth_result",
                        "id": msg_id,
                        "vibration": [100, 100],
                        "message": "Not configured"
                    })
                    continue
                
                try:
                    data = await ptv_client.get_departures(route_type, stop_id, max_results=10, expand=["Direction"])
                    departures = data.get("departures", [])
                    
                    if not departures:
                        await websocket.send_json({
                            "type": "stealth_result",
                            "id": msg_id,
                            "vibration": [200, 200],
                            "message": "No services"
                        })
                        continue
                    
                    now_utc = datetime.now(timezone.utc)
                    found = False
                    
                    for d in departures:
                        if direction_id is not None and d.get("direction_id") != direction_id:
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
                            
                            # Build message with platform if available
                            if minutes == 0:
                                msg = "Now"
                            else:
                                msg = f"{minutes} min"
                            if platform:
                                msg += f" • P{platform}"
                            
                            await websocket.send_json({
                                "type": "stealth_result",
                                "id": msg_id,
                                "vibration": calculate_vibration(minutes),
                                "message": msg,
                                "minutes": minutes,
                                "platform": str(platform) if platform else None
                            })
                            found = True
                            break
                    
                    if not found:
                        await websocket.send_json({
                            "type": "stealth_result",
                            "id": msg_id,
                            "vibration": [200, 200, 200],
                            "message": "No future services"
                        })
                        
                except Exception as e:
                    print(f"WebSocket stealth error: {e}")
                    await websocket.send_json({
                        "type": "stealth_result",
                        "id": msg_id,
                        "vibration": [500, 100, 500],
                        "message": "Error"
                    })
            
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
                if button_id is None or button_id < 1 or button_id > 3:
                    await websocket.send_json({
                        "type": "error",
                        "id": msg_id,
                        "error": "button_id must be 1, 2, or 3"
                    })
                    continue
                
                _button_configs[str(button_id)] = {
                    "name": message.get("name", f"Button {button_id}"),
                    "stop_id": message.get("stop_id"),
                    "stop_name": message.get("stop_name"),
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
            
            elif msg_type == "subscribe_stealth":
                # Subscribe to live stealth updates
                buttons = message.get("buttons", [])
                valid_buttons = []
                
                for btn in buttons:
                    if btn.get("stop_id"):
                        valid_buttons.append({
                            "button_id": btn.get("button_id"),
                            "stop_id": btn.get("stop_id"),
                            "route_type": btn.get("route_type", 0),
                            "direction_id": btn.get("direction_id")
                        })
                
                if valid_buttons:
                    _stealth_subscriptions[websocket] = valid_buttons
                    start_broadcast_task()
                    print(f"Client subscribed to {len(valid_buttons)} stealth buttons")
                    
                    # Fetch and push initial data in background (non-blocking, parallel)
                    async def push_subscribe_data():
                        try:
                            fetch_tasks = [
                                fetch_departure_for_button(
                                    btn["stop_id"], btn.get("route_type", 0), btn.get("direction_id")
                                )
                                for btn in valid_buttons
                            ]
                            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
                            
                            initial_updates = []
                            for btn, result in zip(valid_buttons, results):
                                if isinstance(result, Exception):
                                    result = {"minutes": None, "platform": None, "message": "Error"}
                                initial_updates.append({
                                    "button_id": btn["button_id"],
                                    "minutes": result.get("minutes"),
                                    "platform": result.get("platform"),
                                    "message": result.get("message", "--"),
                                    "departure_time": result.get("departure_time")
                                })
                            
                            await websocket.send_json({
                                "type": "stealth_update",
                                "updates": initial_updates
                            })
                        except Exception as e:
                            print(f"Error pushing subscribe stealth data: {e}")
                    
                    asyncio.create_task(push_subscribe_data())
                
                await websocket.send_json({
                    "type": "stealth_subscribed",
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
        if websocket in _stealth_subscriptions:
            del _stealth_subscriptions[websocket]
        print("WebSocket client disconnected")
    except Exception as e:
        # Clean up subscription on error
        if websocket in _stealth_subscriptions:
            del _stealth_subscriptions[websocket]
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


# Mount static files
web_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
if os.path.exists(web_path):
    app.mount("/", StaticFiles(directory=web_path, html=True), name="web")

