"""
Agent Engine - Pure Tool-Call Architecture (OpenRouter)

The worker model MUST always call a tool. No JSON parsing required.
Terminal tools (return_result, ask_clarification, return_error) end the loop.
Session context is maintained for multi-turn conversations.

Strict JSON Schema Enforcement:
- All tool parameters use strict validation
- Terminal tool responses are validated with Pydantic before returning
- Ensures type-safe, predictable responses from the agent

BYOK: Accepts per-request OpenRouter API keys.
"""

import asyncio
import json
import logging
import hashlib
import os
from openai import AsyncOpenAI, APITimeoutError, APIConnectionError, BadRequestError
from pydantic import ValidationError
from . import tools
from . import session_store
from datetime import datetime
from zoneinfo import ZoneInfo
from .enums import RouteType
from . import schemas
from .config import OPENROUTER_BASE_URL, OPENROUTER_MODEL

# Set up logging for agent debugging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# No server-side key — purely BYOK (Bring Your Own Key, OpenRouter).

# Worker model — overridable via OPENROUTER_MODEL env var.
WORKER_MODEL = OPENROUTER_MODEL

# --- Tool Definitions (OpenAI / OpenRouter format) ---

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
            "name": "setup_favourite_entry",
            "description": "Configure a favourite entry slot (1-10). The user may call these entries / buttons / favourites / saved stops / slots — they all mean the same thing. Just provide station NAMES; IDs resolved automatically. Returns guidance on errors.",
            "parameters": schemas.SETUP_FAVOURITE_ENTRY_SCHEMA,
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

WORKER_TOOLS = DATA_TOOLS + TERMINAL_TOOLS

TERMINAL_TOOL_NAMES = {"return_result", "ask_clarification", "return_error"}

# --- Tool Handlers ---

TOOL_HANDLERS = {
    "search_and_get_departures": tools.search_and_get_departures,
    "search_stops": tools.search_stops,
    "search_routes": tools.search_routes,
    "get_departures": tools.get_departures,
    "get_route_directions": tools.get_route_directions,
    "setup_favourite_entry": tools.setup_favourite_entry,
}

# Tools that need extra context injected at call time (e.g. how many
# favourite entries the user already has, so the slot guard can fire).
COUNT_AWARE_TOOLS = {"setup_favourite_entry"}


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

