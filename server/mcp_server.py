from mcp.server.fastmcp import FastMCP
from . import tools
from .enums import RouteType

# Initialize FastMCP
mcp = FastMCP("ptv")

@mcp.tool()
async def get_departures(stop_id: int, route_type: int = RouteType.TRAIN) -> str:
    """
    Get next departures for a specific stop.
    
    Args:
        stop_id: The ID of the stop (e.g., 1071 for Flinders St).
        route_type: Transport mode enums (TRAIN, TRAM, BUS, VLINE, NIGHT_BUS). Defaults to TRAIN.
    """
    return await tools.get_departures(stop_id, route_type)

@mcp.tool()
async def search_stops(query: str) -> str:
    """
    Search for public transport stops by name.
    Filters for Trains (Metro/VLine) and Trams. Excludes buses.
    
    Args:
        query: The name of the stop to search for (e.g., "Flinders").
    """
    return await tools.search_stops(query)

@mcp.tool()
async def search_routes(query: str) -> str:
    """
    Search for public transport routes by name.
    
    Args:
        query: The name of the route to search for (e.g., "Belgrave").
    """
    return await tools.search_routes(query)

@mcp.tool()
async def get_route_directions(route_id: int) -> str:
    """
    Get directions for a specific route.
    
    Args:
        route_id: The ID of the route.
    """
    return await tools.get_route_directions(route_id)

@mcp.tool()
async def search_and_get_departures(query: str, route_type: int = RouteType.TRAIN) -> str:
    """
    Search for a stop and immediately get departures for the first result.
    Useful when the user specifies a clear stop name.
    
    Args:
        query: The name of the stop to search for (e.g., "Flinders").
        route_type: Transport mode enums (TRAIN, TRAM, BUS, VLINE, NIGHT_BUS). Defaults to TRAIN.
    """
    return await tools.search_and_get_departures(query, route_type)

@mcp.tool()
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
    Use this when the user wants to set up a quick-access button on their watch.
    Returns the configuration values the user needs to enter in their Pebble settings.
    
    Args:
        button_id: Which button to configure (1, 2, or 3).
        stop_name: Human-readable name for the button (e.g., "Richmond → City").
        stop_id: The PTV stop ID (from search_stops).
        route_type: Transport mode (TRAIN=0, TRAM=1, BUS=2, VLINE=3). Defaults to TRAIN.
        direction_id: Optional direction ID for filtering (from get_route_directions).
        direction_name: Optional human-readable direction (e.g., "City (Flinders Street)").
    """
    config = {
        "button_id": button_id,
        "name": stop_name,
        "stop_id": stop_id,
        "route_type": route_type,
    }
    if direction_id is not None:
        config["direction_id"] = direction_id
    if direction_name:
        config["direction_name"] = direction_name
    
    import json
    # Include [BUTTON_CONFIG:{...}] marker so server pushes config to connected clients
    config_json = json.dumps(config)
    return f"""[BUTTON_CONFIG:{config_json}]
Button {button_id} configured for {stop_name}. The button will now show quick departures from this stop."""

if __name__ == "__main__":
    mcp.run()
