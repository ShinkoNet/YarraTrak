import asyncio
import json
import os
from groq import AsyncGroq
from . import config
from . import tools
from . import session_store
from .enums import RouteType

# Initialize Groq Client
groq_client = AsyncGroq(api_key=config.GROQ_API_KEY)

# Models
GUARDRAIL_MODEL = "openai/gpt-oss-safeguard-20b"
WORKER_MODEL = "moonshotai/kimi-k2-instruct"

# Tool Definitions (Moved from api.py)
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
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": "Ask the user for clarification when multiple options exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question_text": {"type": "string", "description": "The question to ask (e.g. 'Which direction?')"},
                    "missing_entity": {"type": "string", "description": "The concept missing (e.g. 'direction', 'line')"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Display text (e.g. 'City')"},
                                "value": {"type": "string", "description": "Internal value (e.g. 'inbound')"}
                            },
                            "required": ["label", "value"]
                        }
                    }
                },
                "required": ["question_text", "missing_entity", "options"]
            }
        }
    }
]

async def run_speculative_race(query: str, session_id: str):
    """
    Run Guardrail and Worker in parallel.
    """
    print(f"Starting Race for: {query}")
    
    guardrail_task = asyncio.create_task(run_guardrail(query, session_id))
    worker_task = asyncio.create_task(run_worker(query, session_id))
    
    # Safety First
    is_safe, refusal_reason = await guardrail_task
    
    if not is_safe:
        print(f"Guardrail BLOCKED: {refusal_reason}")
        worker_task.cancel()
        return {
            "status": "success",
            "type": "ERROR",
            "payload": {
                "message": "I can only help with public transport queries.",
                "error_code": "GUARDRAIL_BLOCK"
            }
        }
        
    try:
        result = await worker_task
        return result
    except asyncio.CancelledError:
        return {"status": "error", "type": "ERROR", "payload": {"message": "Task cancelled"}}
    except Exception as e:
        print(f"Worker Failed: {e}")
        import traceback
        traceback.print_exc()
        return {
            "status": "success", 
            "type": "ERROR", 
            "payload": {
                "message": "Sorry, I encountered an error processing your request.",
                "debug": str(e)
            }
        }

async def run_guardrail(query: str, session_id: str):
    """
    Uses GPT-OSS-Safeguard with Tool Calling to strictly classify requests.
    """
    guardrail_tools = [
        {
            "type": "function",
            "function": {
                "name": "allow_request",
                "description": "Allow the request to proceed if it is related to public transport, travel, time, or general conversation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "rationale": {"type": "string", "description": "Why this request is safe."}
                    },
                    "required": ["rationale"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "block_request",
                "description": "Block the request if it is unrelated to transport (e.g. coding, creative writing, general knowledge).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "rationale": {"type": "string", "description": "Why this request is unsafe."}
                    },
                    "required": ["rationale"]
                }
            }
        }
    ]

    try:
        policy = """# Public Transport Agent Scope Policy

## INSTRUCTIONS
You are a strict content classifier. You MUST call either `allow_request` or `block_request`.

## DEFINITIONS
- **Transport Query**: Requests for schedules, departures, routes, directions, or stop locations.
- **General Interaction**: Greetings, thanks, or simple conversational openers.
- **Out of Scope**: Requests for coding, creative writing, general knowledge (history, math), or sensitive topics.

## VIOLATES (Call block_request)
- Requests to write code or explain programming concepts.
- Requests for creative writing (poems, stories, jokes).
- General knowledge questions unrelated to travel.
- Malicious or sensitive content.

## SAFE (Call allow_request)
- Questions about trains, trams, buses, V/Line.
- Questions about time, dates, or weather.
- Navigation or location questions.
- Standard greetings ("Hi", "Hello")."""
        
        messages = [{"role": "system", "content": policy}]
        
        # Add History for Context
        history = session_store.get_history(session_id)
        messages.extend(history)
        
        messages.append({"role": "user", "content": query})

        chat_completion = await groq_client.chat.completions.create(
            model=GUARDRAIL_MODEL,
            messages=messages,
            tools=guardrail_tools,
            tool_choice="required", # Force tool use
            temperature=0.0,
        )
        
        tool_calls = chat_completion.choices[0].message.tool_calls
        
        if tool_calls:
            fn_name = tool_calls[0].function.name
            args = json.loads(tool_calls[0].function.arguments)
            rationale = args.get("rationale", "No rationale")
            print(f"Guardrail Decision: {fn_name} ({rationale})")
            
            if fn_name == "allow_request":
                return True, None
            elif fn_name == "block_request":
                return False, rationale
        
        print("Guardrail Error: No tool called.")
        return True, None # Fail Open
            
    except Exception as e:
        print(f"Guardrail Error: {e}")
        return True, None # Fail Open

