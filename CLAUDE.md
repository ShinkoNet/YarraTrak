# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

### Pure Tool-Call Agent

The agent uses `tool_choice="required"` to ensure all responses come via tool calls - no JSON parsing needed. The worker model must always call one of:

**Data Tools** (fetch information, loop continues):
- `search_and_get_departures` - Combined search + fetch
- `search_stops` / `search_routes` - Discovery
- `get_departures` / `get_route_directions` - Direct lookups

**Terminal Tools** (end the loop, return to user):
- `return_result` - Structured departure data with TTS text
- `ask_clarification` - Prompt user to choose (direction, line, etc.)
- `return_error` - Error message

### Dual-Model Speculative Race

Guardrail and worker run concurrently. Guardrail can cancel the worker if the query is out-of-scope. Both use session context for multi-turn conversations.

### Backend Files

| File | Purpose |
|------|---------|
| `server/api.py` | FastAPI endpoints, TTS, vibration encoding |
| `server/agent_engine.py` | Guardrail + worker, tool definitions, session handling |
| `server/tools.py` | PTV API tool implementations |
| `server/ptv_client.py` | PTV API client with HMAC-SHA1 signing |
| `server/session_store.py` | In-memory conversation history (4-turn, 120s TTL) |
| `server/mcp_server.py` | MCP protocol wrapper for Claude Desktop |
| `server/enums.py` | RouteType enum (TRAIN=0, TRAM=1, BUS=2, VLINE=3, NIGHT_BUS=4) |

### Frontend

`web/app.js` - Vanilla JS SPA with stealth buttons, voice I/O, clarification chips.

## Environment Variables

Required in `.env`:
- `PTV_DEV_ID` / `PTV_API_KEY` - PTV API credentials
- `GROQ_API_KEY` - Groq API for LLM and Whisper
- `AZURE_SPEECH_KEY` / `AZURE_SPEECH_REGION` - Azure TTS (optional)

## Key Behaviors

### Direction Ambiguity
- "Next train from Richmond" → `ask_clarification` (which line?)
- "Next Pakenham train from Richmond" → `return_result` (line specified)
- "Next train to the city from Richmond" → `return_result` (direction clear)

### ASR Corrections
Session history helps resolve misrecognized speech. "Packingham" can be corrected to "Pakenham" through clarification.

### Vibration Encoding
Minutes encoded as haptic patterns: hours (1000ms), tens (500ms), ones (150ms).
