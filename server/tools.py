import re
import httpx
from .ptv_client import PTVClient
from .enums import RouteType
from datetime import datetime
from zoneinfo import ZoneInfo

# Initialize PTV Client
client = PTVClient()


def sanitize_query(query: str) -> str:
    """Sanitize search query - remove/replace problematic characters."""
    # Remove periods (St. Kilda -> St Kilda)
    query = query.replace('.', ' ')
    # Replace curly apostrophes with straight ones, then remove all apostrophes
    query = query.replace(''', "'").replace(''', "'")
    query = query.replace("'", "")
    # Collapse multiple spaces
    query = re.sub(r'\s+', ' ', query).strip()
    return query

def to_melbourne_time(utc_str: str) -> str:
    """Converts UTC ISO string to Melbourne time ISO string."""
    if not utc_str:
        return "Unknown"
    try:
        # Parse UTC (handle Z)
        dt = datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
        # Convert to Melbourne
        local_dt = dt.astimezone(ZoneInfo("Australia/Melbourne"))
        return local_dt.isoformat()
    except Exception:
        return utc_str

async def get_departures(stop_id: int, route_type: int = RouteType.TRAIN) -> str:
    """
    Get next departures for a specific stop.
    
    Args:
        stop_id: The ID of the stop (e.g., 1071 for Flinders St).
        route_type: Transport mode enums (TRAIN, TRAM, BUS, VLINE, NIGHT_BUS). Defaults to TRAIN.
    """
    try:
        # Fetch departures with direction, route, stop, run and disruption info
        data = await client.get_departures(route_type, stop_id, expand=["Direction", "Route", "Stop", "Run", "Disruption"])
        departures = data.get("departures", [])
        directions = data.get("directions", {})
        routes = data.get("routes", {})
        stops = data.get("stops", {})
        runs = data.get("runs", {})
        disruptions = data.get("disruptions", {})
        
        if not departures:
            return "No upcoming departures found."
        
        # Get stop name
        stop_info = stops.get(str(stop_id), {})
        stop_name = stop_info.get('stop_name', f"Stop {stop_id}")
        
        # Determine stop type suffix
        type_suffixes = {
            RouteType.TRAIN: "Metro Station",
            RouteType.TRAM: "Tram Stop",
            RouteType.BUS: "Bus Stop",
            RouteType.VLINE: "V/Line Station",
            RouteType.NIGHT_BUS: "Night Bus Stop"
        }
        suffix = type_suffixes.get(route_type, "Stop")
        
        result = [f"Departures for {stop_name} ({suffix}):"]

        # Sort by departure time and show more results to cover different lines
        departures.sort(key=lambda x: x.get('scheduled_departure_utc') or '')

        for d in departures[:15]:
            route_id = d.get('route_id')
            direction_id = d.get('direction_id')
            run_ref = d.get('run_ref')
            
            # Resolve Route Name
            route_info = routes.get(str(route_id), {})
            route_name = route_info.get('route_name', 'Unknown Route')
            route_num = route_info.get('route_number')
            
            if route_type in [RouteType.TRAIN, RouteType.VLINE] and "Line" not in route_name: # Train or V/Line
                route_display = f"{route_name} Line"
            elif route_num:
                route_display = f"Route {route_num}"
            else:
                route_display = route_name
            
            # Resolve Direction
            dir_info = directions.get(str(direction_id), {})
            dir_name = dir_info.get('direction_name', 'Unknown Direction')
            dir_desc = dir_info.get('route_direction_description', '')
            
            # Resolve Destination (Run info)
            run_info = runs.get(str(run_ref)) or runs.get(str(d.get('run_id'))) or {}
            destination_name = run_info.get('destination_name')
            
            # Time and Platform
            time_utc = d.get('scheduled_departure_utc')
            platform = d.get('platform_number')
            est_time = d.get('estimated_departure_utc')
            
            raw_time = est_time if est_time else time_utc
            time_str = to_melbourne_time(raw_time)
            
            # Construct Output
            # Format: [Route] towards [Direction], terminating at [Destination]. Departs: [Time] (Platform [X])
            info = f"- {route_display} towards {dir_name}"
            
            if destination_name:
                 info += f", terminating at {destination_name}"
            
            info += f". Departs: {time_str}"
            
            if platform:
                info += f" (Platform {platform})"
            
            if dir_desc:
                info += f" [{dir_desc}]"

            result.append(info)

        # Add disruptions only for routes shown in departures
        if disruptions:
            # Get route IDs from shown departures
            shown_route_ids = set(d.get('route_id') for d in departures[:15])

            relevant_disruptions = []
            for d_id, d in disruptions.items():
                if d.get('disruption_status') != 'Current':
                    continue

                # Check if disruption affects any shown route
                affected_routes = set(r.get('route_id') for r in d.get('routes', []))
                if not affected_routes.intersection(shown_route_ids):
                    continue

                title = d.get('title', '').lower()
                # Only show service-affecting disruptions
                if 'terminate' in title or 'replace' in title or 'delay' in title:
                    relevant_disruptions.append(f"[!] {d.get('title', '')}")

            if relevant_disruptions:
                result.append("")
                result.append("DISRUPTIONS:")
                result.extend(relevant_disruptions[:3])

        return "\n".join(result)
    except httpx.HTTPStatusError as e:
        return f"[API_ERROR] PTV API returned status {e.response.status_code}: {str(e)}"
    except httpx.RequestError as e:
        return f"[API_ERROR] Failed to connect to PTV API: {str(e)}"
    except Exception as e:
        return f"[MCP_ERROR] Error processing departures: {str(e)}"

