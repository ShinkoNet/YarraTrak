import re
import httpx
from .ptv_client import PTVClient
from .enums import RouteType
from datetime import datetime
from zoneinfo import ZoneInfo

# Initialize PTV Client
client = PTVClient()
_station_db_cache: dict[int, list[dict]] = {}
_route_direction_stop_cache: dict[tuple[int, int, int], list[dict]] = {}


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
            
            # Calculate minutes until departure (for LLM to use directly)
            minutes_until = None
            if raw_time:
                try:
                    dep_dt = datetime.fromisoformat(raw_time.replace('Z', '+00:00'))
                    now_utc = datetime.now(ZoneInfo("UTC"))
                    minutes_until = max(0, int((dep_dt - now_utc).total_seconds() / 60))
                except Exception:
                    pass
            
            # Format time for display (Melbourne local, HH:MM only)
            time_display = "Unknown"
            if raw_time:
                try:
                    dep_dt = datetime.fromisoformat(raw_time.replace('Z', '+00:00'))
                    local_dt = dep_dt.astimezone(ZoneInfo("Australia/Melbourne"))
                    time_display = local_dt.strftime("%H:%M")
                except Exception:
                    time_display = raw_time
            
            # Construct Output with pre-calculated minutes for LLM
            # Format: [Route] towards [Direction]. Departs: HH:MM (in X min), Platform Y
            info = f"- {route_display} towards {dir_name}"
            
            if destination_name:
                 info += f", terminating at {destination_name}"
            
            info += f". Departs: {time_display}"
            if minutes_until is not None:
                info += f" (in {minutes_until} min)"
            
            if platform:
                info += f", Platform {platform}"
            
            if dir_desc:
                info += f" [{dir_desc}]"

            result.append(info)

        # Always emit a [DISRUPTIONS:...] block — even when empty — so the
        # agent has explicit signal when the user asks "is the line down?".
        # Without this, the agent infers from departure cadence and may
        # invent a "running normally" answer that contradicts reality.
        shown_route_ids = set(d.get('route_id') for d in departures[:15])
        relevant_disruptions: list[str] = []
        if disruptions:
            for d_id, d in disruptions.items():
                if d.get('disruption_status') != 'Current':
                    continue
                affected_routes = set(r.get('route_id') for r in d.get('routes', []))
                if not affected_routes.intersection(shown_route_ids):
                    continue
                title = d.get('title', '').lower()
                if 'terminate' in title or 'replace' in title or 'delay' in title:
                    relevant_disruptions.append(d.get('title', '') or '')

        result.append("")
        if relevant_disruptions:
            result.append("[DISRUPTIONS: " + " | ".join(relevant_disruptions[:3]) + "]")
        else:
            result.append("[DISRUPTIONS: None reported on shown routes]")

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


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Calculate Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    
    if len(s2) == 0:
        return len(s1)
    
    prev_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row
    
    return prev_row[-1]


def _fuzzy_match_station(query: str, stations: list[dict], max_suggestions: int = 5) -> list[tuple[dict, int]]:
    """
    Find stations matching query using fuzzy matching.
    Returns list of (station, score) tuples, sorted by match quality.
    Score 0 = exact match, higher = worse match.
    """
    query_lower = query.lower().strip()
    query_base = query_lower.replace(" station", "").replace(" stop", "").strip()
    query_words = query_base.split()
    is_single_word = len(query_words) == 1
    
    scored = []
    for s in stations:
        name = s.get("name", "")
        name_lower = name.lower()
        name_base = name_lower.replace(" station", "").replace(" stop", "").strip()
        
        # Exact match
        if name_base == query_base or query_lower == name_lower:
            scored.append((s, 0))
            continue
        
        # Query is substring (e.g., "Flinders" in "Flinders Street Station")
        if query_base in name_lower:
            scored.append((s, 1))
            continue
        
        # Starts with query
        if name_base.startswith(query_base):
            scored.append((s, 2))
            continue
        
        # Fuzzy match using Levenshtein distance
        distance = _levenshtein_distance(query_base, name_base)
        
        # For single-word queries, also try matching first word of station name
        # (e.g., "Flindas" -> "Flinders" in "Flinders Street")
        if is_single_word:
            name_first = name_base.split()[0] if name_base else name_base
            distance_first = _levenshtein_distance(query_base, name_first)
            distance = min(distance, distance_first)
        
        # Only consider if distance is reasonable (< 40% of query length, min 3)
        max_distance = max(3, len(query_base) * 0.4)
        if distance <= max_distance:
            scored.append((s, 10 + distance))
    
    # Sort by score (lower = better)
    scored.sort(key=lambda x: x[1])
    return scored[:max_suggestions]


