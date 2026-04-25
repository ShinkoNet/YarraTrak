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
async def setup_favourite_entry(
    entry_id: int,
    start_station: str,
    destination: str,
    route_type: str = "TRAIN"
) -> str:
    """
    Configure a favourite-entry slot on the Pebble watch (slots 1-10).
    Users may call these entries / buttons / favourites / saved stops / slots.
    Just provide the station NAMES - all IDs and directions are resolved automatically!

    Args:
        entry_id: Which entry slot to configure (1-10)
        start_station: Name of the START station (e.g., "Narre Warren", "Richmond")
        destination: Name of the DESTINATION station (e.g., "Flinders Street", "the city")
        route_type: "TRAIN", "TRAM", or "VLINE" (default: TRAIN)

    Example: For commuting from Narre Warren to the city:
        setup_favourite_entry(1, "Narre Warren", "Flinders Street")

    If there's an error (wrong spelling, stations not on same line), the response
    will tell you exactly what to do next (ask clarification, retry, etc).
    """
    return await tools.setup_favourite_entry(
        entry_id=entry_id,
        start_station=start_station,
        destination=destination,
        route_type=route_type,
    )

if __name__ == "__main__":
    mcp.run()
