from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .enums import RouteType
import os
import asyncio
import azure.cognitiveservices.speech as speechsdk
import uuid
import base64
import html

app = FastAPI()

# Allow CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class StealthRequest(BaseModel):
    button_id: int
    stop_id: int
    stop_name: str | None = None
    direction_id: int | None = None
    direction_name: str | None = None
    route_type: int = RouteType.TRAIN

class AgentRequest(BaseModel):
    query: str

from datetime import datetime, timezone
from .ptv_client import PTVClient

# Initialize Client
ptv_client = PTVClient()

def calculate_vibration(minutes: int):
    """
    Convert minutes to vibration pattern.
    Hours = Very Long Buzz (1000ms)
    Tens = Long Buzz (500ms)
    Ones = Short Buzz (150ms)
    """
    # Special case for 0 minutes (Arriving Now)
    if minutes == 0:
        return [
            80, 120,   # "a" (The new pickup note)
            150, 250,  # "Shave"
            80, 120,   # "and"
            80, 120,   # "a"
            150, 250,  # "hair"
            150, 650,  # "cut" ... (shorter pause to match faster tempo)
            150, 250,  # "two"
            300        # "bits!"
        ]
        
    hours = minutes // 60
    remaining_minutes = minutes % 60
    tens = remaining_minutes // 10
    ones = remaining_minutes % 10
    
    pattern = []
    
    # Hours (Very Long buzzes)
    for _ in range(hours):
        pattern.append(1000) # Vibrate
        pattern.append(400)  # Pause

    # Pause between hours and tens/ones if needed
    if hours > 0 and (tens > 0 or ones > 0):
        if pattern:
            pattern[-1] += 200

    # Tens (Long buzzes)
    for _ in range(tens):
        pattern.append(500) # Vibrate
        pattern.append(300) # Pause
        
    # Pause between tens and ones if both exist
    if tens > 0 and ones > 0:
        if pattern:
            pattern[-1] += 100
        
    # Ones (Short buzzes)
    for _ in range(ones):
        pattern.append(150) # Vibrate
        pattern.append(150) # Pause
        
    return pattern

@app.post("/api/v1/stealth")
async def stealth_mode(req: StealthRequest):
    """
    Handle 'Stealth Mode' button presses.
    Returns a vibration pattern based on real PTV data.
    """
    print(f"Stealth Mode: Button {req.button_id} pressed. Config: {req.stop_name} -> {req.direction_name}")
    
    if not req.stop_id:
        return {"vibration": [100, 100], "message": "Button not configured"}

    try:
        data = await ptv_client.get_departures(req.route_type, req.stop_id, max_results=10, expand=["Direction"])
        departures = data.get("departures", [])
        
        if not departures:
            return {"vibration": [200, 200], "message": "No trains"}
            
        # Find the first departure in the future
        now_utc = datetime.now(timezone.utc)
        next_dep = None
        
        for d in departures:
            # Filter by direction if specified
            if req.direction_id is not None:
                if d.get('direction_id') != req.direction_id:
                    continue

            # Use estimated if available, else scheduled
            dep_str = d.get('estimated_departure_utc') or d.get('scheduled_departure_utc')
            if not dep_str:
                continue
                
            # Parse ISO8601
            dep_str = dep_str.replace('Z', '+00:00')
            dep_time = datetime.fromisoformat(dep_str)
            
            if dep_time > now_utc:
                next_dep = dep_time
                break
        
        if next_dep:
            delta = next_dep - now_utc
            minutes = int(delta.total_seconds() / 60)
            
            # Cap at 12 hours (720 mins) for sanity
            if minutes > 720: minutes = 720
            if minutes < 0: minutes = 0
            pattern = calculate_vibration(minutes)
            # Determine vehicle type label
            vehicle_types = {
                RouteType.TRAIN: "train",
                RouteType.TRAM: "tram",
                RouteType.BUS: "bus",
                RouteType.VLINE: "train",
                RouteType.NIGHT_BUS: "bus"
            }
            v_type = vehicle_types.get(req.route_type, "vehicle")

            if minutes == 0:
                msg = "Arriving Now"
            else:
                msg = f"Next {v_type} in {minutes} min"
            
            print(f"{msg} ({next_dep})")
            return {"vibration": pattern, "message": msg}
        else:
            return {"vibration": [200, 200, 200], "message": "No future services"}

    except Exception as e:
        print(f"Error in stealth_mode: {e}")
        return {"vibration": [500, 100, 500], "message": "Error fetching data"}

from groq import AsyncGroq
from . import tools
from . import config
import json

# Initialize Groq Client
if config.GROQ_API_KEY:
    groq_client = AsyncGroq(api_key=config.GROQ_API_KEY)
