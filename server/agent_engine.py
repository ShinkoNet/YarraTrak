"""
Agent Engine - Pure Tool-Call Architecture

The worker model MUST always call a tool. No JSON parsing required.
Terminal tools (return_result, ask_clarification, return_error) end the loop.
Session context is maintained for multi-turn conversations (ASR corrections, clarifications).
"""

import asyncio
import json
import httpx
from groq import AsyncGroq, APITimeoutError, APIConnectionError, BadRequestError
from . import config
from . import tools
from . import session_store
from .enums import RouteType

groq_client = AsyncGroq(api_key=config.GROQ_API_KEY)

# Models
GUARDRAIL_MODEL = "openai/gpt-oss-safeguard-20b"
WORKER_MODEL = "moonshotai/kimi-k2-instruct"

# --- Tool Definitions ---

DATA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_and_get_departures",
            "description": "Search for a stop by name and get upcoming departures. Use when user specifies a stop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Stop name (e.g., 'Richmond', 'Flinders Street')"},
                    "route_type": {
                        "type": "string",
                        "enum": ["TRAIN", "TRAM", "BUS", "VLINE", "NIGHT_BUS"],
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
            "name": "search_stops",
            "description": "Search for stops by name. Returns stop IDs and types.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Stop name to search"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_routes",
            "description": "Search for routes/lines by name (e.g., 'Pakenham', 'Sandringham').",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Route name to search"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_departures",
            "description": "Get departures for a known stop ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "stop_id": {"type": "integer", "description": "PTV stop ID"},
                    "route_type": {
                        "type": "string",
                        "enum": ["TRAIN", "TRAM", "BUS", "VLINE", "NIGHT_BUS"],
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
            "name": "get_route_directions",
            "description": "Get available directions for a route ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "route_id": {"type": "integer", "description": "PTV route ID"}
                },
                "required": ["route_id"]
            }
        }
    },
]

TERMINAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "return_result",
            "description": "Return departure information to the user. Call this when you have the data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "destination": {"type": "string", "description": "Where the service is heading"},
                    "line": {"type": "string", "description": "Line/route name (e.g., 'Pakenham Line', 'Route 96')"},
                    "departures": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "time": {"type": "string", "description": "Departure time (HH:MM)"},
                                "platform": {"type": ["string", "integer", "null"], "description": "Platform number or null"},
                                "minutes_to_depart": {"type": "integer", "description": "Minutes until departure"}
                            },
                            "required": ["time", "minutes_to_depart"]
                        },
                        "minItems": 1
                    },
                    "tts_text": {"type": "string", "description": "Natural speech for TTS (e.g., 'Next train in 5 minutes from platform 3')"}
                },
                "required": ["destination", "line", "departures", "tts_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": "Ask user to choose when query is ambiguous (e.g., which direction, which line).",
            "parameters": {
                "type": "object",
                "properties": {
                    "question_text": {"type": "string", "description": "Question to ask (e.g., 'Which direction?')"},
                    "missing_entity": {"type": "string", "description": "What's missing: 'direction', 'line', 'stop'"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Display text"},
                                "value": {"type": "string", "description": "Value to use if selected"}
                            },
                            "required": ["label", "value"]
                        },
                        "minItems": 2,
                        "maxItems": 6
                    }
                },
                "required": ["question_text", "missing_entity", "options"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "return_error",
            "description": "Return an error when the request cannot be fulfilled.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Error message for the user"},
                    "tts_text": {"type": "string", "description": "Spoken error message"}
                },
                "required": ["message", "tts_text"]
            }
        }
    },
]

WORKER_TOOLS = DATA_TOOLS + TERMINAL_TOOLS
TERMINAL_TOOL_NAMES = {"return_result", "ask_clarification", "return_error"}

# --- Tool Handlers ---

TOOL_HANDLERS = {
    "search_and_get_departures": tools.search_and_get_departures,
    "search_stops": tools.search_stops,
    "search_routes": tools.search_routes,
    "get_departures": tools.get_departures,
    "get_route_directions": tools.get_route_directions,
}


def convert_route_type(args: dict) -> dict:
    """Convert string route_type to integer enum value."""
    if "route_type" in args and isinstance(args["route_type"], str):
        rt = args["route_type"].upper()
        if rt in RouteType.__members__:
            args["route_type"] = RouteType[rt].value
    return args


