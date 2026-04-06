# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

PTV Notify is a voice-enabled public transport assistant for Melbourne, Australia. It provides real-time departure information for trains, trams, and buses using the PTV (Public Transport Victoria) API. The app features an LLM-powered conversational agent with speech-to-text (Groq Whisper) and text-to-speech (Azure Speech) capabilities.

## Commands

### Run the Server
```bash
python -m uvicorn server.api:app --host 0.0.0.0 --port 8000 --reload
```

### Run Tests
```bash
pytest tools/ -v
```

### Run the MCP Server (for Claude Desktop integration)
```bash
python -m server.mcp_server
```

### Install Dependencies
```bash
pip install -r server/requirements.txt
```

### Deploy server code to Production
```bash
#git commit and push first!
ssh root@web01.waifu.trash "cd /srv/http/ptv-notify && git pull && systemctl restart netcavy-ptv"
```

### Deploy app to watch and read code
```bash
pebble build && pebble install --phone=10.1.0.244 --logs
```

## Architecture

### System Flow

```
User Voice/Text → FastAPI → Agent Engine → Tools → PTV API
                     │
    ┌────────────────┼────────────────┐
    │                │                │
    ▼                ▼                ▼
Session Store   Speculative      Groq LLM
(conversation)  Fetch (parallel)  (worker)
                     │                │
                     └───► Pre-fetched data injected into prompt
                                      │
                                      ▼
                          Tool Calls → Terminal Tool → Response
```

### Speculative Execution

On repeat queries to previously searched stops, the server pre-fetches departure data in parallel with ASR. This reduces agent latency by ~55% on warm requests:

- **Cold request**: 2000-2500ms (LLM calls `search_and_get_departures` → PTV API)
- **Warm request**: 900-1000ms (LLM uses pre-fetched data → direct `return_result`)

**How it works:**

1. Client stores successful stop queries in `localStorage` (`query_history`)
2. Client sends `query_history` with each `/voice` or `/query` request
3. Server pre-fetches departures for those stops in parallel with ASR
4. Pre-fetched data is injected into the LLM prompt
5. LLM can skip the `search_and_get_departures` call and return directly
6. Server returns `learned_stop` for client to store

### Pure Tool-Call Agent

The agent uses `tool_choice="required"` to ensure all responses come via tool calls - no JSON parsing needed. An assistant prefill (`"I'll call the appropriate tool now.\n"`) nudges the model toward tool use.

**Data Tools** (fetch information, loop continues):
- `search_and_get_departures` - Combined search + fetch with smart ranking
- `search_stops` / `search_routes` - Discovery
- `get_departures` / `get_route_directions` - Direct lookups
- `configure_pebble_button` - Generate button config for user (Favourites)

**Terminal Tools** (end the loop, return to user):
- `return_result` - Structured departure data with TTS text
- `ask_clarification` - Prompt user to choose (direction, line, etc.)
- `return_error` - Error message

### Worker Loop (agent_engine.py)

```python
for turn in range(10):  # Max 10 turns
    # Prefill nudges model toward tool use
    messages_with_prefill = messages + [{"role": "assistant", "content": "I'll call the appropriate tool now.\n"}]

    response = await groq_client.chat.completions.create(
        model=WORKER_MODEL,
        messages=messages_with_prefill,
        tools=WORKER_TOOLS,
        tool_choice="required",
    )

    # Execute tool, check if terminal
    if fn_name in TERMINAL_TOOL_NAMES:
        return result  # End loop
    # Otherwise, add tool result to messages and continue
```

### Guardrail (Optional)

When `ENABLE_GUARDRAIL = True`, a guardrail model runs in parallel with the worker. It classifies queries as `allow` or `block`. If blocked, the worker is cancelled. Currently disabled for development.

## Backend Files

| File | Purpose |
|------|---------|
| `server/api.py` | FastAPI endpoints (`/voice`, `/query`, `/favourite`), vibration encoding, speculative fetch |
| `server/agent_engine.py` | Guardrail + worker, tool definitions, session handling, prefill logic |
| `server/tools.py` | PTV API tool implementations, query sanitization, speculative fetch, disruption filtering |
| `server/ptv_client.py` | PTV API client with HMAC-SHA1 signing |
| `server/session_store.py` | In-memory conversation history (4-turn, 120s TTL) |
| `server/mcp_server.py` | MCP protocol wrapper for Claude Desktop |
| `server/config.py` | Environment variables and feature flags |
| `server/enums.py` | RouteType enum (TRAIN=0, TRAM=1, BUS=2, VLINE=3, NIGHT_BUS=4) |

