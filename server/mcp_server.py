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

if __name__ == "__main__":
    mcp.run()