async def run_worker(query: str, session_id: str):
    """
    The main agent logic using Kimi-k2.
    """
    history = session_store.get_history(session_id)
    
    system_prompt = """You are a semantic router for a Headless Transport Agent in Melbourne, Australia.
Your output MUST be a JSON object describing the next state.
You have access to tools to fetch real-time data.

**CORE RULES:**
1. **Strict Determinism:** Do not output conversational filler. Output ONLY JSON or Tool Calls.
2. **Single Direction:** Never provide multiple directions. If ambiguous, use `ask_clarification`.
3. **Vic Only:** You operate in Victoria, Australia.
4. **Tram vs Train:** Trams often don't have platform numbers.
5. **Ambiguity:** If a user says "Next train" and there are multiple lines/directions, you MUST ask for clarification.

**OUTPUT SCHEMA (Final Response):**
You must eventually output one of these JSON structures:

TYPE A: RESULT (When you have data)
{
  "type": "RESULT",
  "payload": {
    "destination": "Flinders Street",
    "departures": [
      {
        "time": "10:05",
        "platform": "4", // or null
        "status": "ON TIME",
        "line": "Frankston Line",
        "minutes_to_depart": 12 // Calculated integer
      }
    ],
    "tts_text": "The next train to Flinders Street departs in 12 minutes from Platform 1."
  }
}

TYPE B: CLARIFICATION (When you need more info)
{
  "type": "CLARIFICATION",
  "payload": {
    "question_text": "Which direction?",
    "missing_entity": "direction",
    "options": [
      { "label": "City", "value": "inbound" },
      { "label": "Outbound", "value": "outbound" }
    ]
  }
}
"""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": query})
    
    for _ in range(5): # Max 5 turns
        response = await groq_client.chat.completions.create(
            model=WORKER_MODEL,
            messages=messages,
            tools=tools_schema,
            tool_choice="auto",
            temperature=0.1
        )
        
        msg = response.choices[0].message
        tool_calls = msg.tool_calls
        
        if not tool_calls:
            content = msg.content
            try:
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0].strip()
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0].strip()
                
                data = json.loads(content)
                
                session_store.update_history(session_id, "user", query)
                session_store.update_history(session_id, "assistant", content)
                
                return {"status": "success", **data}
                
            except json.JSONDecodeError:
                print(f"JSON Parse Error: {content}")
                return {
                    "status": "success",
                    "type": "ERROR",
                    "payload": {"message": "Agent output invalid format."}
                }
        
        messages.append(msg)
        
        for tool_call in tool_calls:
            fn_name = tool_call.function.name
            fn_args = json.loads(tool_call.function.arguments)
            print(f"Tool Call: {fn_name}({fn_args})")
            
            # RouteType Conversion
            if "route_type" in fn_args and isinstance(fn_args["route_type"], str):
                try:
                    r_type_str = fn_args["route_type"].upper()
                    if r_type_str in RouteType.__members__:
                        fn_args["route_type"] = RouteType[r_type_str].value
                        print(f"DEBUG: Converted route_type '{r_type_str}' to {fn_args['route_type']}")
                except Exception as e:
                    print(f"DEBUG: Failed to convert route_type: {e}")

            result_str = ""
            try:
                if fn_name == "get_departures":
                    result_str = await tools.get_departures(**fn_args)
                elif fn_name == "search_stops":
                    result_str = await tools.search_stops(**fn_args)
                elif fn_name == "search_routes":
                    result_str = await tools.search_routes(**fn_args)
                elif fn_name == "get_route_directions":
                    result_str = await tools.get_route_directions(**fn_args)
                elif fn_name == "search_and_get_departures":
                    result_str = await tools.search_and_get_departures(**fn_args)
                elif fn_name == "ask_clarification":
                    # Immediate Return for Clarification
                    return {
                        "status": "success",
                        "type": "CLARIFICATION",
                        "payload": fn_args
                    }
                elif fn_name == "configure_button":
                     result_str = json.dumps({"status": "success", "msg": "Button configured"})
                else:
                    result_str = json.dumps({"error": "Unknown tool"})
            except Exception as e:
                result_str = json.dumps({"error": str(e)})
                
            messages.append({
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": fn_name,
                "content": result_str
            })
            
    return {
        "status": "success",
        "type": "ERROR",
        "payload": {"message": "Agent loop limit reached."}
    }
