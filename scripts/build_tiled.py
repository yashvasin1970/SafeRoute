"""Build a large SafeRoute area from OpenStreetMap by downloading it in smaller tiles.

One big Overpass query for a 30 km radius times out, so this splits the square around the centre
into a grid, downloads each tile, merges them by OpenStreetMap id, keeps what is inside the circle,
and writes the same area file as build_graph.py.

    python scripts/build_tiled.py --name "Satyabama, Chennai (30 km)" --lat 12.8731 --lon 80.2219 \
        --radius-km 30 --grid 3 --out data/satyabama30.json
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import networkx as nx
import osmnx as ox
from build_graph import SHELTER_TAGS, haversine_m, to_area


def tile_boxes(lat: float, lon: float, radius_km: float, grid: int) -> list[tuple[float, float, float, float]]:
    """Grid of (north, south, east, west) boxes covering the square around the centre."""
    half_lat = radius_km / 111.0
    half_lon = radius_km / (111.0 * math.cos(math.radians(lat)))
    step_lat = 2 * half_lat / grid
    step_lon = 2 * half_lon / grid
    boxes = []
    for i in range(grid):
        for j in range(grid):
            south = lat - half_lat + i * step_lat
            west = lon - half_lon + j * step_lon
            boxes.append((south + step_lat, south, west + step_lon, west))
    return boxes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--radius-km", type=float, default=30.0)
    parser.add_argument("--grid", type=int, default=3, help="tiles per side")
    parser.add_argument("--shelters", type=int, default=5)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    center = (args.lat, args.lon)
    radius_m = args.radius_km * 1000
    merged = nx.MultiDiGraph()
    pois = []

    boxes = tile_boxes(args.lat, args.lon, args.radius_km, args.grid)
    for index, (north, south, east, west) in enumerate(boxes, start=1):
        print(f"tile {index}/{len(boxes)}: {south:.3f},{west:.3f} to {north:.3f},{east:.3f}", flush=True)
        graph = None
        for attempt in range(3):  # Overpass sometimes drops a request; retry before giving up
            try:
                graph = ox.graph_from_bbox(bbox=(west, south, east, north), network_type="drive", simplify=True)
                break
            except Exception as error:  # an empty tile (for example, sea) raises here too
                print(f"  attempt {attempt + 1} failed: {error}", flush=True)
                time.sleep(20)
        if graph is not None:
            merged = nx.compose(merged, graph)
        try:
            features = ox.features_from_bbox(bbox=(west, south, east, north), tags=SHELTER_TAGS)
            pois.append(features)
        except Exception as error:  # a tile with no shelters is fine
            print(f"  no shelters in this tile ({error})", flush=True)

    # Keep only junctions inside the circle, then the roads between them.
    inside = [n for n, data in merged.nodes(data=True) if haversine_m(center, (data["y"], data["x"])) <= radius_m]
    graph = merged.subgraph(inside).copy()

    import pandas as pd

    features = pd.concat(pois) if pois else None
    if features is not None:
        centroids = features.geometry.centroid
        inside_mask = [haversine_m(center, (p.y, p.x)) <= radius_m for p in centroids]
        features = features[inside_mask]

    area = to_area(args.name, center, graph, features, args.shelters)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(area, separators=(",", ":")), encoding="utf-8")
    print(
        f"Wrote {args.out}: {len(area['nodes'])} junctions, {len(area['roads'])} roads, "
        f"{len(area['shelters'])} shelters"
    )


if __name__ == "__main__":
    main()