def _load_station_db(route_type: int) -> list[dict]:
    """Load the appropriate station database for the route type."""
    import json
    import os

    cached = _station_db_cache.get(route_type)
    if cached is not None:
        return cached

    base_dir = os.path.dirname(__file__)

    if route_type == RouteType.TRAIN:
        filename = "stations_train.json"
    elif route_type == RouteType.VLINE:
        filename = "stations_vline.json"
    elif route_type == RouteType.TRAM:
        filename = "stops_tram.json"
    else:
        return []

    filepath = os.path.join(base_dir, filename)
    if not os.path.exists(filepath):
        return []

    try:
        with open(filepath, "r") as f:
            data = json.load(f)
        stops = data.get("stops", [])
        _station_db_cache[route_type] = stops
        return stops
    except Exception:
        return []


def get_route_direction_stops(
    route_id: int,
    direction_id: int,
    route_type: int = RouteType.TRAIN,
) -> list[dict]:
    """Return ordered stops for a specific route/direction with stop sequence metadata."""
    cache_key = (route_type, route_id, direction_id)
    cached = _route_direction_stop_cache.get(cache_key)
    if cached is not None:
        return cached

    stops = []
    for stop in _load_station_db(route_type):
        route_info = (stop.get("routes") or {}).get(str(route_id))
        if not route_info:
            continue
        dir_info = (route_info.get("dirs") or {}).get(str(direction_id))
        if not dir_info:
            continue
        seq = dir_info.get("seq")
        if seq is None:
            continue
        stops.append({
            "stop_id": stop.get("stop_id"),
            "stop_name": stop.get("name", ""),
            "seq": seq,
        })

    stops.sort(key=lambda item: (item["seq"], item["stop_name"]))
    _route_direction_stop_cache[cache_key] = stops
    return stops


def resolve_trip_patterns(
    start_stop_id: int,
    dest_stop_id: int,
    route_type: int = RouteType.TRAIN
) -> list[dict]:
    """
    Resolve all valid route/direction pairs for traveling from start to destination.

    Returns a list of:
    {route_id, route_name, direction_id, direction_name}
    """
    stations = _load_station_db(route_type)
    if not stations:
        return []

    start_station = next((s for s in stations if s.get("stop_id") == start_stop_id), None)
    dest_station = next((s for s in stations if s.get("stop_id") == dest_stop_id), None)

    if not start_station or not dest_station:
        return []

    start_routes = start_station.get("routes", {})
    dest_routes = dest_station.get("routes", {})

    if not start_routes or not dest_routes:
        return []

    patterns = []
    for route_id, route_info in start_routes.items():
        dest_route_info = dest_routes.get(route_id)
        if not dest_route_info:
            continue

        dirs = route_info.get("dirs", {})
        dest_dirs = dest_route_info.get("dirs", {})
        for dir_id, dir_info in dirs.items():
            start_seq = dir_info.get("seq")
            dest_seq = dest_dirs.get(dir_id, {}).get("seq")

            if start_seq is not None and dest_seq is not None and start_seq < dest_seq:
                patterns.append({
                    "route_id": int(route_id),
                    "route_name": route_info.get("name", ""),
                    "direction_id": int(dir_id),
                    "direction_name": dir_info.get("name", "")
                })

    patterns.sort(key=lambda p: (p["route_id"], p["direction_id"]))
    return patterns



