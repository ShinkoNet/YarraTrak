"""
PTV Notify API - FastAPI server for public transport queries.
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
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

class AgentRequest(BaseModel):
    query: str
    session_id: str | None = None

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


@app.post("/api/v1/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    """Transcribe audio using Groq Whisper."""
    from groq import AsyncGroq

    if not config.GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="Groq API key not configured")

    try:
        groq = AsyncGroq(api_key=config.GROQ_API_KEY)
        content = await file.read()
        result = await groq.audio.transcriptions.create(
            file=(file.filename, content),
            model="whisper-large-v3",
            response_format="json",
            language="en",
            temperature=0.0
        )
        return {"text": result.text}
    except Exception as e:
        print(f"Transcription error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/agent/query")
async def agent_query(request: AgentRequest, background_tasks: BackgroundTasks):
    """
    Main agent endpoint. Returns structured response with optional audio ticket.
    """
    session_id = request.session_id or str(uuid.uuid4())
    result = await agent_engine.run_agent(request.query, session_id)

    # Extract TTS text based on response type
    payload = result.get("payload", {})
    tts_text = payload.get("tts_text") or payload.get("question_text") or payload.get("message", "")

    # Start TTS generation in background
    audio_ticket = None
    if tts_text and speech_config:
        audio_ticket = f"aud_{uuid.uuid4().hex[:8]}"
        _audio_store[audio_ticket] = {"status": "pending", "created_at": time.time()}
        background_tasks.add_task(_generate_audio, audio_ticket, tts_text)

    # Add vibration pattern for results
    if result.get("type") == "RESULT":
        departures = payload.get("departures", [])
        if departures and departures[0].get("minutes_to_depart") is not None:
            payload["vibration"] = calculate_vibration(departures[0]["minutes_to_depart"])

    return {
        "status": "success",
        "session_id": session_id,
        "audio_ticket": audio_ticket,
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


# Mount static files
web_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
if os.path.exists(web_path):
    app.mount("/", StaticFiles(directory=web_path, html=True), name="web")
