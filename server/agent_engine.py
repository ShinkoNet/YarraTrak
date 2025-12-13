"""
Agent Engine - Pure Tool-Call Architecture

The worker model MUST always call a tool. No JSON parsing required.
Terminal tools (return_result, ask_clarification, return_error) end the loop.
Session context is maintained for multi-turn conversations (ASR corrections, clarifications).

Strict JSON Schema Enforcement:
- All tool parameters use `additionalProperties: false` for strict validation
- Terminal tool responses are validated with Pydantic before returning
- Ensures type-safe, predictable responses from the agent
"""

import asyncio
import json
import httpx
from groq import AsyncGroq, APITimeoutError, APIConnectionError, BadRequestError
from pydantic import ValidationError
from . import config
from . import tools
from . import session_store
from .enums import RouteType
from . import schemas

groq_client = AsyncGroq(api_key=config.GROQ_API_KEY)

# Models
GUARDRAIL_MODEL = "openai/gpt-oss-safeguard-20b"
WORKER_MODEL = "moonshotai/kimi-k2-instruct"

# --- Tool Definitions with Strict Schemas ---
# All schemas use `additionalProperties: false` for strict JSON validation

DATA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_and_get_departures",
            "description": "Search for a stop by name and get upcoming departures. Use when user specifies a stop.",
            "parameters": schemas.SEARCH_AND_GET_DEPARTURES_SCHEMA,
            "strict": True
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_stops",
            "description": "Search for stops by name. Returns stop IDs and types.",
            "parameters": schemas.SEARCH_STOPS_SCHEMA,
            "strict": True
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_routes",
            "description": "Search for routes/lines by name (e.g., 'Pakenham', 'Sandringham').",
            "parameters": schemas.SEARCH_ROUTES_SCHEMA,
            "strict": True
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_departures",
            "description": "Get departures for a known stop ID.",
            "parameters": schemas.GET_DEPARTURES_SCHEMA,
            "strict": True
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_route_directions",
            "description": "Get available directions for a route ID.",
            "parameters": schemas.GET_ROUTE_DIRECTIONS_SCHEMA,
            "strict": True
        }
    },
    {
        "type": "function",
        "function": {
            "name": "configure_pebble_button",
            "description": "Generate configuration for a Pebble watch stealth button. Use when user wants to set up a quick-access button.",
            "parameters": schemas.CONFIGURE_PEBBLE_BUTTON_SCHEMA,
            "strict": True
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
            "strict": True
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": "Ask user to choose when query is ambiguous (e.g., which direction, which line).",
            "parameters": schemas.ASK_CLARIFICATION_SCHEMA,
            "strict": True
        }
    },
    {
        "type": "function",
        "function": {
            "name": "return_error",
            "description": "Return an error when the request cannot be fulfilled.",
            "parameters": schemas.RETURN_ERROR_SCHEMA,
            "strict": True
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
    "configure_pebble_button": tools.configure_pebble_button,
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
        - validated_payload: The normalized payload (may have coerced types)
        - validation_errors: List of validation error messages (empty if valid)
    """
    errors = []
    
    try:
        if fn_name == "return_result":
            validated = schemas.validate_return_result(payload)
            # Convert back to dict, now with validated/coerced types
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
        # Collect all validation errors
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            errors.append(f"{loc}: {err['msg']}")
        # Return original payload with errors
        return payload, errors




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
3. When you have departure data, call `return_result` with ONLY the single next departure.
4. If the requested service isn't available, call `return_result` with available alternatives or `return_error`.
5. Victoria, Australia only. Trains, trams, buses, V/Line.
6. Use conversation history to understand context (corrections, follow-ups).

DIRECTION LOGIC:
- "Next train from Richmond" → ambiguous (which line?), ask clarification
- "Next Pakenham train from Richmond" → clear (Pakenham line = away from city)
- "Next train to the city from Richmond" → clear (city-bound)
- "Next 96 tram" → ambiguous (St Kilda or East Brunswick?), ask clarification

ASR RECOVERY (CRITICAL):
- Voice input often has spelling errors. NEVER call `return_error` on first search failure.
- If search returns no stops, ALWAYS call `ask_clarification` with your best guess at the correct spelling.
- Common ASR mistakes: dropped letters, phonetic spellings, word boundaries
- Examples:
  - "Nary Warren" → ask "Did you mean Narre Warren?"
  - "Sandringam" → ask "Did you mean Sandringham?"
  - "Flinder Street" → ask "Did you mean Flinders Street?"
  - "Camber Well" → ask "Did you mean Camberwell?"
- Think about what station SOUNDS like the input, not just exact matches.
- Only call `return_error` if the user confirms the spelling and it still doesn't exist.

PEBBLE BUTTON CONFIGURATION:
When the user wants to set up a Pebble watch button (phrases like "configure button", "set up my watch", "stealth button"):
1. First use `search_stops` to find the stop_id
2. If direction matters, use `get_route_directions` to find the direction_id
3. Call `configure_pebble_button` with the button_id (1, 2, or 3), stop_name, stop_id, route_type, and optionally direction_id
4. Tell the user to enter these values in their Pebble app settings

ERROR HANDLING:
- If you see [API_ERROR], tell the user there's a temporary service issue.
- If you see [MCP_ERROR], tell the user there was a processing error.

Use data tools to fetch information, then call a terminal tool (return_result, ask_clarification, or return_error)."""


async def run_worker(query: str, session_id: str, prefetched_context: str = "") -> dict:
    """
    Execute the worker loop. Always returns a structured response.
    No JSON parsing - all responses come from tool calls.
    Session history provides context for multi-turn conversations.

    Args:
        query: User's query text
        session_id: Session identifier
        prefetched_context: Optional pre-fetched departure data to inject into prompt
    """
    history = session_store.get_history(session_id)

    # Build system prompt with optional pre-fetched context
    system_prompt = WORKER_PROMPT
    if prefetched_context:
        system_prompt = f"{WORKER_PROMPT}\n\n{prefetched_context}\n\nIf the user's query matches one of these pre-fetched stops, use the data directly and call return_result without calling search_and_get_departures."

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": query})

    tool_use_retries = 0
    max_tool_use_retries = 3
    learned_stop = None  # Track stop info from successful queries
    button_config = None  # Track button config from configure_pebble_button

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

            # Terminal tools - validate with Pydantic and return
            if fn_name in TERMINAL_TOOL_NAMES:
                # Validate response against strict schema
                validated_payload, validation_errors = validate_terminal_response(fn_name, fn_args)
                
                if validation_errors:
                    print(f"[Turn {turn}] Schema validation errors for {fn_name}: {validation_errors}")
                    # Log but continue with best-effort response
                
                # Build a summary of the assistant's response for history
                if fn_name == "return_result":
                    assistant_msg = validated_payload.get("tts_text", "")
                    session_store.update_history(session_id, "user", query)
                    session_store.update_history(session_id, "assistant", assistant_msg)
                    # Include learned stop info for client to store
                    if learned_stop:
                        validated_payload["_stop_info"] = learned_stop
                    # Include button config for client to store
                    if button_config:
                        validated_payload["_button_config"] = button_config
                    # Include validation status for debugging
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
                fn_args = convert_route_type(fn_args)
                # Inject session_id for tools that need it
                if fn_name == "search_and_get_departures":
                    fn_args["session_id"] = session_id
                try:
                    result = await handler(**fn_args)
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

            messages.append({
                "tool_call_id": tc.id,
                "role": "tool",
                "name": fn_name,
                "content": str(result)
            })

    return {"type": "ERROR", "payload": {"message": "Max turns reached", "tts_text": "Sorry, I couldn't complete your request."}}


# --- Main Entry Point ---

async def run_agent(query: str, session_id: str, prefetched_context: str = "") -> dict:
    """
    Main entry point. Runs guardrail (if enabled) and worker.
    Returns structured response dict with type and payload.

    Args:
        query: User's query text
        session_id: Session identifier
        prefetched_context: Optional pre-fetched departure data from speculative execution
    """
    if config.ENABLE_GUARDRAIL:
        # Run guardrail and worker in parallel
        guardrail_task = asyncio.create_task(run_guardrail(query, session_id))
        worker_task = asyncio.create_task(run_worker(query, session_id, prefetched_context))

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
            return await run_worker(query, session_id, prefetched_context)
        except Exception as e:
            print(f"Worker error: {e}")
            return {"type": "ERROR", "payload": {"message": str(e), "tts_text": "Sorry, an error occurred."}}