def resolve_direction_id(
    start_stop_id: int,
    dest_stop_id: int,
    route_type: int = RouteType.TRAIN
) -> dict:
    """
    Resolve the direction_id for traveling from start to destination.
    Uses the enhanced station database with route/direction/stop_sequence data.
    
    Returns: {direction_id: int, direction_name: str, route_id: int, route_name: str} or {}
    """
    patterns = resolve_trip_patterns(start_stop_id, dest_stop_id, route_type)
    if not patterns:
        return {}
    return patterns[0]


async def configure_pebble_button(
    button_id: int,
    stop_name: str,
    stop_id: int,
    route_type: int = RouteType.TRAIN,
    dest_stop_id: int | None = None,
    dest_name: str | None = None,
    direction_id: int | None = None,
    direction_name: str | None = None
) -> str:
    """
    Generate configuration for a Pebble favourite button.
    Returns structured data that will be pushed to the connected Pebble client.
    
    Args:
        button_id: Which button to configure (1, 2, or 3).
        stop_name: Human-readable name for the button (start station).
        stop_id: The PTV stop ID for the start station.
        route_type: Transport mode (TRAIN=0, TRAM=1, BUS=2, VLINE=3).
        dest_stop_id: Optional destination stop ID (for direction filtering).
        dest_name: Optional destination name.
        direction_id: Optional explicit direction ID (overrides auto-resolution).
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
    
    # Auto-resolve direction if dest provided but direction not
    if dest_stop_id and not direction_id:
        resolved = resolve_direction_id(stop_id, dest_stop_id, route_type)
        if resolved:
            direction_id = resolved.get("direction_id")
            direction_name = resolved.get("direction_name")
    
    # Structured config that will be extracted and pushed to client
    config = {
        "button_id": button_id,
        "name": stop_name,
        "stop_id": stop_id,
        "route_type": route_type,
        "dest_id": dest_stop_id,
        "dest_name": dest_name,
        "direction_id": direction_id,
        "direction_name": direction_name
    }
    
    direction_text = f"towards {direction_name}" if direction_name else "any direction"
    dest_text = f" → {dest_name}" if dest_name else ""
    
    # Return both human-readable text AND structured data marker
    return f"""[BUTTON_CONFIG:{json.dumps(config)}]

Button {button_id} configured: {stop_name}{dest_text} ({rt_name}, {direction_text}).
Direction ID resolved: {direction_id}.
Your Pebble app will be updated automatically."""


# "city" / "cbd" / "downtown" reach the agent via dictation but the local
# fuzzy matcher returns nonsense ("Crib Point Station") because none of them
# substring-match a real station name. Resolve the common shorthand to
# Flinders Street before matching so the happy path doesn't bottom out on
# an alias miss.
_STATION_NAME_ALIASES = {
    "city": "Flinders Street",
    "the city": "Flinders Street",
    "cbd": "Flinders Street",
    "cdb": "Flinders Street",
    "downtown": "Flinders Street",
    "central": "Flinders Street",
    "city centre": "Flinders Street",
    "city center": "Flinders Street",
}


def _resolve_station_alias(name: str) -> str:
    if not isinstance(name, str):
        return name
    key = name.lower().strip()
    return _STATION_NAME_ALIASES.get(key, name)


async def setup_favourite_entry(
    entry_id: int,
    start_station: str,
    destination: str,
    route_type: str = "TRAIN",
    current_entries: int | None = None,
) -> str:
    """
    Smart favourite-entry setup. Just provide station NAMES - all IDs resolved automatically.
    Returns actionable guidance on failure to help the agent recover.

    Args:
        entry_id: Which entry slot (1-10). Users may call this entry / button / favourite / saved stop / slot.
        start_station: Name of the START station (e.g., "Narre Warren")
        destination: Name of the DESTINATION station (e.g., "Flinders Street")
        route_type: "TRAIN", "TRAM", or "VLINE"
        current_entries: How many entry slots the user currently has filled (0-10).
            Injected by the server. None means "unknown — proceed without slot guarding".
    """
    import json

    # --- Slot validation ---
    # Hard bound: storage only has slots 1-10.
    if not isinstance(entry_id, int) or entry_id < 1 or entry_id > 10:
        return f"""[ERROR] entry_id {entry_id} is out of range. Valid slots are 1-10.
