"""
Agent Engine - Pure Tool-Call Architecture (Anthropic Claude)

The worker model MUST always call a tool. No JSON parsing required.
Terminal tools (return_result, ask_clarification, return_error) end the loop.
Session context is maintained for multi-turn conversations.

Strict JSON Schema Enforcement:
- All tool parameters use strict validation
- Terminal tool responses are validated with Pydantic before returning
- Ensures type-safe, predictable responses from the agent

BYOK: Accepts per-request API keys. Falls back to server ANTHROPIC_API_KEY.
"""

import asyncio
import json
import logging
import hashlib
from anthropic import AsyncAnthropic, APITimeoutError, APIConnectionError, BadRequestError
from pydantic import ValidationError
from . import tools
from . import session_store
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import json
from .enums import RouteType
from . import schemas

# Set up logging for agent debugging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# No server-side key — purely BYOK (Bring Your Own Key)

# Models
WORKER_MODEL = "claude-sonnet-4-6-20250514"

# --- Tool Definitions (Anthropic format) ---
# Anthropic uses "input_schema" instead of "parameters"

def _groq_to_anthropic_tool(tool_def: dict) -> dict:
    """Convert a Groq-format tool definition to Anthropic format."""
    fn = tool_def["function"]
    return {
        "name": fn["name"],
        "description": fn["description"],
        "input_schema": fn["parameters"],
    }


DATA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_and_get_departures",
            "description": "Search for a stop by name and get upcoming departures. Use when user specifies a stop.",
            "parameters": schemas.SEARCH_AND_GET_DEPARTURES_SCHEMA,
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_stops",
            "description": "Search for stops by name. Returns stop IDs and types.",
            "parameters": schemas.SEARCH_STOPS_SCHEMA,
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_routes",
            "description": "Search for routes/lines by name (e.g., 'Pakenham', 'Sandringham').",
            "parameters": schemas.SEARCH_ROUTES_SCHEMA,
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_departures",
            "description": "Get departures for a known stop ID.",
            "parameters": schemas.GET_DEPARTURES_SCHEMA,
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_route_directions",
            "description": "Get available directions for a route ID.",
            "parameters": schemas.GET_ROUTE_DIRECTIONS_SCHEMA,
        }
    },
    {
        "type": "function",
        "function": {
            "name": "setup_pebble_button",
            "description": "Set up a Pebble button. Just provide station NAMES - IDs resolved automatically. Returns guidance on errors.",
            "parameters": schemas.SETUP_PEBBLE_BUTTON_SCHEMA,
        }
    },
]

TERMINAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "return_result",
            "description": "Return departure information to the user. Call this when you have the data.",
            "parameters": schemas.RETURN_RESULT_SCHEMA,
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": "Ask user to choose when query is ambiguous (e.g., which direction, which line).",
            "parameters": schemas.ASK_CLARIFICATION_SCHEMA,
        }
    },
    {
        "type": "function",
        "function": {
            "name": "return_error",
            "description": "Return an error when the request cannot be fulfilled.",
            "parameters": schemas.RETURN_ERROR_SCHEMA,
        }
    },
]

# Convert to Anthropic format
ANTHROPIC_DATA_TOOLS = [_groq_to_anthropic_tool(t) for t in DATA_TOOLS]
ANTHROPIC_TERMINAL_TOOLS = [_groq_to_anthropic_tool(t) for t in TERMINAL_TOOLS]
ANTHROPIC_WORKER_TOOLS = ANTHROPIC_DATA_TOOLS + ANTHROPIC_TERMINAL_TOOLS

TERMINAL_TOOL_NAMES = {"return_result", "ask_clarification", "return_error"}

# --- Tool Handlers ---

TOOL_HANDLERS = {
    "search_and_get_departures": tools.search_and_get_departures,
    "search_stops": tools.search_stops,
    "search_routes": tools.search_routes,
    "get_departures": tools.get_departures,
    "get_route_directions": tools.get_route_directions,
    "setup_pebble_button": tools.setup_pebble_button,
}


