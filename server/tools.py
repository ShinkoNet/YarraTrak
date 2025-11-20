from .ptv_client import PTVClient
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

async def get_departures(stop_id: int, route_type: int = 0) -> str:
    """
    Get next departures for a specific stop.
    
    Args:
        stop_id: The ID of the stop (e.g., 1071 for Flinders St).
        route_type: Transport mode (0=Train, 1=Tram, 2=Bus, 3=VLine, 4=Night Bus).
    """
    try:
        # Fetch departures with direction info
        data = await client.get_departures(route_type, stop_id, expand=["Direction"])
        departures = data.get("departures", [])
        directions = data.get("directions", {})
        
        if not departures:
            return "No upcoming departures found."
        
        # Basic formatting
        result = []
        for d in departures[:5]:
            route_id = d.get('route_id')
            direction_id = d.get('direction_id')
            # Get direction name
            dir_name = directions.get(str(direction_id), {}).get('direction_name', 'Unknown Direction')
            
            time_utc = d.get('scheduled_departure_utc')
            platform = d.get('platform_number')
            est_time = d.get('estimated_departure_utc')
            
            raw_time = est_time if est_time else time_utc
            time_str = to_melbourne_time(raw_time)
            
            info = f"Route {route_id} ({dir_name}) @ {time_str}"
            if platform:
                info += f" (Plat {platform})"
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
        allowed_types = [0, 1, 3]
        filtered_stops = [s for s in stops if s.get('route_type') in allowed_types]
        
        if not filtered_stops:
            return f"No train or tram stops found matching '{query}' (Buses hidden)."
            
        # Sort: Train (0) -> VLine (3) -> Tram (1)
        def sort_key(s):
            rt = s.get('route_type')
            if rt == 0: return 0
            if rt == 3: return 1
            return 2
            
        filtered_stops.sort(key=sort_key)
        
        # Type Mapping
        type_names = {
            0: "Metro Train",
            1: "Tram",
            2: "Bus",
            3: "V/Line Train",
            4: "Night Bus"
        }
        
        result = []
        for s in filtered_stops[:15]: # Limit to 15
            name = s.get('stop_name')
            sid = s.get('stop_id')
            rtype = s.get('route_type')
            type_str = type_names.get(rtype, f"Type {rtype}")
            
            result.append(f"{name} ({type_str}) [ID: {sid}]")
            
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
            0: "Metro Train",
            1: "Tram",
            2: "Bus",
            3: "V/Line Train",
            4: "Night Bus"
        }
        
        result = []
        for r in routes[:15]:
            name = r.get('route_name')
            rid = r.get('route_id')
            rtype = r.get('route_type')
            type_str = type_names.get(rtype, f"Type {rtype}")
            
            result.append(f"{name} ({type_str}) [ID: {rid}]")
            
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