async def search_stops(query: str) -> str:
    """
    Search for public transport stops by name.
    Filters for Trains (Metro/VLine) and Trams. Excludes buses.

    Args:
        query: The name of the stop to search for (e.g., "Flinders").
    """
    try:
        query = sanitize_query(query)
        data = await client.search(query)
        stops = data.get("stops", [])
        
        if not stops:
            return f"No stops found matching '{query}'."
        
        # Filter: Keep only Train (0), Tram (1), VLine (3)
        allowed_types = [RouteType.TRAIN, RouteType.TRAM, RouteType.VLINE]
        filtered_stops = [s for s in stops if s.get('route_type') in allowed_types]
        
        if not filtered_stops:
            return f"No train or tram stops found matching '{query}' (Buses hidden)."
            
        # Sort: Train (0) -> VLine (3) -> Tram (1)
        def sort_key(s):
            rt = s.get('route_type')
            if rt == RouteType.TRAIN: return 0
            if rt == RouteType.VLINE: return 1
            return 2
            
        filtered_stops.sort(key=sort_key)
        
        # Type Mapping
        type_names = {
            RouteType.TRAIN: "Metro Train",
            RouteType.TRAM: "Tram",
            RouteType.BUS: "Bus",
            RouteType.VLINE: "V/Line Train",
            RouteType.NIGHT_BUS: "Night Bus"
        }
        
        result = []
        for s in filtered_stops[:15]: # Limit to 15
            name = s.get('stop_name')
            sid = s.get('stop_id')
            rtype = s.get('route_type')
            type_str = type_names.get(rtype, f"Type {rtype}")
            
            result.append(f"{name} ({type_str}, Type {rtype}) [ID: {sid}]")
            
        return "\n".join(result)
    except httpx.HTTPStatusError as e:
        return f"[API_ERROR] PTV API returned status {e.response.status_code}"
    except httpx.RequestError as e:
        return f"[API_ERROR] Failed to connect to PTV API: {str(e)}"
    except Exception as e:
        return f"[MCP_ERROR] Error searching stops: {str(e)}"

async def search_routes(query: str) -> str:
    """
    Search for public transport routes by name.
    """
    try:
        query = sanitize_query(query)
        data = await client.search(query)
        routes = data.get("routes", [])
        
        if not routes:
            return f"No routes found matching '{query}'."
            
        # Type Mapping
        type_names = {
            RouteType.TRAIN: "Metro Train",
            RouteType.TRAM: "Tram",
            RouteType.BUS: "Bus",
            RouteType.VLINE: "V/Line Train",
            RouteType.NIGHT_BUS: "Night Bus"
        }
        
        result = []
        for r in routes[:15]:
            name = r.get('route_name')
            rid = r.get('route_id')
            rtype = r.get('route_type')
            type_str = type_names.get(rtype, f"Type {rtype}")
            
            result.append(f"{name} ({type_str}, Type {rtype}) [ID: {rid}]")
            
        return "\n".join(result)
    except httpx.HTTPStatusError as e:
        return f"[API_ERROR] PTV API returned status {e.response.status_code}"
    except httpx.RequestError as e:
        return f"[API_ERROR] Failed to connect to PTV API: {str(e)}"
    except Exception as e:
        return f"[MCP_ERROR] Error searching routes: {str(e)}"