def convert_route_type(args: dict) -> dict:
    """Convert string route_type to integer enum value."""
    if "route_type" in args and isinstance(args["route_type"], str):
        rt = args["route_type"].upper()
        if rt in RouteType.__members__:
            args["route_type"] = RouteType[rt].value
    return args


def validate_terminal_response(fn_name: str, payload: dict) -> tuple[dict, list[str]]:
    """
    Validate terminal tool response against strict schema using Pydantic.
    
    Returns:
        Tuple of (validated_payload, validation_errors)
    """
    errors = []
    
    try:
        if fn_name == "return_result":
            validated = schemas.validate_return_result(payload)
            return validated.model_dump(), errors
        elif fn_name == "ask_clarification":
            validated = schemas.validate_ask_clarification(payload)
            return validated.model_dump(), errors
        elif fn_name == "return_error":
            validated = schemas.validate_return_error(payload)
            return validated.model_dump(), errors
        else:
            return payload, [f"Unknown terminal tool: {fn_name}"]
    except ValidationError as e:
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            errors.append(f"{loc}: {err['msg']}")
        return payload, errors


# --- Worker ---

WORKER_PROMPT = """You are a Melbourne public transport assistant. You MUST call a tool for every response. Be sure to use English language.

CORE FLOW:
1. Call a DATA TOOL (search_and_get_departures, search_stops, setup_pebble_button, etc.) to get information
2. Read the result - if it contains [ERROR] and ACTION:, follow that ACTION instruction exactly
3. Call a TERMINAL TOOL to respond to the user:
   - `return_result` → You have departure info to share
   - `ask_clarification` → Need user to pick from options (station spelling, direction, line)
   - `return_error` → Unrecoverable error (user confirmed wrong station, service unavailable)

WHEN TO USE EACH TERMINAL TOOL:

`ask_clarification` - Use when:
- Tool returned [ERROR] with SIMILAR STATIONS list
- User's query is ambiguous (which direction? which line?)
- Multiple valid options exist

`return_result` - Use when:
- You have departure data (time, platform, line)
- Button was successfully configured

`return_error` - Use ONLY when:
- User already clarified and it still fails
- System error that can't be recovered

VAGUE QUERIES:
- "When's the next train?" → ask_clarification: "Which station are you at?"
- "Next train from Richmond" → ask_clarification: "Which direction?" with City/Belgrave/etc options
- "Next Pakenham train from Richmond" → search_and_get_departures, then return_result

TOOL RESULT PATTERNS:
- [BUTTON_CONFIG:...] → Success! Call return_result to confirm
- [ERROR] ... ACTION: Call ask_clarification... → Follow it! Call ask_clarification
- [ERROR] ... ACTION: Call return_error... → Follow it! Call return_error
- [STOP_INFO:...] followed by departures → Call return_result with the departure info

PEBBLE BUTTON CONFIGURATION:
When user wants to set up a Pebble button ("configure button", "set up my watch"):
1. Ask for START station and DESTINATION station if not provided
2. Call `setup_pebble_button` with button_id, start_station, destination
3. If [ERROR] returned, follow the ACTION instruction

Example: "Set up button 1 for Narre Warren to Flinders Street"
→ setup_pebble_button(1, "Narre Warren", "Flinders Street")

ERROR HANDLING:
- [API_ERROR] → return_error with "temporary service issue"
- [MCP_ERROR] → return_error with "processing error" """

# Inject known station names to help with spelling
try:
    _db_path = os.path.join(os.path.dirname(__file__), "stations_train.json")
    if os.path.exists(_db_path):
        with open(_db_path, "r") as f:
            _st_data = json.load(f)
            _st_names = [s["name"].replace(" Station", "") for s in _st_data.get("stops", [])]
            _st_list = ", ".join(_st_names[:300])
            WORKER_PROMPT += f"\n\nKNOWN STATIONS (for spelling correction):\n{_st_list}"
except Exception as e:
    print(f"Failed to load station names for prompt: {e}")


