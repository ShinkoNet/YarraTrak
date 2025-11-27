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
pytest tests/ -v
```

### Run the MCP Server (for Claude Desktop integration)
```bash
python -m server.mcp_server
```

### Install Dependencies
```bash
pip install -r server/requirements.txt
```

## Architecture

### System Flow

```
User Voice/Text → FastAPI → Agent Engine → Tools → PTV API
                     ↓
              Session Store (context)
                     ↓
              Groq LLM (worker)
                     ↓
              Tool Calls → Terminal Tool → Response
```

### Pure Tool-Call Agent

The agent uses `tool_choice="required"` to ensure all responses come via tool calls - no JSON parsing needed. An assistant prefill (`"I'll call the appropriate tool now.\n"`) nudges the model toward tool use.

**Data Tools** (fetch information, loop continues):
- `search_and_get_departures` - Combined search + fetch with smart ranking
- `search_stops` / `search_routes` - Discovery
- `get_departures` / `get_route_directions` - Direct lookups

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
| `server/api.py` | FastAPI endpoints (`/query`, `/transcribe`, `/tts`), vibration encoding |
| `server/agent_engine.py` | Guardrail + worker, tool definitions, session handling, prefill logic |
| `server/tools.py` | PTV API tool implementations, query sanitization, disruption filtering |
| `server/ptv_client.py` | PTV API client with HMAC-SHA1 signing |
| `server/session_store.py` | In-memory conversation history (4-turn, 120s TTL) |
| `server/mcp_server.py` | MCP protocol wrapper for Claude Desktop |
| `server/config.py` | Environment variables and feature flags |
| `server/enums.py` | RouteType enum (TRAIN=0, TRAM=1, BUS=2, VLINE=3, NIGHT_BUS=4) |

### Frontend

`web/app.js` - Vanilla JS SPA with stealth buttons, voice I/O, clarification chips.

## Environment Variables

Required in `.env`:
- `PTV_DEV_ID` / `PTV_API_KEY` - PTV API credentials
- `GROQ_API_KEY` - Groq API for LLM and Whisper
- `AZURE_SPEECH_KEY` / `AZURE_SPEECH_REGION` - Azure TTS (optional)

In `server/config.py`:
- `ENABLE_GUARDRAIL` - Set to `False` to disable the guardrail agent (currently False)

## Key Implementation Details

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

## Key Behaviors

### Direction Ambiguity
- "Next train from Richmond" → `ask_clarification` (which line?)
- "Next Pakenham train from Richmond" → `return_result` (line specified)
- "Next train to the city from Richmond" → `return_result` (direction clear)

### ASR Recovery
The prompt instructs the model to ask clarification before erroring on no results:
- "Sandringam" not found → ask "Did you mean Sandringham?"
- Only `return_error` after clarification fails

### Vibration Encoding
Minutes encoded as haptic patterns:
- Hours: 1000ms pulse
- Tens: 500ms pulse
- Ones: 150ms pulse

## Testing

Tests hit the real Groq API (no mocking). Key test files:
- `tests/test_conversations.py` - Agent scenarios, multi-turn, tool selection
- `tests/conftest.py` - Loads `.env` for tests

Guardrail tests skip when `ENABLE_GUARDRAIL = False`.

## Models Used

- **Worker**: `moonshotai/kimi-k2-instruct` (via Groq)
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
Each tool call is logged: `[Turn N] Tool: name({args})`

Check for:
- Repeated searches (model struggling)
- Max turns reached (10)
- Schema validation errors from Groq
