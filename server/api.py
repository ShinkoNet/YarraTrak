from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from .enums import RouteType
from . import agent_engine
from . import config
from .ptv_client import PTVClient

import os
import asyncio
import azure.cognitiveservices.speech as speechsdk
import uuid
import base64
import html
import time
import json
from datetime import datetime, timezone

app = FastAPI()

# Allow CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Data Models ---
class StealthRequest(BaseModel):
    button_id: int
    stop_id: int
    stop_name: str | None = None
    direction_id: int | None = None
    direction_name: str | None = None
    route_type: int = RouteType.TRAIN

class AgentRequest(BaseModel):
    query: str
    session_id: str | None = None

# --- Initialization ---
ptv_client = PTVClient()

# Initialize Azure Speech
speech_config = None
if config.AZURE_SPEECH_KEY and config.AZURE_SPEECH_REGION:
    try:
        speech_config = speechsdk.SpeechConfig(subscription=config.AZURE_SPEECH_KEY, region=config.AZURE_SPEECH_REGION)
        speech_config.speech_synthesis_voice_name = "en-US-AshleyNeural"
        speech_config.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Riff48Khz16BitMonoPcm)
        print("Azure Speech SDK initialized successfully")
    except Exception as e:
        print(f"Failed to initialize Azure Speech SDK: {e}")
else:
    print("WARNING: AZURE_SPEECH_KEY or AZURE_SPEECH_REGION not set. TTS will be disabled.")

# --- Audio Claim Check Store ---
# {ticket_id: {"status": "pending"|"ready"|"error", "data": bytes, "created_at": timestamp}}
_audio_store = {}

def cleanup_audio_store():
    now = time.time()
    expired = [k for k, v in _audio_store.items() if now - v["created_at"] > 300] # 5 min TTL
    for k in expired:
        del _audio_store[k]

async def generate_audio_background(ticket_id: str, text: str):
    """Background task to generate TTS."""
    print(f"TTS Background: Starting for {ticket_id}")
    try:
        if not speech_config or not text:
            _audio_store[ticket_id] = {"status": "error", "data": None, "created_at": time.time()}
            return

        # Use PullAudioOutputStream to get audio data in memory
        pull_stream = speechsdk.audio.PullAudioOutputStream()
        audio_config = speechsdk.audio.AudioOutputConfig(stream=pull_stream)
        synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
        
        # Escape text for XML
        safe_text = html.escape(text)
        
        # Construct SSML for pitch adjustment (+30%)
        ssml = f"""
        <speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
            <voice name="{speech_config.speech_synthesis_voice_name}">
                <prosody pitch="+30%">
                    {safe_text}
                </prosody>
            </voice>
        </speak>
        """
        
        def _run_tts():
            return synthesizer.speak_ssml_async(ssml).get()

        result = await asyncio.to_thread(_run_tts)
        
        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            print(f"TTS Background: Success for {ticket_id}")
            _audio_store[ticket_id] = {
                "status": "ready", 
                "data": result.audio_data, 
                "created_at": time.time()
            }
        else:
            print(f"TTS Background: Failed for {ticket_id} - {result.reason}")
            _audio_store[ticket_id] = {"status": "error", "data": None, "created_at": time.time()}
            
    except Exception as e:
        print(f"TTS Background: Exception for {ticket_id} - {e}")
        _audio_store[ticket_id] = {"status": "error", "data": None, "created_at": time.time()}
    
    cleanup_audio_store()

# --- Helper Functions ---
def calculate_vibration(minutes: int):
    """Convert minutes to vibration pattern."""
    if minutes == 0:
        return [80, 120, 150, 250, 80, 120, 80, 120, 150, 250, 150, 650, 150, 250, 300]
        
    hours = minutes // 60
    remaining_minutes = minutes % 60
    tens = remaining_minutes // 10
    ones = remaining_minutes % 10
    
    pattern = []
    for _ in range(hours):
        pattern.extend([1000, 400])
    if hours > 0 and (tens > 0 or ones > 0):
        if pattern: pattern[-1] += 200
    for _ in range(tens):
        pattern.extend([500, 300])
    if tens > 0 and ones > 0:
        if pattern: pattern[-1] += 100
    for _ in range(ones):
        pattern.extend([150, 150])
        
    return pattern

# --- Endpoints ---

