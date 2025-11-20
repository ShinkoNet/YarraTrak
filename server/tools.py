from .ptv_client import PTVClient
from .enums import RouteType
from datetime import datetime
from zoneinfo import ZoneInfo

# Initialize PTV Client
client = PTVClient()

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
        # Fetch departures with direction, route, stop, and run info
        data = await client.get_departures(route_type, stop_id, expand=["Direction", "Route", "Stop", "Run"])
        departures = data.get("departures", [])
        directions = data.get("directions", {})
        routes = data.get("routes", {})
        stops = data.get("stops", {})
        runs = data.get("runs", {})
        
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
        
        for d in departures[:5]:
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
            
        return "\n".join(result)
    except Exception as e:
        return f"Error fetching departures: {str(e)}"

async def search_stops(query: str) -> str:
    """
    Search for public transport stops by name.
    Filters for Trains (Metro/VLine) and Trams. Excludes buses.
    
    Args:
        query: The name of the stop to search for (e.g., "Flinders").
    """
    try:
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
    except Exception as e:
        return f"Error searching stops: {str(e)}"

async def search_routes(query: str) -> str:
    """
    Search for public transport routes by name.
    """
    try:
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
    except Exception as e:
        return f"Error searching routes: {str(e)}"

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
    except Exception as e:
        return f"Error fetching directions: {str(e)}"

async def search_and_get_departures(query: str, route_type: int = RouteType.TRAIN) -> str:
    """
    Search for a stop and immediately get departures for the first result.
    Useful when the user specifies a clear stop name.
    
    Args:
        query: The name of the stop to search for.
        route_type: Transport mode enums (TRAIN, TRAM, BUS, VLINE, NIGHT_BUS). Defaults to TRAIN.
    """
    try:
        # 1. Search for the stop
        data = await client.search(query)
        stops = data.get("stops", [])
        
        if not stops:
            return f"No stops found matching '{query}'."
            
        # Filter by route_type
        filtered_stops = [s for s in stops if s.get('route_type') == route_type]
        
        if not filtered_stops:
            return f"No stops of type {route_type} found matching '{query}'."
            
        # Pick the first result
        best_match = filtered_stops[0]
        stop_id = best_match.get('stop_id')
        stop_name = best_match.get('stop_name')
        
        # 2. Get departures for this stop
        # We reuse the existing get_departures logic by calling it directly
        # Note: get_departures is async, so we await it
        departures_output = await get_departures(stop_id, route_type)
        
        return f"Found stop '{stop_name}' (ID: {stop_id}).\n\n{departures_output}"
        
    except Exception as e:
        return f"Error in search_and_get_departures: {str(e)}"