def _get_client(llm_api_key: str) -> AsyncAnthropic:
    """Get an Anthropic client using the user-provided key (BYOK)."""
    return AsyncAnthropic(api_key=llm_api_key)


def _session_label(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]


async def run_worker(query: str, session_id: str, prefetched_context: str = "", llm_api_key: str | None = None) -> dict:
    """
    Execute the worker loop. Always returns a structured response.
    No JSON parsing - all responses come from tool calls.
    Session history provides context for multi-turn conversations.

    Args:
        query: User's query text
        session_id: Session identifier
        prefetched_context: Optional pre-fetched departure data to inject into prompt
        llm_api_key: Optional per-request Anthropic API key (BYOK)
    """
    client = _get_client(llm_api_key)
    history = session_store.get_history(session_id)

    # Build system prompt with optional pre-fetched context
    system_prompt = WORKER_PROMPT
    if prefetched_context:
        system_prompt = f"{WORKER_PROMPT}\n\n{prefetched_context}\n\nIf the user's query matches one of these pre-fetched stops, use the data directly and call return_result without calling search_and_get_departures."

    # Build messages (Anthropic format: system is separate, not in messages)
    messages = []
    messages.extend(history)
    messages.append({"role": "user", "content": query})

    learned_stop = None  # Track stop info from successful queries
    button_config = None  # Track button config from configure_pebble_button

    for turn in range(10):
        try:
            response = await client.messages.create(
                model=WORKER_MODEL,
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
                tools=ANTHROPIC_WORKER_TOOLS,
                tool_choice={"type": "any"},  # Force tool use
                temperature=0.0,
            )
        except APITimeoutError:
            return {"type": "ERROR", "payload": {"message": "Request timed out. Please try again.", "tts_text": "Sorry, the request timed out. Please try again.", "error_code": "ANTHROPIC_TIMEOUT"}}
        except APIConnectionError:
            return {"type": "ERROR", "payload": {"message": "Could not connect to AI service.", "tts_text": "Sorry, I couldn't connect to the AI service.", "error_code": "ANTHROPIC_CONNECTION"}}
        except BadRequestError as e:
            error_str = str(e)
            logger.warning(f"[Turn {turn}] Anthropic BadRequestError: {error_str}")
            return {"type": "ERROR", "payload": {"message": "Request failed.", "tts_text": "Sorry, there was an issue processing your request.", "error_code": "ANTHROPIC_BAD_REQUEST"}}
        except Exception as e:
            logger.error(f"[Turn {turn}] Unexpected error in worker: {type(e).__name__}: {e}")
            return {"type": "ERROR", "payload": {"message": f"Error: {e}", "tts_text": "Sorry, an unexpected error occurred.", "error_code": "ANTHROPIC_ERROR"}}

        # Process response content blocks
        # Anthropic returns content as a list of blocks (text and/or tool_use)
        tool_use_blocks = [block for block in response.content if block.type == "tool_use"]

        if not tool_use_blocks:
            # No tool calls - shouldn't happen with tool_choice=any
            return {"type": "ERROR", "payload": {"message": "No tool called", "tts_text": "Sorry, something went wrong."}}

        # Add assistant response to messages
        messages.append({"role": "assistant", "content": response.content})

        # Process each tool use block
        tool_results = []
        
        for block in tool_use_blocks:
            fn_name = block.name
            fn_args = block.input

            logger.info(
                "[Turn %d] Tool call: %s arg_keys=%s",
                turn,
                fn_name,
                sorted(fn_args.keys()),
            )

            # Terminal tools - validate with Pydantic and return
            if fn_name in TERMINAL_TOOL_NAMES:
                validated_payload, validation_errors = validate_terminal_response(fn_name, fn_args)
                
                if validation_errors:
                    logger.warning(f"[Turn {turn}] Schema validation errors for {fn_name}: {validation_errors}")
                
                if fn_name == "return_result":
                    assistant_msg = validated_payload.get("tts_text", "")
                    session_store.update_history(session_id, "user", query)
                    session_store.update_history(session_id, "assistant", assistant_msg)
                    if learned_stop:
                        validated_payload["_stop_info"] = learned_stop
                    if button_config:
                        validated_payload["_button_config"] = button_config
                    if validation_errors:
                        validated_payload["_validation_errors"] = validation_errors
                    return {"type": "RESULT", "payload": validated_payload}
                elif fn_name == "ask_clarification":
                    assistant_msg = validated_payload.get("question_text", "")
                    session_store.update_history(session_id, "user", query)
                    session_store.update_history(session_id, "assistant", assistant_msg)
                    if validation_errors:
                        validated_payload["_validation_errors"] = validation_errors
                    return {"type": "CLARIFICATION", "payload": validated_payload}
                elif fn_name == "return_error":
                    if validation_errors:
                        validated_payload["_validation_errors"] = validation_errors
                    return {"type": "ERROR", "payload": validated_payload}

            # Data tools - execute and continue
            handler = TOOL_HANDLERS.get(fn_name)
            if handler:
                fn_args_copy = dict(fn_args)
                fn_args_copy = convert_route_type(fn_args_copy)
                # Inject session_id for tools that need it
                if fn_name == "search_and_get_departures":
                    fn_args_copy["session_id"] = session_id
                try:
                    result = await handler(**fn_args_copy)
                    # Extract stop info for client to learn
                    if "[STOP_INFO:" in str(result):
                        import re
                        match = re.search(r'\[STOP_INFO:(\d+):(\d+):([^\]]+)\]', str(result))
                        if match:
                            learned_stop = {
                                "stop_id": int(match.group(1)),
                                "route_type": int(match.group(2)),
                                "stop_name": match.group(3)
                            }
                    # Extract button config if present
                    if "[BUTTON_CONFIG:" in str(result):
                        import re
                        match = re.search(r'\[BUTTON_CONFIG:({.*?})\]', str(result))
                        if match:
                            try:
                                button_config = json.loads(match.group(1))
                                # Short-circuit: return immediately with canned response
                                btn_name = button_config.get("name", f"Button {button_config.get('button_id')}")
                                return {
                                    "type": "RESULT",
                                    "payload": {
                                        "tts_text": f"Button configured for {btn_name}!",
                                        "destination": btn_name,
                                        "line": "Button Configuration",
                                        "departure": {"time": "ready", "platform": None, "minutes_to_depart": 0}
                                    },
                                    "_button_config": button_config
                                }
                            except json.JSONDecodeError:
                                pass
                except Exception as e:
                    result = f"Error: {e}"
            else:
                result = f"Unknown tool: {fn_name}"

            # Log the tool result for debugging
            result_length = len(str(result))
            logger.info(
                "[Turn %d] Tool result from %s length=%d",
                turn,
                fn_name,
                result_length,
            )

            # Add tool result in Anthropic format
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(result),
            })

        # Add all tool results as a user message
        if tool_results:
            messages.append({"role": "user", "content": tool_results})

    return {"type": "ERROR", "payload": {"message": "Max turns reached", "tts_text": "Sorry, I couldn't complete your request."}}


# --- Main Entry Point ---

async def run_agent(query: str, session_id: str, prefetched_context: str = "", llm_api_key: str | None = None) -> dict:
    """
    Main entry point. Runs worker (guardrail disabled for contest).
    Returns structured response dict with type and payload.

    Args:
        query: User's query text
        session_id: Session identifier
        prefetched_context: Optional pre-fetched departure data from speculative execution
        llm_api_key: Optional per-request Anthropic API key (BYOK)
    """
    logger.info(
        "[Query] session=%s query_len=%d prefetched=%s",
        _session_label(session_id),
        len(query),
        bool(prefetched_context),
    )
    
    try:
        return await run_worker(query, session_id, prefetched_context, llm_api_key=llm_api_key)
    except Exception as e:
        logger.error(f"Worker error: {e}")
        return {"type": "ERROR", "payload": {"message": str(e), "tts_text": "Sorry, an error occurred."}}