WORKER_PROMPT = """You are a Melbourne public transport assistant. You MUST call a tool for every response. Always reply in English.

OUTPUT FORMAT — strict:
- Plain text only inside every tool call's text fields (tts_text, question_text, message).
- No markdown (no **, _, #, lists with -, no backticks), no emojis, no HTML.
- No special characters beyond standard punctuation. The output is read aloud and shown on a small monochrome watch screen.
- Be brief but informative — 1 to 3 sentences typically, target around 200-300 characters. Lead with the answer (next departure: time, platform, line / minutes), then add at most one supporting detail.

INPUT IS DICTATED, NOT TYPED:
The user's query comes from a Pebble watch's voice dictation. Expect:
- Word splits: "Bell Grave" = Belgrave, "Narrie War Wren" = Narre Warren, "Fern Tree Gully" = Ferntree Gully
- Homophones: "Coal Field" = Caulfield, "Two Rack" = Toorak, "Wear A Bee" = Werribee, "Bear Wick" = Berwick, "Patters On" = Patterson, "Glen Ferry" = Glenferrie, "Ring Wood" = Ringwood
- Casual phrasing: "the city", "cbd", "home", "next one"
Match phonetically against the KNOWN STATIONS list. Don't reject a query just because the spelling is unusual — try the search tool first.

VOCABULARY:
"button", "entry", "favourite", "saved stop", "slot" all refer to the same thing — a saved entry on the watch (slots 1-10). Treat them as synonyms.

CORE FLOW:
1. Call a DATA TOOL (search_and_get_departures, search_stops, setup_favourite_entry, etc.) to get information.
2. Read the result — if it contains [ERROR] and ACTION:, follow that ACTION instruction exactly.
3. Call a TERMINAL TOOL to respond to the user.

TERMINAL TOOLS — pick the right one:

`ask_clarification` — Use whenever your reply contains a question. If you would otherwise put a question mark in tts_text, you MUST switch to ask_clarification with explicit options instead. Triggers:
- Tool returned [ERROR] with SIMILAR STATIONS list
- User's query is ambiguous (which direction, which line, which entry slot)
- Multiple valid options exist

`return_result` — Use only when:
- You have a definitive answer (departure data, successful entry config, status read directly from a tool's output)
- The reply is a statement, not a question

`return_error` — Use when:
- User already clarified and it still fails
- System error that can't be recovered
- Query is out of scope (see CAPABILITIES section below)

WHAT WE CAN AND CANNOT DO — never invent data outside this list:

We CAN answer:
- Next departure(s) from a stop, with time, platform, line, direction
- Disruption status for shown routes — but ONLY based on what the [DISRUPTIONS: ...] line in the tool output says. The tool ALWAYS emits this line. If it says "None reported on shown routes", report exactly that. Do NOT infer from cadence or invent service details.
- Saving / configuring a favourite entry (slots 1-10)
- Disambiguating stops, routes, directions

We CANNOT answer (call return_error politely):
- "Last train tonight" / scheduled-timetable queries beyond the next ~10 departures the live tool returns
- General disruption status for a line we haven't fetched departures for (no standalone disruption tool)
- Multi-leg journey planning, transfers, cross-line directions, or anything that requires changing trains/trams (e.g. "How do I get from Belgrave to Pakenham"). For these, use this exact phrasing: "I currently can't help with directions outside of a single train or tram line."
  - EXCEPTION: City Loop stations — Flinders Street, Southern Cross, Melbourne Central, Parliament, Town Hall, Flagstaff — sit on EVERY metro train line. So "X to Town Hall", "X to the city", "X to Flinders Street" etc. is always a single-line query, never cross-line. Do NOT refuse these — call setup_favourite_entry / search_and_get_departures normally.
- Walking directions, fares, ticketing, weather, news, jokes, prompt-introspection ("what model are you"), code help, or anything outside Melbourne PT

OUT-OF-SCOPE QUERIES:
If the user asks about something we cannot do, call return_error with a brief polite message naming the limit. Do NOT call data tools to fish for an answer.

VAGUE QUERIES:
- "When's the next train?" → ask_clarification: "Which station are you at?"
- "Next train from Richmond" → ask_clarification: "Which direction?" with City/Belgrave/etc options
- "Next Pakenham train from Richmond" → search_and_get_departures, then return_result

TOOL RESULT PATTERNS:
- [ENTRY_CONFIG:...] → Success! Call return_result to confirm
- [ERROR] ... ACTION: Call ask_clarification... → Follow it! Call ask_clarification
- [ERROR] ... ACTION: Call return_error... → Follow it! Call return_error
- [STOP_INFO:...] followed by departures → Call return_result with the departure info
- [DISRUPTIONS: None reported on shown routes] → If user asked about disruptions, return_result quoting that
- [DISRUPTIONS: <titles>] → Mention the disruption(s) verbatim or summarized in return_result

FAVOURITE ENTRY CONFIGURATION:
When user wants to save an entry ("set up button 1", "save Caulfield as favourite 2", "add entry 3"):
1. Ask for START station and DESTINATION station if not provided.
2. Call `setup_favourite_entry` with entry_id, start_station, destination.
3. If [ERROR] returned, follow the ACTION instruction. Slot-guard errors will tell you to ask_clarification AND remind you to retry the tool with the SAME start_station/destination once the user picks a slot — do NOT lose that context across the round-trip.

Example: "Set up button 1 for Narre Warren to Flinders Street"
→ setup_favourite_entry(1, "Narre Warren", "Flinders Street")

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


def _get_client(llm_api_key: str) -> AsyncOpenAI:
    """Get an OpenAI-compatible client pointed at OpenRouter (BYOK)."""
    return AsyncOpenAI(api_key=llm_api_key, base_url=OPENROUTER_BASE_URL)


def _session_label(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]


async def run_worker(
    query: str,
    session_id: str,
    prefetched_context: str = "",
    llm_api_key: str | None = None,
    current_entries: int | None = None,
) -> dict:
    """
    Execute the worker loop. Always returns a structured response.
    No JSON parsing - all responses come from tool calls.
    Session history provides context for multi-turn conversations.

    Args:
        query: User's query text
        session_id: Session identifier
        prefetched_context: Optional pre-fetched departure data to inject into prompt
        llm_api_key: OpenRouter API key (BYOK)
        current_entries: How many favourite-entry slots the user currently has
            filled (0-10). Used by `setup_favourite_entry` to guard against
            gap-creating slot picks ("button 7 when only 3 are saved").
    """
    client = _get_client(llm_api_key)
    history = session_store.get_history(session_id)

    # Build system prompt with optional pre-fetched context
    system_prompt = WORKER_PROMPT
    extras = []
    if prefetched_context:
        extras.append(prefetched_context)
        extras.append("If the user's query matches one of these pre-fetched stops, use the data directly and call return_result without calling search_and_get_departures.")
    if current_entries is not None:
        extras.append(f"USER CONTEXT: They currently have {current_entries} favourite entries saved (max 10).")
    if extras:
        system_prompt = WORKER_PROMPT + "\n\n" + "\n\n".join(extras)

    # Build messages (OpenAI format: system is part of the messages list)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": query})

    learned_stop = None  # Track stop info from successful queries
    entry_config = None  # Track entry config emitted by setup_favourite_entry

    for turn in range(10):
        try:
            response = await client.chat.completions.create(
                model=WORKER_MODEL,
                max_tokens=4096,
                messages=messages,
                tools=WORKER_TOOLS,
                # Reasoning models (e.g. deepseek-reasoner backing
                # deepseek-v4-flash) reject tool_choice="required". "auto" is
                # universally supported and the prompt already pushes the
                # model toward tool use; the no-tool fallback below handles
                # the edge case where it answers in plain text.
                tool_choice="auto",
                temperature=0.0,
            )
        except APITimeoutError:
            return {"type": "ERROR", "payload": {"message": "Request timed out. Please try again.", "tts_text": "Sorry, the request timed out. Please try again.", "error_code": "OPENROUTER_TIMEOUT"}}
        except APIConnectionError:
            return {"type": "ERROR", "payload": {"message": "Could not connect to AI service.", "tts_text": "Sorry, I couldn't connect to the AI service.", "error_code": "OPENROUTER_CONNECTION"}}
        except BadRequestError as e:
            error_str = str(e)
            logger.warning(f"[Turn {turn}] OpenRouter BadRequestError: {error_str}")
            return {"type": "ERROR", "payload": {"message": "Request failed.", "tts_text": "Sorry, there was an issue processing your request.", "error_code": "OPENROUTER_BAD_REQUEST"}}
        except Exception as e:
            logger.error(f"[Turn {turn}] Unexpected error in worker: {type(e).__name__}: {e}")
            return {"type": "ERROR", "payload": {"message": f"Error: {e}", "tts_text": "Sorry, an unexpected error occurred.", "error_code": "OPENROUTER_ERROR"}}

        choice = response.choices[0]
        assistant_msg = choice.message
        tool_calls = assistant_msg.tool_calls or []

        if not tool_calls:
            # Model answered in plain text instead of calling a tool. With
            # `tool_choice="auto"` this happens for trivially refusable
            # queries ("hi", off-topic). Surface the text as a RESULT so the
            # watch displays something sensible, but cap it so it fits the
            # AppMessage budget downstream.
            text = (assistant_msg.content or "").strip() or "Sorry, I couldn't process that."
            session_store.update_history(session_id, "user", query)
            session_store.update_history(session_id, "assistant", text)
            return {
                "type": "RESULT",
                "payload": {"tts_text": text[:480]},
            }

        # Append assistant turn verbatim. We round-trip via model_dump so
        # reasoning-model fields (`reasoning`, `reasoning_details`) and the
        # exact tool_call shape (including index) are preserved. DeepSeek's
        # reasoning models reject follow-up calls with "The reasoning_content
        # in the thinking mode must be passed back to the API." if those
        # fields are dropped; manually rebuilding the dict also tends to
        # introduce shape drift (empty content vs absent content) the
        # provider doesn't like.
        asst_turn = assistant_msg.model_dump(exclude_none=True)
        asst_turn["role"] = "assistant"
        messages.append(asst_turn)

        # Process each tool call
        for tc in tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                fn_args = {}

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
                    final_msg = validated_payload.get("tts_text", "")
                    session_store.update_history(session_id, "user", query)
                    session_store.update_history(session_id, "assistant", final_msg)
                    if learned_stop:
                        validated_payload["_stop_info"] = learned_stop
                    if entry_config:
                        validated_payload["_entry_config"] = entry_config
                    if validation_errors:
                        validated_payload["_validation_errors"] = validation_errors
                    return {"type": "RESULT", "payload": validated_payload}
                elif fn_name == "ask_clarification":
                    final_msg = validated_payload.get("question_text", "")
                    session_store.update_history(session_id, "user", query)
                    session_store.update_history(session_id, "assistant", final_msg)
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
                # Inject current entry count for slot-aware tools
                if fn_name in COUNT_AWARE_TOOLS:
                    fn_args_copy["current_entries"] = current_entries
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
                    # Extract entry config if present
                    if "[ENTRY_CONFIG:" in str(result):
                        import re
                        match = re.search(r'\[ENTRY_CONFIG:({.*?})\]', str(result))
                        if match:
                            try:
                                entry_config = json.loads(match.group(1))
                                # Short-circuit: return immediately with canned response
                                entry_name = entry_config.get("name", f"Entry {entry_config.get('entry_id')}")
                                return {
                                    "type": "RESULT",
                                    "payload": {
                                        "tts_text": f"Entry configured for {entry_name}!",
                                        "destination": entry_name,
                                        "line": "Entry Configuration",
                                        "departure": {"time": "ready", "platform": None, "minutes_to_depart": 0}
                                    },
                                    "_entry_config": entry_config
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

            # Add tool result in OpenAI format
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(result),
            })

    return {"type": "ERROR", "payload": {"message": "Max turns reached", "tts_text": "Sorry, I couldn't complete your request."}}


# --- Main Entry Point ---

async def run_agent(
    query: str,
    session_id: str,
    prefetched_context: str = "",
    llm_api_key: str | None = None,
    current_entries: int | None = None,
) -> dict:
    """
    Main entry point. Runs worker (guardrail disabled for contest).
    Returns structured response dict with type and payload.

    Args:
        query: User's query text
        session_id: Session identifier
        prefetched_context: Optional pre-fetched departure data from speculative execution
        llm_api_key: OpenRouter API key (BYOK)
        current_entries: Filled favourite-entry slot count (0-10), for slot guards.
    """
    logger.info(
        "[Query] session=%s query_len=%d prefetched=%s entries=%s",
        _session_label(session_id),
        len(query),
        bool(prefetched_context),
        current_entries,
    )

    try:
        return await run_worker(
            query,
            session_id,
            prefetched_context,
            llm_api_key=llm_api_key,
            current_entries=current_entries,
        )
    except Exception as e:
        logger.error(f"Worker error: {e}")
        return {"type": "ERROR", "payload": {"message": str(e), "tts_text": "Sorry, an error occurred."}}