else:
    groq_client = None
    print("WARNING: GROQ_API_KEY not set. Agent mode will fail.")

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

# Global Chat History (Simple In-Memory Context for MVP)
chat_history = []

# Load Train Station Names
STATION_NAMES = []
try:
    json_path = os.path.join(os.path.dirname(__file__), "train_station_names.json")
    with open(json_path, "r") as f:
        data = json.load(f)
        STATION_NAMES = data.get("train_station_names", [])
    print(f"Loaded {len(STATION_NAMES)} train station names for LLM context.")
except Exception as e:
    print(f"WARNING: Failed to load train_station_names.json: {e}")

# Tool Definitions (OpenAI Format)
tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "get_departures",
            "description": "Get next departures for a specific stop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "stop_id": {
                        "type": "integer",
                        "description": "The ID of the stop (e.g., 1071 for Flinders St)."
                    },
                    "route_type": {
                        "type": "string",
                        "enum": ["TRAIN", "TRAM", "BUS", "VLINE", "NIGHT_BUS"],
                        "description": "Transport mode. Defaults to TRAIN.",
                        "default": "TRAIN"
                    }
                },
                "required": ["stop_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_stops",
            "description": "Search for public transport stops by name. Filters for Trains (Metro/VLine) and Trams.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The name of the stop to search for (e.g., 'Flinders')."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_routes",
            "description": "Search for public transport routes by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The name of the route to search for (e.g., 'Belgrave')."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_route_directions",
            "description": "Get directions for a specific route.",
            "parameters": {
                "type": "object",
                "properties": {
                    "route_id": {
                        "type": "integer",
                        "description": "The ID of the route."
                    }
                },
                "required": ["route_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_and_get_departures",
            "description": "Search for a stop and immediately get departures for the first result. Useful when the user specifies a clear stop name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The name of the stop to search for (e.g., 'Flinders')."
                    },
                    "route_type": {
                        "type": "string",
                        "enum": ["TRAIN", "TRAM", "BUS", "VLINE", "NIGHT_BUS"],
                        "description": "Transport mode. Defaults to TRAIN.",
                        "default": "TRAIN"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "configure_button",
            "description": "Configure one of the 3 physical buttons with a station and direction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "button_index": {"type": "integer", "description": "Button number (1, 2, or 3)"},
                    "stop_id": {"type": "integer", "description": "The PTV stop ID"},
                    "stop_name": {"type": "string", "description": "Name of the stop"},
                    "direction_id": {"type": "integer", "description": "The direction ID (optional, if specific direction needed)"},
                    "direction_name": {"type": "string", "description": "Name of the direction (e.g. 'City')"},
                    "route_type": {
                        "type": "string",
                        "enum": ["TRAIN", "TRAM", "BUS", "VLINE", "NIGHT_BUS"],
                        "description": "Transport mode. Default TRAIN.",
                        "default": "TRAIN"
                    }
                },
                "required": ["button_index", "stop_id", "stop_name"]
            }
        }
    }
]

# JSON Schema for final response (used when tools are done)
response_schema = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "Natural language response to the user"
        },
        "departure_time_utc": {
            "type": "string",
            "description": "ISO 8601 UTC timestamp of the primary departure (optional)"
        }
    },
    "required": ["text"],
    "additionalProperties": False
}