async def get_route_directions(route_id: int) -> str:
    """
    Get directions for a specific route.
    """
    try:
        data = await client.get_directions(route_id)
        directions = data.get("directions", [])
        
        if not directions:
            return f"No directions found for route {route_id}."
            
        result = []
        for d in directions:
            name = d.get('direction_name')
            did = d.get('direction_id')
            desc = d.get('route_direction_description')
            
            info = f"{name} [ID: {did}]"
            if desc:
                info += f" - {desc}"
            result.append(info)
            
        return "\n".join(result)
    except httpx.HTTPStatusError as e:
        return f"[API_ERROR] PTV API returned status {e.response.status_code}"
    except httpx.RequestError as e:
        return f"[API_ERROR] Failed to connect to PTV API: {str(e)}"
    except Exception as e:
        return f"[MCP_ERROR] Error fetching directions: {str(e)}"


async def configure_pebble_button(
    button_id: int,
    stop_name: str,
    stop_id: int,
    route_type: int = RouteType.TRAIN,
    direction_id: int | None = None,
    direction_name: str | None = None
) -> str:
    """
    Generate configuration for a Pebble stealth button.
    Returns structured data that will be pushed to the connected Pebble client.
    
    Args:
        button_id: Which button to configure (1, 2, or 3).
        stop_name: Human-readable name for the button.
        stop_id: The PTV stop ID.
        route_type: Transport mode (TRAIN=0, TRAM=1, BUS=2, VLINE=3).
        direction_id: Optional direction ID for filtering.
        direction_name: Optional human-readable direction.
    """
    import json
    
    route_type_names = {
        RouteType.TRAIN: "Train",
        RouteType.TRAM: "Tram",
        RouteType.BUS: "Bus",
        RouteType.VLINE: "V/Line",
        RouteType.NIGHT_BUS: "Night Bus"
    }
    rt_name = route_type_names.get(route_type, "Unknown")
    
    # Structured config that will be extracted and pushed to client
    config = {
        "button_id": button_id,
        "name": stop_name,
        "stop_id": stop_id,
        "route_type": route_type,
        "direction_id": direction_id,
        "direction_name": direction_name
    }
    
    direction_text = f"{direction_name}" if direction_name else "Any direction"
    
    # Return both human-readable text AND structured data marker
    return f"""[BUTTON_CONFIG:{json.dumps(config)}]

Button {button_id} configured for {stop_name} ({rt_name}, {direction_text}).
Your Pebble app will be updated automatically."""


# Track repeated searches within a session to detect when model is struggling
_search_cache: dict[str, set[str]] = {}  # session_id -> set of queries


def _rank_stop(stop: dict, query: str) -> int:
    """Rank a stop by how well it matches the query. Lower = better."""
    name = stop.get('stop_name', '').lower()
    query_lower = query.lower().strip()

    # Exact match: "Richmond" matches "Richmond Station" or "Richmond"
    name_base = name.replace(' station', '').strip()
    if name_base == query_lower:
        return 0
    # Starts with query as a word: "Richmond Station" for "Richmond"
    if name.startswith(query_lower + ' ') or name.startswith(query_lower + '/'):
        return 1
    # Contains query as a word
    if f' {query_lower} ' in f' {name} ':
        return 2
    # Starts with query
    if name.startswith(query_lower):
        return 3
    # Contains query anywhere
    if query_lower in name:
        return 4
    return 5


def clear_search_cache(session_id: str) -> None:
    """Clear search cache for a session (call when session ends or on new user message)."""
    _search_cache.pop(session_id, None)