### API Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/voice` | Voice input: ASR + speculative fetch (parallel) + Agent. Accepts `file`, `session_id`, `query_history` |
| `POST /api/v1/query` | Text input: speculative fetch + Agent. Accepts JSON `{query, session_id, query_history}` |
| `POST /api/v1/favourite` | Direct PTV lookup for pre-configured buttons (no LLM). Used by Web Simulator. |
| `GET /api/v1/media/{ticket}` | Retrieve TTS audio by ticket ID |
| `GET /pebble-config.html` | Serves the Pebble configuration page (single source of truth for settings) |

### Frontend

`web/app.js` - Vanilla JS SPA with favourite buttons, voice I/O, clarification chips.

**localStorage keys:**
- `ptv_session_id` - Persistent session ID (survives page refresh)
- `ptv_query_history` - Array of learned stops for speculative fetch (max 10)
- `ptv_btn_1/2/3` - Favourite button configurations

## Environment Variables

Required in `.env`:
- `PTV_DEV_ID` / `PTV_API_KEY` - PTV API credentials
- `GROQ_API_KEY` - Groq API for LLM and Whisper
- `AZURE_SPEECH_KEY` / `AZURE_SPEECH_REGION` - Azure TTS (optional)

In `server/config.py`:
- `ENABLE_GUARDRAIL` - Set to `False` to disable the guardrail agent (currently False)

## Key Implementation Details

### Speculative Fetch (tools.py, api.py)

```python
# tools.py
async def speculative_fetch(query_history: list[dict], max_stops: int = 3) -> list[dict]:
    """Pre-fetch departures for stops from client-provided history."""
    # Fetches in parallel, returns formatted departure data

# api.py - /voice endpoint
async def run_speculative():
    prefetched = await tools.speculative_fetch(history_list)
    return tools.format_speculative_context(prefetched)

# ASR and speculative fetch run in parallel
transcript, prefetched_context = await asyncio.gather(run_asr(), run_speculative())
```

The `learned_stop` is extracted from tool output via `[STOP_INFO:stop_id:route_type:stop_name]` marker and returned to client.

### Smart Stop Ranking (tools.py)

`search_and_get_departures` ranks stops by match quality:
1. Exact match: "Richmond" → "Richmond Station" (rank 0)
2. Starts with: "Rich" → "Richmond Station" (rank 1)
3. Contains as word (rank 2)
4. Starts with anywhere (rank 3)
5. Contains anywhere (rank 4)

If a search is repeated in the same session, returns multiple options instead of auto-selecting.

### Query Sanitization (tools.py)

```python
def sanitize_query(query: str) -> str:
    # "St. Kilda" → "St Kilda"
    # "Prince's Bridge" → "Princes Bridge"
    # Collapses multiple spaces
```

### Disruption Filtering (tools.py)

Only shows disruptions that:
1. Affect routes shown in departures
2. Have status "Current"
3. Contain keywords: "terminate", "replace", or "delay"

Limited to 3 disruptions max.

### Type Coercion (agent_engine.py)

`normalize_result_payload()` handles model quirks:
- Platform: int → str (model sometimes passes `6` instead of `"6"`)
- minutes_to_depart: ensures int

### Error Handling

Tool errors return tagged messages:
- `[API_ERROR]` - PTV API failures (HTTP errors, connection issues)
- `[MCP_ERROR]` - Tool processing exceptions

Agent handles:
- `APITimeoutError` → `GROQ_TIMEOUT`
- `APIConnectionError` → `GROQ_CONNECTION`
- `BadRequestError` with `tool_use_failed` → Retries up to 3 times with nudge toward `ask_clarification`

### WebSocket Protocol (pebble <-> server)

**Connection URL:**
```
wss://server/ws?buttons=1:STOP_ID:ROUTE_TYPE:DIR_ID,2:STOP_ID:...
```
The `buttons` param enables instant departure data push on connection (no waiting for subscribe_favourites message).

**Client -> Server:**
- `query`: Standard agent query `{type: "query", text, session_id, llm_api_key}`
- `favourite`: Direct departure check `{type: "favourite", stop_id: 123, ...}` - returns vibration + platform
- `subscribe_favourites`: Subscribe to live updates `{type: "subscribe_favourites", buttons: [{button_id, stop_id, route_type, direction_id}, ...]}`

