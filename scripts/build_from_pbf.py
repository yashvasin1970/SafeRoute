"""Build a SafeRoute area from a local OpenStreetMap extract (.osm.pbf), within a radius of a centre.

Use this for big areas that are too heavy for one Overpass query, for example a 30 km radius.
Download an extract once (for example the Geofabrik southern-zone file), then:

    python scripts/build_from_pbf.py --pbf path/to/southern-zone-latest.osm.pbf \
        --name "Satyabama, Chennai (30 km)" --lat 12.8731 --lon 80.2219 --radius-km 30 \
        --out data/satyabama30.json

Needs pyrosm (pip install pyrosm). The road and shelter rules are the same as build_graph.py.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import networkx as nx
from build_graph import SHELTER_TAGS, haversine_m, to_area


def degrees_box(lat: float, lon: float, radius_km: float) -> list[float]:
    """A bounding box [min_lon, min_lat, max_lon, max_lat] that contains the circle."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * math.cos(math.radians(lat)))
    return [lon - dlon, lat - dlat, lon + dlon, lat + dlat]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pbf", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--radius-km", type=float, default=30.0)
    parser.add_argument("--shelters", type=int, default=5)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    from pyrosm import OSM  # imported here so the rest of the tools do not need it

    center = (args.lat, args.lon)
    radius_m = args.radius_km * 1000
    osm = OSM(str(args.pbf), bounding_box=degrees_box(args.lat, args.lon, args.radius_km))
    nodes_gdf, edges_gdf = osm.get_network(network_type="driving", nodes=True)
    pois = osm.get_pois(custom_filter=SHELTER_TAGS)

    # Keep only the junctions inside the circle, then only the roads that touch them.
    graph = nx.MultiDiGraph()
    inside = set()
    for row in nodes_gdf.itertuples(index=False):
        if haversine_m(center, (row.lat, row.lon)) <= radius_m:
            inside.add(row.id)
            graph.add_node(row.id, x=row.lon, y=row.lat)
    for row in edges_gdf.itertuples(index=False):
        if row.u in inside and row.v in inside:
            graph.add_edge(
                row.u,
                row.v,
                length=float(row.length),
                name=row.name,
                highway=row.highway,
                geometry=row.geometry,
            )

    area = to_area(args.name, center, graph, pois, args.shelters)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(area, separators=(",", ":")), encoding="utf-8")
    print(
        f"Wrote {args.out}: {len(area['nodes'])} junctions, {len(area['roads'])} roads, "
        f"{len(area['shelters'])} shelters"
    )


if __name__ == "__main__":
    main()
