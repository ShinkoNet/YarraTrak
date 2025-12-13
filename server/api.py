"""
PTV Notify API - FastAPI server for public transport queries.
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


# --- Endpoints ---

@app.get("/api/v1/search")
async def search_stops_api(q: str):
    """
    Search for stops by name. Used by settings page for button configuration.
    Returns simplified results suitable for the config UI.
    """
    if not q or len(q) < 2:
        return {"stops": []}
    
    try:
        data = await ptv_client.search(q)
        stops = data.get("stops", [])
        
        # Filter for trains/trams only, format for config page
        type_names = {
            RouteType.TRAIN: "Train",
            RouteType.TRAM: "Tram",
            RouteType.VLINE: "V/Line",
        }
        
        results = []
        for s in stops[:10]:
            route_type = s.get("route_type")
            if route_type in type_names:
                results.append({
                    "name": s.get("stop_name"),
                    "stop_id": s.get("stop_id"),
                    "route_type": route_type,
                    "type_name": type_names[route_type]
                })
        
        return {"stops": results}
    except Exception as e:
        print(f"Search error: {e}")
        return {"stops": [], "error": str(e)}


@app.get("/api/v1/stations")
async def get_stations(type: str = "train"):
    """
    Get all stations from the baked-in database.
    type: 'train' or 'tram' (default: train)
    """
    db_path = os.path.join(os.path.dirname(__file__), "stations_db.json")
    if not os.path.exists(db_path):
        return {"stations": []}
        
    try:
        with open(db_path, "r") as f:
            data = json.load(f)
            
        stations = data.get("stations", [])
        
        # Filter if needed (currently DB is mostly train/vline)
        # We might want to filter by route_type if we mix them later
        # 0=Train, 1=Tram, 3=VLine
        
        target_types = [0, 3] # Default to Train+Vline
        if type == "tram":
            target_types = [1]
            
        filtered = [
            s for s in stations 
            if s.get("route_type") in target_types
        ]
        
        return {"stations": filtered}
    except Exception as e:
        print(f"Error reading station DB: {e}")
        return {"stations": [], "error": str(e)}


@app.post("/api/v1/stealth")
async def stealth_mode(req: StealthRequest):
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
    background_tasks: BackgroundTasks = None
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
async def text_query(request: AgentRequest, background_tasks: BackgroundTasks):
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
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time communication.
    
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
                            
                            vehicle = {RouteType.TRAIN: "train", RouteType.TRAM: "tram", RouteType.BUS: "bus",
                                       RouteType.VLINE: "train", RouteType.NIGHT_BUS: "bus"}.get(route_type, "service")
                            
                            await websocket.send_json({
                                "type": "stealth_result",
                                "id": msg_id,
                                "vibration": calculate_vibration(minutes),
                                "message": "Arriving Now" if minutes == 0 else f"Next {vehicle} in {minutes} min",
                                "minutes": minutes
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
            
            else:
                await websocket.send_json({
                    "type": "error",
                    "id": msg_id,
                    "error": f"Unknown message type: {msg_type}"
                })
                
    except WebSocketDisconnect:
        print("WebSocket client disconnected")
    except Exception as e:
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