def normalize_result_payload(payload: dict) -> dict:
    """Normalize return_result payload - coerce types for robustness."""
    if "departures" in payload:
        for dep in payload["departures"]:
            # Coerce platform to string (model sometimes passes int)
            if "platform" in dep and dep["platform"] is not None:
                dep["platform"] = str(dep["platform"])
            # Coerce minutes_to_depart to int
            if "minutes_to_depart" in dep:
                try:
                    dep["minutes_to_depart"] = int(dep["minutes_to_depart"])
                except (ValueError, TypeError):
                    dep["minutes_to_depart"] = 0
    return payload


# --- Guardrail ---

GUARDRAIL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "allow",
            "description": "Allow: transport queries, greetings, time/weather questions.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "block",
            "description": "Block: coding, creative writing, general knowledge, harmful content.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
]

GUARDRAIL_PROMPT = """You are a content classifier for a Melbourne public transport assistant.
Call `allow` for transport queries, greetings, time/weather, or responses that make sense in the conversation context.
Call `block` for coding, trivia, creative writing, or harmful content.

IMPORTANT: Consider conversation history. A reply like "Yes", "The second one", or a station name may seem off-topic in isolation but is valid if it follows a transport-related question."""


async def run_guardrail(query: str, session_id: str) -> bool:
    """Returns True if query is allowed, False if blocked."""
    try:
        history = session_store.get_history(session_id)

        messages = [{"role": "system", "content": GUARDRAIL_PROMPT}]
        messages.extend(history)
        messages.append({"role": "user", "content": query})

        response = await groq_client.chat.completions.create(
            model=GUARDRAIL_MODEL,
            messages=messages,
            tools=GUARDRAIL_TOOLS,
            tool_choice="required",
            temperature=0.0,
        )
        tool_call = response.choices[0].message.tool_calls[0]
        return tool_call.function.name == "allow"
    except Exception as e:
        print(f"Guardrail error: {e}")
        return True  # Fail open


# --- Worker ---

WORKER_PROMPT = """You are a Melbourne public transport assistant. You MUST call a tool for every response.

RULES:
1. Always call a tool. Never output plain text - put any explanation in tts_text or question_text.
2. For ambiguous queries (multiple directions/lines possible), call `ask_clarification`.
3. When you have departure data, call `return_result` with structured info.
4. If the requested service isn't available, call `return_result` with available alternatives or `return_error`.
5. Victoria, Australia only. Trains, trams, buses, V/Line.
6. Use conversation history to understand context (corrections, follow-ups).

DIRECTION LOGIC:
- "Next train from Richmond" → ambiguous (which line?), ask clarification
- "Next Pakenham train from Richmond" → clear (Pakenham line = away from city)
- "Next train to the city from Richmond" → clear (city-bound)
- "Next 96 tram" → ambiguous (St Kilda or East Brunswick?), ask clarification

NO RESULTS / ASR RECOVERY:
- If search returns no stops, the station name may be misheard (ASR error).
- Before calling `return_error`, call `ask_clarification` to suggest similar-sounding stations.
- Example: "Sandringam" not found → ask "Did you mean Sandringham?"
- Only call `return_error` after clarification fails or if you're confident the station doesn't exist.

ERROR HANDLING:
- If you see [API_ERROR], tell the user there's a temporary service issue.
- If you see [MCP_ERROR], tell the user there was a processing error.

Use data tools to fetch information, then call a terminal tool (return_result, ask_clarification, or return_error)."""