@app.post("/api/v1/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    """
    Transcribe uploaded audio file using Groq Whisper API.
    """
    if not groq_client:
        raise HTTPException(status_code=500, detail="Groq API Key not configured")

    try:
        print(f"Transcribing file: {file.filename}")
        content = await file.read()
        
        # Create transcription
        transcription = await groq_client.audio.transcriptions.create(
            file=(file.filename, content),
            model="whisper-large-v3",
            response_format="json",
            language="en",
            temperature=0.0
        )
        
        print(f"Transcription result: {transcription.text}")
        return {"text": transcription.text}
    except Exception as e:
        print(f"Transcription error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/agent")
async def agent_query(request: AgentRequest):
    global chat_history
    query = request.query
    print(f"Agent received: {query}")
    
    if not groq_client:
        return {"text": "Error: Groq API Key not configured."}

    # Initialize history if empty
    if not chat_history:
        chat_history = [
            {
                "role": "system",
                "content": """You are a helpful public transport assistant for Melbourne, Australia.
Your goal is to provide quick, actionable information using the available tools.

If the user asks to "set button X" or "configure button X", use the `configure_button` tool.
You must find the Stop ID and Direction ID first using `search_stops` and `get_departures`.

It's possible that due to the faults of speech-to-text's weaknesses with place names, a user might query a station name that does not exist.

**STATION NAME CORRECTION:**
I have provided you with a list of valid Melbourne Train Station names below.
1. If the user's query sounds like one of these stations (e.g., "Flinders Treat" -> "Flinders Street", "Darry Warren" -> "Narre Warren"), USE THE CORRECT NAME from the list when calling `search_stops`.
2. **EXCEPTION**: If the user specifically asks for a TRAM stop, BUS stop, or a location not on this list (e.g., "St Kilda Beach", "Chadstone"), DO NOT force it to match a train station. Use their original query.

**VALID TRAIN STATIONS:**
{", ".join(STATION_NAMES)}

CRITICAL RULE - ONE DIRECTION ONLY:
You must NEVER provide information about multiple train directions in a single response.
If you see multiple directions, you MUST stop and ask which one.

GUIDELINES:
1. **Be Decisive**: Do not ask for clarification if a reasonable default exists.
2. **Prefer Metro Trains**: If a stop has both Metro Train (route_type 0) and V/Line (route_type 3) services, ALWAYS prefer Metro Train unless the user specifically asks for V/Line or "regional".
3. **Search Results**: If a user searches for a stop, call `search_stops`. If results are found, choose from those matches immediately with their IDs. Do NOT ask "which one?". You can reconfirm the name of something with a user ONLY IF there are no results.
4. **Departures & Directions - STRICT SINGLE DIRECTION POLICY**:
   - Call `get_departures` to see the "Direction Name".
   - **"To City"**: If user says "to city", "inbound", or "up the line", filter for directions containing "City" or "Flinders". Provide ONE departure time.
   - **"Down the line"**: If user says "down the line", "outbound", "back", or "away", look for non-City directions.
   - **STOP RULE**: If you see multiple outbound directions, STOP immediately and ask: "Which line - [Direction X] or [Direction Y]?"
   - **STOP RULE**: If the user did not specify inbound or outbound, STOP and ask: "Inbound to the city, or outbound?"
   
5. **What NOT to do** (FORBIDDEN):
   ❌ "There's X train at 10:04pm and Y train at 10:06pm"
   ❌ Mentioning multiple directions in any form
   
6. **What TO do** (CORRECT):
   ✅ "I see trains to both X and Y. Which line do you need?"
   ✅ "There are outbound and inbound trains. Which direction?"
   
7. **Formatting**: Keep responses concise. Use 12-hour time (e.g., 5:30pm).
8. **Use Tools**: You can call multiple tools in sequence to gather information before responding.
"""
            }
        ]

    # Append user message
    chat_history.append({"role": "user", "content": query})

    try:
        max_tool_iterations = 5
        pending_config = None
        
        for iteration in range(max_tool_iterations):
            response = await groq_client.chat.completions.create(
                model="moonshotai/kimi-k2-instruct",
                messages=chat_history,
                tools=tools_schema,
                tool_choice="auto",
                temperature=0.7
            )
            
            response_message = response.choices[0].message
            tool_calls = response_message.tool_calls
            
            # If no tool calls, model is ready to respond
            if not tool_calls:
                # Store the assistant's message (without tools)
                if response_message.content:
                    chat_history.append({
                        "role": "assistant",
                        "content": response_message.content
                    })
                break
            
            # Model wants to use tools - append its message
            chat_history.append(response_message)
            
            # Execute each tool call
            for tool_call in tool_calls:
                function_name = tool_call.function.name
                function_args = json.loads(tool_call.function.arguments)
                
                print(f"DEBUG: Calling {function_name} with {function_args}")
                
                # Convert route_type string to integer if present
                if "route_type" in function_args and isinstance(function_args["route_type"], str):
                    try:
                        # Convert "TRAIN" -> RouteType.TRAIN (which is 0)
                        # We assume the LLM sends the exact enum name
                        r_type_str = function_args["route_type"].upper()
                        if r_type_str in RouteType.__members__:
                            function_args["route_type"] = RouteType[r_type_str].value
                            print(f"DEBUG: Converted route_type '{r_type_str}' to {function_args['route_type']}")
                    except Exception as e:
                        print(f"DEBUG: Failed to convert route_type: {e}")

                # Execute the tool
                try:
                    if function_name == "get_departures":
                        tool_result = await tools.get_departures(**function_args)
                    elif function_name == "search_stops":
                        tool_result = await tools.search_stops(**function_args)
                    elif function_name == "search_routes":
                        tool_result = await tools.search_routes(**function_args)
                    elif function_name == "get_route_directions":
                        tool_result = await tools.get_route_directions(**function_args)
                    elif function_name == "search_and_get_departures":
                        tool_result = await tools.search_and_get_departures(**function_args)
                    elif function_name == "configure_button":
                        pending_config = function_args
                        tool_result = json.dumps({"status": "success", "message": "Button configuration received. Tell the user it has been updated."})
                    else:
                        tool_result = json.dumps({"error": "Unknown tool", "is_error": True})
                    
                    print(f"DEBUG: Tool result: {tool_result[:200]}...")
                    
                except Exception as e:
                    # Return error in tool response (Groq best practice)
                    tool_result = json.dumps({
                        "error": str(e),
                        "is_error": True
                    })
                    print(f"DEBUG: Tool error: {e}")
                
                # Append tool result to history
                chat_history.append({
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "name": function_name,
                    "content": str(tool_result)
                })
        
        print("DEBUG: Requesting final JSON response")
        
        # Add instruction for JSON output
        chat_history.append({
            "role": "user",
            "content": "Please provide your final response as a JSON object with 'text' (your answer) and optionally 'departure_time_utc' (ISO 8601 format) if you mentioned a specific departure time."
        })
        
        # Request with JSON mode (using prefill technique)
        chat_history.append({
            "role": "assistant",
            "content": "```json\n"
        })
        
        final_response = await groq_client.chat.completions.create(
            model="moonshotai/kimi-k2-instruct",
            messages=chat_history,
            temperature=0.3,  # Lower temp for structured output
            stop=["```"]  # Stop at closing fence
        )
        
        # Extract and parse JSON
        json_content = final_response.choices[0].message.content
        
        # Remove the prefill message from history (keep it clean)
        chat_history.pop()
        
        # Parse the JSON
        try:
            llm_data = json.loads(json_content.strip())
        except json.JSONDecodeError as e:
            print(f"DEBUG: JSON parse failed: {e}")
            print(f"DEBUG: Raw content: {json_content}")
            # Fallback to text-only response
            llm_data = {"text": json_content.strip()}
        
        # Store the complete assistant response in history
        chat_history.append({
            "role": "assistant",
            "content": f"```json\n{json.dumps(llm_data)}\n```"
        })
        
        vibration = []
        dep_time_str = llm_data.get("departure_time_utc")
        
        if dep_time_str:
            try:
                dep_time_str = dep_time_str.replace('Z', '+00:00')
                dep_time = datetime.fromisoformat(dep_time_str)
                now_utc = datetime.now(timezone.utc)
                delta = dep_time - now_utc
                minutes = int(delta.total_seconds() / 60)
                
                # Cap at 99 for vibration sanity
                minutes = max(0, min(99, minutes))
                
                vibration = calculate_vibration(minutes)
                print(f"DEBUG: Vibration pattern: {minutes} min -> {vibration}")
            except Exception as ve:
                print(f"DEBUG: Vibration calc error: {ve}")
        
        # Generate Audio
        audio_base64 = None
        text_response = llm_data.get("text", "")
        
        if speech_config and text_response:
            try:
                # Use PullAudioOutputStream to get audio data in memory
                pull_stream = speechsdk.audio.PullAudioOutputStream()
                audio_config = speechsdk.audio.AudioOutputConfig(stream=pull_stream)
                synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
                
                print(f"Synthesizing audio...")
                
                # Escape text for XML
                safe_text = html.escape(text_response)
                
                # Construct SSML for pitch adjustment
                ssml = f"""
                <speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
                    <voice name="{speech_config.speech_synthesis_voice_name}">
                        <prosody pitch="+30%">
                            {safe_text}
                        </prosody>
                    </voice>
                </speak>
                """
                
                result = synthesizer.speak_ssml_async(ssml).get()
                
                if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                    print("Speech synthesized successfully")
                    
                    # Read audio data from the stream
                    audio_data = result.audio_data
                    audio_base64 = base64.b64encode(audio_data).decode('utf-8')
                    
                elif result.reason == speechsdk.ResultReason.Canceled:
                    cancellation_details = result.cancellation_details
                    print(f"Speech synthesis canceled: {cancellation_details.reason}")
                    if cancellation_details.reason == speechsdk.CancellationReason.Error:
                        print(f"Error details: {cancellation_details.error_details}")
            except Exception as e:
                print(f"TTS Error: {e}")

        return {
            "text": text_response,
            "vibration": vibration,
            "config_update": pending_config,
            "audio_base64": audio_base64
        }

    except Exception as e:
        print(f"ERROR: Agent failed: {e}")
        import traceback
        traceback.print_exc()
        chat_history = []  # Reset on error
        return {"text": f"Error: {str(e)}"}

@app.post("/api/v1/reset")
async def reset_chat():
    global chat_history
    chat_history = []
    print("Chat session reset.")
    return {"message": "Conversation reset"}

# Mount Web App
web_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
if os.path.exists(web_path):
    app.mount("/", StaticFiles(directory=web_path, html=True), name="web")
else:
    print(f"WARNING: Web directory not found at {web_path}")