@app.post("/api/v1/stealth")
async def stealth_mode(req: StealthRequest):
    """Handle 'Stealth Mode' button presses."""
    print(f"Stealth Mode: Button {req.button_id} pressed.")
    
    if not req.stop_id:
        return {"vibration": [100, 100], "message": "Button not configured"}

    try:
        data = await ptv_client.get_departures(req.route_type, req.stop_id, max_results=10, expand=["Direction"])
        departures = data.get("departures", [])
        
        if not departures:
            return {"vibration": [200, 200], "message": "No trains"}
            
        now_utc = datetime.now(timezone.utc)
        next_dep = None
        
        for d in departures:
            if req.direction_id is not None:
                if d.get('direction_id') != req.direction_id:
                    continue
            dep_str = d.get('estimated_departure_utc') or d.get('scheduled_departure_utc')
            if not dep_str: continue
            dep_str = dep_str.replace('Z', '+00:00')
            dep_time = datetime.fromisoformat(dep_str)
            if dep_time > now_utc:
                next_dep = dep_time
                break
        
        if next_dep:
            delta = next_dep - now_utc
            minutes = int(delta.total_seconds() / 60)
            minutes = max(0, min(720, minutes))
            pattern = calculate_vibration(minutes)
            
            vehicle_types = {
                RouteType.TRAIN: "train",
                RouteType.TRAM: "tram",
                RouteType.BUS: "bus",
                RouteType.VLINE: "train",
                RouteType.NIGHT_BUS: "bus"
            }
            v_type = vehicle_types.get(req.route_type, "vehicle")
            msg = "Arriving Now" if minutes == 0 else f"Next {v_type} in {minutes} min"
            
            return {"vibration": pattern, "message": msg}
        else:
            return {"vibration": [200, 200, 200], "message": "No future services"}

    except Exception as e:
        print(f"Error in stealth_mode: {e}")
        return {"vibration": [500, 100, 500], "message": "Error fetching data"}

@app.post("/api/v1/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    """Transcribe uploaded audio file using Groq Whisper API."""
    from groq import AsyncGroq
    groq_client = AsyncGroq(api_key=config.GROQ_API_KEY) if config.GROQ_API_KEY else None
    
    if not groq_client:
        raise HTTPException(status_code=500, detail="Groq API Key not configured")

    try:
        content = await file.read()
        transcription = await groq_client.audio.transcriptions.create(
            file=(file.filename, content),
            model="whisper-large-v3",
            response_format="json",
            language="en",
            temperature=0.0
        )
        return {"text": transcription.text}
    except Exception as e:
        print(f"Transcription error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/agent/query")
async def agent_query_endpoint(request: AgentRequest, background_tasks: BackgroundTasks):
    """
    Async Agent Endpoint.
    Returns JSON immediately. Audio is generated in background.
    """
    session_id = request.session_id or str(uuid.uuid4())
    print(f"Agent Query: {request.query} (Session: {session_id})")
    
    # 1. Run the Speculative Race
    result = await agent_engine.run_speculative_race(request.query, session_id)
    
    # 2. Handle Audio Generation
    audio_ticket = None
    tts_text = ""
    
    if result.get("type") == "RESULT":
        tts_text = result.get("payload", {}).get("tts_text", "")
    elif result.get("type") == "CLARIFICATION":
        tts_text = result.get("payload", {}).get("question_text", "")
    elif result.get("type") == "ERROR":
        tts_text = result.get("payload", {}).get("message", "")
        
    if tts_text:
        audio_ticket = f"aud_{uuid.uuid4().hex[:8]}"
        _audio_store[audio_ticket] = {"status": "pending", "created_at": time.time()}
        background_tasks.add_task(generate_audio_background, audio_ticket, tts_text)
        
    # 3. Add Vibration Data if RESULT
    if result.get("type") == "RESULT":
        # Try to find minutes_to_depart in payload
        departures = result.get("payload", {}).get("departures", [])
        if departures:
            minutes = departures[0].get("minutes_to_depart")
            if minutes is not None:
                result["payload"]["vibration"] = calculate_vibration(minutes)
    
    # 4. Return Response
    return {
        "status": "success",
        "session_id": session_id,
        "audio_ticket": audio_ticket,
        "data": result
    }

@app.get("/api/v1/media/{ticket_id}")
async def get_media(ticket_id: str):
    """
    Claim Check for Audio.
    Long polls for up to 2 seconds.
    """
    start_time = time.time()
    timeout = 2.0
    
    while time.time() - start_time < timeout:
        item = _audio_store.get(ticket_id)
        
        if not item:
            raise HTTPException(status_code=404, detail="Ticket not found")
            
        if item["status"] == "ready":
            # Return audio stream
            def iterfile():
                yield item["data"]
            return StreamingResponse(iterfile(), media_type="audio/wav")
            
        if item["status"] == "error":
            raise HTTPException(status_code=500, detail="Audio generation failed")
            
        # Wait a bit
        await asyncio.sleep(0.1)
        
    # If timeout, return 202 Accepted (Client should retry)
    return Response(status_code=202)

@app.post("/api/v1/reset")
async def reset_chat():
    # In-memory store doesn't really need explicit reset for this MVP logic
    # but we can clear the session if we passed it.
    return {"message": "Conversation reset"}

# Mount Web App
web_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
if os.path.exists(web_path):
    app.mount("/", StaticFiles(directory=web_path, html=True), name="web")