async def run_worker(query: str, session_id: str) -> dict:
    """
    Execute the worker loop. Always returns a structured response.
    No JSON parsing - all responses come from tool calls.
    Session history provides context for multi-turn conversations.
    """
    history = session_store.get_history(session_id)

    messages = [{"role": "system", "content": WORKER_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": query})

    tool_use_retries = 0
    max_tool_use_retries = 3

    for turn in range(10):
        # Add prefill to nudge model toward tool use (helps with some models)
        messages_with_prefill = messages + [
            {"role": "assistant", "content": "I'll call the appropriate tool now.\n"}
        ]

        try:
            response = await groq_client.chat.completions.create(
                model=WORKER_MODEL,
                messages=messages_with_prefill,
                tools=WORKER_TOOLS,
                tool_choice="required",
                temperature=0.0,
            )
        except APITimeoutError:
            return {"type": "ERROR", "payload": {"message": "Request timed out. Please try again.", "tts_text": "Sorry, the request timed out. Please try again.", "error_code": "GROQ_TIMEOUT"}}
        except APIConnectionError:
            return {"type": "ERROR", "payload": {"message": "Could not connect to AI service.", "tts_text": "Sorry, I couldn't connect to the AI service.", "error_code": "GROQ_CONNECTION"}}
        except BadRequestError as e:
            # Handle tool_use_failed - model tried to output text instead of tool call
            if "tool_use_failed" in str(e):
                tool_use_retries += 1
                print(f"[Turn {turn}] Tool use failed, retry {tool_use_retries}/{max_tool_use_retries}")
                if tool_use_retries >= max_tool_use_retries:
                    return {"type": "ERROR", "payload": {"message": "Could not process request.", "tts_text": "Sorry, I had trouble processing that request.", "error_code": "TOOL_USE_FAILED"}}
                # Add nudge toward clarification
                messages.append({
                    "role": "user",
                    "content": "Please use one of the available tools. If you're unsure what the user wants, use ask_clarification to ask them."
                })
                continue
            raise

        msg = response.choices[0].message
        tool_calls = msg.tool_calls

        if not tool_calls:
            # Should not happen with tool_choice="required"
            return {"type": "ERROR", "payload": {"message": "No tool called", "tts_text": "Sorry, something went wrong."}}

        messages.append(msg)

        for tc in tool_calls:
            fn_name = tc.function.name
            fn_args = json.loads(tc.function.arguments)

            print(f"[Turn {turn}] Tool: {fn_name}({fn_args})")

            # Terminal tools - save to history and return
            if fn_name in TERMINAL_TOOL_NAMES:
                # Build a summary of the assistant's response for history
                if fn_name == "return_result":
                    fn_args = normalize_result_payload(fn_args)
                    assistant_msg = fn_args.get("tts_text", "")
                    session_store.update_history(session_id, "user", query)
                    session_store.update_history(session_id, "assistant", assistant_msg)
                    return {"type": "RESULT", "payload": fn_args}
                elif fn_name == "ask_clarification":
                    assistant_msg = fn_args.get("question_text", "")
                    session_store.update_history(session_id, "user", query)
                    session_store.update_history(session_id, "assistant", assistant_msg)
                    return {"type": "CLARIFICATION", "payload": fn_args}
                elif fn_name == "return_error":
                    return {"type": "ERROR", "payload": fn_args}

            # Data tools - execute and continue
            handler = TOOL_HANDLERS.get(fn_name)
            if handler:
                fn_args = convert_route_type(fn_args)
                # Inject session_id for tools that need it
                if fn_name == "search_and_get_departures":
                    fn_args["session_id"] = session_id
                try:
                    result = await handler(**fn_args)
                except Exception as e:
                    result = f"Error: {e}"
            else:
                result = f"Unknown tool: {fn_name}"

            messages.append({
                "tool_call_id": tc.id,
                "role": "tool",
                "name": fn_name,
                "content": str(result)
            })

    return {"type": "ERROR", "payload": {"message": "Max turns reached", "tts_text": "Sorry, I couldn't complete your request."}}


# --- Main Entry Point ---

async def run_agent(query: str, session_id: str) -> dict:
    """
    Main entry point. Runs guardrail (if enabled) and worker.
    Returns structured response dict with type and payload.
    """
    if config.ENABLE_GUARDRAIL:
        # Run guardrail and worker in parallel
        guardrail_task = asyncio.create_task(run_guardrail(query, session_id))
        worker_task = asyncio.create_task(run_worker(query, session_id))

        is_allowed = await guardrail_task

        if not is_allowed:
            worker_task.cancel()
            return {
                "type": "ERROR",
                "payload": {
                    "message": "I can only help with public transport queries.",
                    "tts_text": "I can only help with public transport queries.",
                    "error_code": "GUARDRAIL_BLOCK"
                }
            }

        try:
            return await worker_task
        except asyncio.CancelledError:
            return {"type": "ERROR", "payload": {"message": "Cancelled", "tts_text": "Request cancelled."}}
        except Exception as e:
            print(f"Worker error: {e}")
            return {"type": "ERROR", "payload": {"message": str(e), "tts_text": "Sorry, an error occurred."}}
    else:
        # Guardrail disabled - run worker directly
        try:
            return await run_worker(query, session_id)
        except Exception as e:
            print(f"Worker error: {e}")
            return {"type": "ERROR", "payload": {"message": str(e), "tts_text": "Sorry, an error occurred."}}
