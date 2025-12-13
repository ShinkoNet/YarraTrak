# PTV Notify

A voice-enabled public transport assistant for Melbourne, Australia. Get real-time train, tram, and bus departure information through natural conversation.

## Features

- **Voice Interface** - Speech-to-text (Groq Whisper) and text-to-speech (Azure Speech)
- **Real-time Departures** - Live data from PTV (Public Transport Victoria) API
- **Smart Search** - Fuzzy matching handles typos and ASR errors ("Sandringam" → "Sandringham")
- **Multi-turn Conversations** - Context-aware follow-ups and clarifications
- **Speculative Execution** - Pre-fetches departures for frequently queried stops, reducing latency by ~55%
- **Disruption Alerts** - Bus replacements, delays, and service changes
- **Haptic Feedback** - Vibration patterns encode departure times for discreet use
- **MCP Integration** - Use with Claude Desktop via Model Context Protocol

## Quick Start

### 1. Install Dependencies

```bash
pip install -r server/requirements.txt
```

### 2. Configure Environment

Create a `.env` file:

```env
PTV_DEV_ID=your_ptv_dev_id
PTV_API_KEY=your_ptv_api_key
GROQ_API_KEY=your_groq_api_key
AZURE_SPEECH_KEY=your_azure_key        # Optional - for TTS
AZURE_SPEECH_REGION=australiaeast      # Optional - for TTS
```

Get PTV API credentials at: https://www.ptv.vic.gov.au/footer/data-and-reporting/datasets/ptv-timetable-api/

### 3. Run the Server

```bash
python -m uvicorn server.api:app --host 0.0.0.0 --port 8000 --reload
```

Open http://localhost:8000 in your browser.

### 4. Run the Pebble App

```bash
./run_pebble.sh
```

### 5. Run Tests

```bash
pytest tests/ -v
```

## Usage Examples

**Simple queries:**
- "Next train from Flinders Street"
- "When's the next 96 tram?"
- "Trains to the city from Richmond"

**Specific queries:**
- "Next Pakenham train from Caulfield"
- "Frankston line from South Yarra"

**Follow-ups:**
- User: "Next train from Richmond"
- Assistant: "Which line - Pakenham, Frankston, or Glen Waverley?"
- User: "Pakenham"

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Frontend                              │
│  web/app.js - Voice I/O, clarification chips, haptic feedback, for testing.
│ pebble/src/js/app.js - The pebble app's frontend             |
│  localStorage: session_id, query_history, button configs     │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                     FastAPI Server                           │
│  server/api.py - /voice, /query, /stealth endpoints         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Agent Engine                              │
│  server/agent_engine.py                                      │
│  ┌─────────────┐    ┌──────────────────────────────────┐    │
│  │  Guardrail  │    │            Worker                 │    │
│  │  (optional) │    │  Tool loop with prefill nudging   │    │
│  │             │    │  Max 10 turns, tool_choice=required│   │
│  └─────────────┘    └──────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      Tools Layer                             │
│  server/tools.py                                             │
│  - search_and_get_departures (smart ranking, repeat detect) │
│  - search_stops / search_routes                              │
│  - get_departures (with disruption info)                    │
│  - get_route_directions                                      │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                     PTV Client                               │
│  server/ptv_client.py - HMAC-SHA1 signed requests           │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    [ PTV Timetable API ]
```

## Tool-Call Agent Design

The agent uses a **pure tool-call architecture** - no JSON parsing required. The LLM must always call a tool:

**Data Tools** (loop continues):
| Tool | Purpose |
|------|---------|
| `search_and_get_departures` | Smart search + fetch departures |
| `search_stops` | Find stops by name |
| `search_routes` | Find routes/lines by name |
| `get_departures` | Get departures for a stop ID |
| `get_route_directions` | Get directions for a route |

**Terminal Tools** (loop ends):
| Tool | Purpose |
|------|---------|
| `return_result` | Structured departure data + TTS text |
| `ask_clarification` | Request user input (direction, line, etc.) |
| `return_error` | Error message to user |

## Configuration

Edit `server/config.py`:

```python
ENABLE_GUARDRAIL = False  # Set True to block off-topic queries
```

## MCP Server

For Claude Desktop integration:

```bash
python -m server.mcp_server
```

Add to Claude Desktop config:
```json
{
  "mcpServers": {
    "ptv-notify": {
      "command": "python",
      "args": ["-m", "server.mcp_server"],
      "cwd": "/path/to/ptv-notify"
    }
  }
}
```

## License

MIT