ACTION: Call return_error to tell the user: "Favourite entries are numbered 1 to 10. Please pick a slot in that range." """

    # Gap guard: refuse picks that would leave empty slots between filled ones
    # (e.g. user asks for slot 7 with only 3 filled would leave slots 4-6 empty).
    # Overwriting an *existing* slot is fine — trust the user's explicit pick;
    # the watch UI handles the "are you sure?" confirmation. Skip when
    # current_entries is unknown (legacy callers, HTTP entry-point, tests).
    if (
        current_entries is not None
        and 0 <= current_entries <= 10
        and entry_id > current_entries + 1
    ):
        next_slot = current_entries + 1
        overwrite_options = "\n".join(
            f'  - label "Overwrite entry #{i}" value "{i}"'
            for i in range(1, min(current_entries, 3) + 1)
        )
        return f"""[ERROR] User asked for entry {entry_id} but only {current_entries} are filled — picking {entry_id} would leave slots {next_slot}..{entry_id - 1} empty.
ACTION: Call ask_clarification with question "You only have {current_entries} favourite entries set up. Should I add this as entry #{next_slot}, or overwrite an existing entry?".
After the user picks a slot, call setup_favourite_entry AGAIN with the user's chosen entry_id, and the SAME start_station="{start_station}" and destination="{destination}" you used this time.
Options:
  - label "Add as entry #{next_slot}" value "{next_slot}"
{overwrite_options}"""

    # Resolve dictated/casual aliases ("city" -> "Flinders Street") before
    # the fuzzy match, otherwise the matcher returns wildly wrong suggestions
    # and the agent bottoms out.
    start_station = _resolve_station_alias(start_station)
    destination = _resolve_station_alias(destination)

    # Convert route type string to int
    rt_map = {"TRAIN": RouteType.TRAIN, "TRAM": RouteType.TRAM, "VLINE": RouteType.VLINE, "BUS": RouteType.BUS}
    rt = rt_map.get(route_type.upper(), RouteType.TRAIN)
    
    # Load station database
    stations = _load_station_db(rt)
    if not stations:
        return f"""[ERROR] No station database found for {route_type}.