async def search_and_get_departures(query: str, route_type: int = RouteType.TRAIN, session_id: str = "") -> str:
    """
    Search for a stop and get departures. Uses smart ranking to pick the best match.
    If called repeatedly with the same query, returns multiple options.

    Args:
        query: The name of the stop to search for.
        route_type: Transport mode enums (TRAIN, TRAM, BUS, VLINE, NIGHT_BUS). Defaults to TRAIN.
        session_id: Optional session ID to track repeated searches.
    """
    try:
        query = sanitize_query(query)
        cache_key = f"{query.lower()}:{route_type}"

        # Check if this is a repeated search
        is_repeat = False
        if session_id:
            if session_id not in _search_cache:
                _search_cache[session_id] = set()
            if cache_key in _search_cache[session_id]:
                is_repeat = True
            _search_cache[session_id].add(cache_key)

        # 1. Search for the stop
        data = await client.search(query)
        stops = data.get("stops", [])

        if not stops:
            return f"No stops found matching '{query}'."

        # Filter by route_type
        filtered_stops = [s for s in stops if s.get('route_type') == route_type]

        if not filtered_stops:
            return f"No stops of type {route_type} found matching '{query}'."

        # Rank stops by match quality
        filtered_stops.sort(key=lambda s: _rank_stop(s, query))

        best_match = filtered_stops[0]
        best_rank = _rank_stop(best_match, query)

        # If repeated search OR no clear best match, return options
        if is_repeat or (len(filtered_stops) > 1 and best_rank > 1):
            result = [f"Multiple stops match '{query}'. Use get_departures with the correct stop_id:"]
            for s in filtered_stops[:6]:
                result.append(f"- {s.get('stop_name')} [ID: {s.get('stop_id')}]")
            return "\n".join(result)

        # Clear best match - get departures directly
        stop_id = best_match.get('stop_id')
        stop_name = best_match.get('stop_name')

        departures_output = await get_departures(stop_id, route_type)

        # Include stop info for client to learn (will be extracted by API)
        stop_info_line = f"[STOP_INFO:{stop_id}:{route_type}:{stop_name}]"

        return f"Found stop '{stop_name}' (ID: {stop_id}).\n{stop_info_line}\n\n{departures_output}"

    except httpx.HTTPStatusError as e:
        return f"[API_ERROR] PTV API returned status {e.response.status_code}"
    except httpx.RequestError as e:
        return f"[API_ERROR] Failed to connect to PTV API: {str(e)}"
    except Exception as e:
        return f"[MCP_ERROR] Error in search_and_get_departures: {str(e)}"


async def speculative_fetch(query_history: list[dict], max_stops: int = 3) -> list[dict]:
    """
    Pre-fetch departures for stops from client-provided history.
    Called in parallel with ASR to reduce latency.

    Args:
        query_history: List of {stop_id, stop_name, route_type} from client localStorage

    Returns list of {stop_name, route_type_name, departures_text} for injection into prompt.
    """
    import asyncio

    if not query_history:
        return []

    async def fetch_one(query_info: dict) -> dict | None:
        try:
            stop_id = query_info["stop_id"]
            stop_name = query_info["stop_name"]
            route_type = query_info["route_type"]

            departures_text = await get_departures(stop_id, route_type)

            if "[API_ERROR]" in departures_text or "[MCP_ERROR]" in departures_text:
                return None

            route_type_name = {
                RouteType.TRAIN: "train",
                RouteType.TRAM: "tram",
                RouteType.BUS: "bus",
                RouteType.VLINE: "V/Line",
                RouteType.NIGHT_BUS: "night bus"
            }.get(route_type, "transport")

            return {
                "stop_name": stop_name,
                "route_type_name": route_type_name,
                "departures_text": departures_text
            }
        except Exception:
            return None

    tasks = [fetch_one(h) for h in query_history[:max_stops]]
    results = await asyncio.gather(*tasks)

    return [r for r in results if r is not None]


def format_speculative_context(prefetched: list[dict]) -> str:
    """Format pre-fetched departures for injection into the system prompt."""
    if not prefetched:
        return ""

    lines = ["PRE-FETCHED DEPARTURES (stops this user has asked about before):"]
    for item in prefetched:
        lines.append(f"\n### {item['stop_name']} ({item['route_type_name']})")
        lines.append(item['departures_text'])

    return "\n".join(lines)