**Server -> Client:**
- `result`: Agent response
- `favourite_result`: Vibration pattern + message + platform `{type: "favourite_result", minutes: 5, platform: "2", ...}`
- `favourite_update`: Live broadcast (every 15s) `{type: "favourite_update", updates: [{button_id, minutes, platform, message}, ...]}`
- `favourites_subscribed`: Confirmation of subscription `{type: "favourites_subscribed", buttons: 3}`

### Favourite Cache & Broadcasts

The server keeps an in-memory cache of departure lookups and a per-connection favourite subscription list:

1. **First connect** (cold): Pebble sends buttons in URL → server fetches PTV API (~1-2s per button)
2. **Background loop** (every 15s): Refreshes connected clients' favourite subscriptions
3. **Subsequent connects**: Cache hit → faster initial response for the same stop/direction combo

There is no longer a global shared button registry between clients.

## Key Behaviors

### Direction Ambiguity
- "Next train from Richmond" → `ask_clarification` (which line?)
- "Next Pakenham train from Richmond" → `return_result` (line specified)
- "Next train to the city from Richmond" → `return_result` (direction clear)

### ASR Recovery
The prompt instructs the model to ask clarification before erroring on no results:
- "Sandringam" not found → ask "Did you mean Sandringham?"
- Only `return_error` after clarification fails

### Vibration Encoding (Client-Side)
Minutes encoded as haptic patterns by clients (Pebble and web app):
- Hours: 1000ms pulse
- Tens: 500ms pulse
- Ones: 150ms pulse
- NOW! (0 min): "Shave and a haircut" special rhythm

Clients calculate vibration locally from `departure_time` or `minutes_to_depart`.

### Station Watching Mode

When a favourite button is pressed, the Pebble enters **watching mode**:

- **Custom Window**: Uses `Window` with `Text` + `Rect` elements (not `Card`)
- **Big Timer**: LECO 42pt monospace font; switches to BITHAM for "NOW!"
- **Progress Bar**: White bar at bottom shrinks as seconds count down, hidden for NOW!
- **Hours Format**: Shows `H:MM:SS` for 60+ minute waits
- **Vibration**: Buzzes on each minute boundary and train transitions
- **Persistent**: Panel stays open until back button pressed

Key functions in `pebble/src/js/app.js`:
- `runFavouriteQuery()` - Opens watching window
- `updateWatchingDisplay()` - Updates timer, platform, route, progress bar
- `stopWatching()` - Cleans up and hides window

## Testing

Tests hit the real Groq API (no mocking). Key test files:
- `tools/test_conversations.py` - Agent scenarios, multi-turn, tool selection
- `tools/conftest.py` - Loads `.env` for tests
- `tools/test_latency.py` - End-to-end latency testing for voice pipeline and speculative execution

Guardrail tests skip when `ENABLE_GUARDRAIL = False`.

### Latency Testing

```bash
python test_latency.py
```

Tests the full voice pipeline with two requests:
1. Cold request (no history) - measures baseline latency
2. Warm request (with query_history) - measures speculative fetch benefit

## Models Used

- **Worker**: `moonshotai/kimi-k2-instruct-0905` (via Groq)
- **Guardrail**: `openai/gpt-oss-safeguard-20b` (via Groq) - when enabled
- **Whisper**: Groq Whisper API for STT

## Common Maintenance Tasks

### Adding a New Tool
1. Add tool definition to `DATA_TOOLS` or `TERMINAL_TOOLS` in `agent_engine.py`
2. Implement handler in `tools.py`
3. Add to `TOOL_HANDLERS` dict in `agent_engine.py`
4. If terminal, add name to `TERMINAL_TOOL_NAMES`

### Changing the LLM Model
Update `WORKER_MODEL` in `agent_engine.py`. Test with various queries as different models behave differently with tool calls.

### Adjusting Prompt Behavior
Edit `WORKER_PROMPT` in `agent_engine.py`. Key sections:
- RULES: Core constraints
- DIRECTION LOGIC: When to clarify vs return
- NO RESULTS / ASR RECOVERY: Error handling guidance
- ERROR HANDLING: How to report API/MCP errors

### Debugging Tool Calls
Each tool call is logged with its name and argument keys, without dumping the full user prompt or tool payloads.

Check for:
- Repeated searches (model struggling)
- Max turns reached (10)
- Schema validation errors from Groq