ACTION: Tell the user there's a system configuration issue."""

    # --- Find START station using fuzzy matching ---
    start_matches = _fuzzy_match_station(start_station, stations)
    
    if not start_matches:
        return f"""[ERROR] Could not find start station "{start_station}". No similar matches found.
ACTION: Call ask_clarification to ask the user to spell the station name differently."""
    
    # Check if we got an exact/substring match (score < 10) vs fuzzy match (score >= 10)
    best_score = start_matches[0][1]
    if best_score >= 10:
        # Fuzzy match only - ask for confirmation
        suggestions = [s.get("name") for s, _ in start_matches]
        return f"""[ERROR] Could not find exact match for start station "{start_station}".
SIMILAR STATIONS: {', '.join(suggestions)}
ACTION: Call ask_clarification to ask: "I couldn't find '{start_station}'. Did you mean one of these: {', '.join(suggestions)}?" with options for each."""
    
    # Got a good match
    start = start_matches[0][0]
    start_id = start.get("stop_id")
    start_name = start.get("name")
    start_routes = start.get("routes", {})

    # --- Find DESTINATION station using fuzzy matching ---
    dest_matches = _fuzzy_match_station(destination, stations)
    
    if not dest_matches:
        return f"""[ERROR] Could not find destination station "{destination}". No similar matches found.
ACTION: Call ask_clarification to ask the user to spell the station name differently."""
    
    best_score = dest_matches[0][1]
    if best_score >= 10:
        suggestions = [s.get("name") for s, _ in dest_matches]
        return f"""[ERROR] Could not find exact match for destination "{destination}".
SIMILAR STATIONS: {', '.join(suggestions)}
ACTION: Call ask_clarification to ask: "I couldn't find '{destination}'. Did you mean one of these: {', '.join(suggestions)}?" with options for each."""

    dest = dest_matches[0][0]
    dest_id = dest.get("stop_id")
    dest_name = dest.get("name")
    dest_routes = dest.get("routes", {})

    # --- Check if stations share a route ---
    shared_route = None
    for route_id in start_routes:
        if route_id in dest_routes:
            shared_route = route_id
            break
    
    if not shared_route:
        # List the lines each station is on
        start_lines = [r.get("name", "") for r in start_routes.values()]
        dest_lines = [r.get("name", "") for r in dest_routes.values()]
        
        return f"""[ERROR] {start_name} and {dest_name} are NOT on the same line!
{start_name} is on: {', '.join(start_lines) or 'unknown'}
{dest_name} is on: {', '.join(dest_lines) or 'unknown'}
ACTION: Call return_error to tell the user: "Sorry, {start_name} and {dest_name} aren't on the same train line. You can only set up a button for stations on the same line. What line do you want to use?" """

    # --- Resolve direction ---
    route_info = start_routes[shared_route]
    route_name = route_info.get("name", "")
    dirs = route_info.get("dirs", {})
    
    direction_id = None
    direction_name = None
    
    for dir_id, dir_info in dirs.items():
        start_seq = dir_info.get("seq")
        dest_dir_info = dest_routes.get(shared_route, {}).get("dirs", {}).get(dir_id, {})
        dest_seq = dest_dir_info.get("seq")
        
        if start_seq is not None and dest_seq is not None and start_seq < dest_seq:
            direction_id = int(dir_id)
            direction_name = dir_info.get("name", "")
            break
    
    if direction_id is None:
        return f"""[ERROR] Could not determine direction from {start_name} to {dest_name}.
This might mean {dest_name} comes BEFORE {start_name} on the {route_name} line.
ACTION: Call ask_clarification to ask: "Do you want trains FROM {start_name} or TO {start_name}?" with options."""

    # --- Success! Build config ---
    config = {
        "entry_id": entry_id,
        "name": start_name,
        "stop_id": start_id,
        "route_type": rt,
        "dest_id": dest_id,
        "dest_name": dest_name,
        "direction_id": direction_id,
        "direction_name": direction_name
    }

    return f"""[ENTRY_CONFIG:{json.dumps(config)}]

SUCCESS! Entry {entry_id} configured:
  From: {start_name}
  To: {dest_name}
  Line: {route_name}
  Direction: {direction_name} (ID: {direction_id})

The Pebble app will be updated automatically."""


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
    Falls back to local fuzzy matching if PTV API returns nothing (ASR error recovery).

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

        # 1. Search for the stop via PTV API
        data = await client.search(query)
        stops = data.get("stops", [])

        # Filter by route_type
        filtered_stops = [s for s in stops if s.get('route_type') == route_type]

        # 2. If PTV API returns nothing, try local fuzzy matching (ASR recovery)
        if not filtered_stops:
            type_name = {0: "train", 1: "tram", 3: "V/Line"}.get(route_type, "transport")
            local_stations = _load_station_db(route_type)
            
            if local_stations:
                fuzzy_matches = _fuzzy_match_station(query, local_stations, max_suggestions=5)
                if fuzzy_matches:
                    best_score = fuzzy_matches[0][1]
                    if best_score < 10:
                        # Good local match - use it directly
                        station = fuzzy_matches[0][0]
                        stop_id = station.get("stop_id")
                        stop_name = station.get("name")
                        
                        departures_output = await get_departures(stop_id, route_type)
                        stop_info_line = f"[STOP_INFO:{stop_id}:{route_type}:{stop_name}]"
                        return f"Found stop '{stop_name}' (ID: {stop_id}).\n{stop_info_line}\n\n{departures_output}"
                    else:
                        # Fuzzy match needs confirmation
                        suggestions = [f"{s.get('name')} [ID: {s.get('stop_id')}]" for s, _ in fuzzy_matches]
                        return f"""[ERROR] No {type_name} stops found exactly matching '{query}'.
SIMILAR STATIONS: {', '.join([s.get('name') for s, _ in fuzzy_matches])}
ACTION: Call ask_clarification to ask: "I couldn't find '{query}'. Did you mean {fuzzy_matches[0][0].get('name')}?" with options for the similar stations."""
            
            # No fuzzy matches at all
            return f"""[ERROR] No {type_name} stops found matching '{query}'.
ACTION: Call ask_clarification to ask the user to spell or say the station name differently."""

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
