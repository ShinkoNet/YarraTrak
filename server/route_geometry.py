import json
import math
import os
from typing import Dict, List, Tuple

# Caches loaded from stations_train.json (metro only)
_loaded = False
_stop_index: Dict[int, Tuple[float, float]] = {}
_route_dir_points: Dict[Tuple[int, int], List[Tuple[float, float, int]]] = {}
_route_dir_cumdist: Dict[Tuple[int, int], List[float]] = {}
_route_dir_stopdist: Dict[Tuple[int, int], Dict[int, float]] = {}

EARTH_RADIUS_M = 6371000.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_r = math.radians(lat1)
    lon1_r = math.radians(lon1)
    lat2_r = math.radians(lat2)
    lon2_r = math.radians(lon2)

    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_M * c


def _to_xy(lat: float, lon: float, lat0_rad: float) -> Tuple[float, float]:
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    x = EARTH_RADIUS_M * lon_r * math.cos(lat0_rad)
    y = EARTH_RADIUS_M * lat_r
    return x, y


def load_train_routes(path: str | None = None) -> None:
    global _loaded
    if _loaded:
        return

    base_dir = os.path.dirname(__file__)
    path = path or os.path.join(base_dir, "stations_train.json")
    if not os.path.exists(path):
        print(f"route_geometry: stations file not found at {path}")
        return

    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception as e:
        print(f"route_geometry: failed to load {path}: {e}")
        return

    stops = data.get("stops", [])
    for stop in stops:
        stop_id = stop.get("stop_id")
        lat = stop.get("lat")
        lon = stop.get("lon")
        if stop_id is None or lat is None or lon is None:
            continue

        _stop_index[int(stop_id)] = (float(lat), float(lon))

        routes = stop.get("routes", {})
        for route_id_str, route_info in routes.items():
            dirs = route_info.get("dirs", {})
            for dir_id_str, dir_info in dirs.items():
                seq = dir_info.get("seq")
                if seq is None:
                    continue
                key = (int(route_id_str), int(dir_id_str))
                _route_dir_points.setdefault(key, []).append(
                    (int(seq), int(stop_id), float(lat), float(lon))
                )

    # Sort by sequence and build cumulative distances
    for key, points in list(_route_dir_points.items()):
        points.sort(key=lambda x: x[0])
        sorted_points = [(p[2], p[3], p[1]) for p in points]  # (lat, lon, stop_id)
        _route_dir_points[key] = sorted_points

        if not sorted_points:
            continue

        cum = [0.0]
        stopdist = {sorted_points[0][2]: 0.0}

        for i in range(1, len(sorted_points)):
            lat1, lon1, _ = sorted_points[i - 1]
            lat2, lon2, stop_id = sorted_points[i]
            seg_m = _haversine_m(lat1, lon1, lat2, lon2)
            cum.append(cum[-1] + seg_m)
            stopdist[stop_id] = cum[-1]

        _route_dir_cumdist[key] = cum
        _route_dir_stopdist[key] = stopdist

    _loaded = True
    print(f"route_geometry: loaded {len(_route_dir_points)} route-direction shapes")


def _ensure_loaded() -> None:
    if not _loaded:
        load_train_routes()


def _direct_distance_km(stop_id: int, vehicle_lat: float, vehicle_lon: float) -> float | None:
    stop_pos = _stop_index.get(int(stop_id))
    if not stop_pos:
        return None
    dist_m = _haversine_m(vehicle_lat, vehicle_lon, stop_pos[0], stop_pos[1])
    return dist_m / 1000.0


def distance_along_route(route_id: int, direction_id: int, vehicle_lat: float, vehicle_lon: float) -> float | None:
    _ensure_loaded()
    key = (int(route_id), int(direction_id))
    points = _route_dir_points.get(key)
    if not points or len(points) < 2:
        return None

    cum = _route_dir_cumdist.get(key, [])
    best_dist = None
    best_along = None

    for i in range(len(points) - 1):
        lat1, lon1, _ = points[i]
        lat2, lon2, _ = points[i + 1]
        lat0_rad = math.radians((lat1 + lat2) / 2.0)

        ax, ay = _to_xy(lat1, lon1, lat0_rad)
        bx, by = _to_xy(lat2, lon2, lat0_rad)
        px, py = _to_xy(vehicle_lat, vehicle_lon, lat0_rad)

        vx = bx - ax
        vy = by - ay
        wx = px - ax
        wy = py - ay
        seg_len2 = vx * vx + vy * vy
        if seg_len2 == 0:
            continue

        t = (wx * vx + wy * vy) / seg_len2
        if t < 0:
            t = 0.0
        elif t > 1:
            t = 1.0

        proj_x = ax + t * vx
        proj_y = ay + t * vy
        dist = math.hypot(px - proj_x, py - proj_y)

        if best_dist is None or dist < best_dist:
            seg_m = _haversine_m(lat1, lon1, lat2, lon2)
            best_dist = dist
            best_along = cum[i] + t * seg_m

    return best_along


def distance_to_stop(
    route_id: int | None,
    direction_id: int | None,
    vehicle_lat: float,
    vehicle_lon: float,
    stop_id: int,
) -> float | None:
    _ensure_loaded()
    if route_id is None or direction_id is None:
        return _direct_distance_km(stop_id, vehicle_lat, vehicle_lon)

    key = (int(route_id), int(direction_id))
    stopdist = _route_dir_stopdist.get(key)
    if not stopdist or int(stop_id) not in stopdist:
        return _direct_distance_km(stop_id, vehicle_lat, vehicle_lon)

    vehicle_along = distance_along_route(route_id, direction_id, vehicle_lat, vehicle_lon)
    if vehicle_along is None:
        return _direct_distance_km(stop_id, vehicle_lat, vehicle_lon)

    dist_m = stopdist[int(stop_id)] - vehicle_along
    if dist_m < 0:
        dist_m = 0.0
    return dist_m / 1000.0
