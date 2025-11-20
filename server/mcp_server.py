from mcp.server.fastmcp import FastMCP
from . import tools

# Initialize FastMCP
mcp = FastMCP("ptv")

@mcp.tool()
async def get_departures(stop_id: int, route_type: int = 0) -> str:
    """
    Get next departures for a specific stop.
    
    Args:
        stop_id: The ID of the stop (e.g., 1071 for Flinders St).
        route_type: Transport mode (0=Train, 1=Tram, 2=Bus, 3=VLine, 4=Night Bus).
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

if __name__ == "__main__":
    mcp.run()
