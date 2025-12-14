#!/usr/bin/env python3
"""
Regenerate station databases with enhanced schema, split by route type.
Includes route, direction, zone, and suburb data for filtering destinations.
"""

import asyncio
import json
import httpx
from collections import defaultdict
from ptv_client import PTVClient


async def get_all_routes(client: PTVClient, route_type: int) -> list[dict]:
    """Get all routes for a route type."""
    endpoint = "/v3/routes"
    params = {"route_types": route_type}
    url = client._sign_request(endpoint, params)
    
    async with httpx.AsyncClient() as http_client:
        resp = await http_client.get(url)
        resp.raise_for_status()
        return resp.json().get("routes", [])


async def get_directions_for_route(client: PTVClient, route_id: int) -> dict:
    """Get directions for a route, returns {direction_id: direction_name}."""
    result = await client.get_directions(route_id)
    return {d["direction_id"]: d["direction_name"] for d in result.get("directions", [])}


async def get_stops_for_route_direction(client: PTVClient, route_id: int, route_type: int, direction_id: int) -> list:
    """Get stops for a route+direction with stop_sequence."""
    endpoint = f"/v3/stops/route/{route_id}/route_type/{route_type}"
    params = {"direction_id": direction_id}
    url = client._sign_request(endpoint, params)
    
    async with httpx.AsyncClient() as http_client:
        resp = await http_client.get(url)
        resp.raise_for_status()
        return resp.json().get("stops", [])


async def build_enhanced_db(client: PTVClient, route_type: int, label: str) -> dict:
    """Build enhanced stop database for a route type."""
    print(f"\n{'='*60}")
    print(f"Building {label} database (route_type={route_type})")
    print("="*60)
    
    routes = await get_all_routes(client, route_type)
    print(f"Found {len(routes)} routes")
    
    # stop_id -> stop data with routes info
    stops_map = {}
    
    for i, route in enumerate(routes):
        route_id = route["route_id"]
        route_name = route["route_name"]
        print(f"[{i+1}/{len(routes)}] {route_name}...", end=" ", flush=True)
        
        try:
            # Get directions
            directions = await get_directions_for_route(client, route_id)
            
            # Get stops for each direction
            for dir_id, dir_name in directions.items():
                stops = await get_stops_for_route_direction(client, route_id, route_type, dir_id)
                
                for stop in stops:
                    stop_id = stop["stop_id"]
                    
                    if stop_id not in stops_map:
                        # First time seeing this stop
                        ticket = stop.get("stop_ticket", {}) or {}
                        stops_map[stop_id] = {
                            "name": stop.get("stop_name", "Unknown"),
                            "stop_id": stop_id,
                            "route_type": route_type,
                            "lat": stop.get("stop_latitude"),
                            "lon": stop.get("stop_longitude"),
                            "suburb": stop.get("stop_suburb", ""),
                            "zone": ticket.get("zone", ""),
                            "free_zone": ticket.get("is_free_fare_zone", False),
                            "routes": {}
                        }
                    
                    # Add route+direction info
                    if str(route_id) not in stops_map[stop_id]["routes"]:
                        stops_map[stop_id]["routes"][str(route_id)] = {
                            "name": route_name,
                            "dirs": {}
                        }
                    
                    stops_map[stop_id]["routes"][str(route_id)]["dirs"][str(dir_id)] = {
                        "name": dir_name,
                        "seq": stop.get("stop_sequence", 0)
                    }
                
                await asyncio.sleep(0.05)  # Be nice to API
            
            print(f"✓")
            
        except Exception as e:
            print(f"ERROR: {e}")
            continue
    
    # Convert to list sorted by name
    stops_list = sorted(stops_map.values(), key=lambda s: s["name"].lower())
    
    print(f"\nTotal stops: {len(stops_list)}")
    
    return {"stops": stops_list, "route_type": route_type, "label": label}


async def main():
    client = PTVClient()
    
    # Build separate databases
    configs = [
        (0, "Metro Train", "stations_train.json"),
        (3, "V/Line", "stations_vline.json"),
        (1, "Tram", "stops_tram.json"),
    ]
    
    for route_type, label, filename in configs:
        data = await build_enhanced_db(client, route_type, label)
        
        # Save to file
        with open(filename, "w") as f:
            json.dump(data, f, indent=2)
        
        size = len(json.dumps(data))
        print(f"Saved to {filename} ({size/1024:.1f} KB)")
        
        # Show sample
        if data["stops"]:
            sample = data["stops"][0]
            print(f"Sample: {json.dumps(sample, indent=2)[:500]}...")


if __name__ == "__main__":
    asyncio.run(main())
