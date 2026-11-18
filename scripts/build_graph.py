"""Build a SafeRoute area file from OpenStreetMap with OSMnx.

This is the "Load road network" step. It reads the drivable roads for an area, keeps one undirected
edge per pair of junctions, picks candidate shelters from mapped schools, colleges and community
halls, and writes everything to a small JSON file that the app loads at startup.

Only this script needs OSMnx. The app itself reads the JSON file, so it runs offline and starts in
well under a second.

Two ways to get the map data:

  From an .osm file (use the Export button on openstreetmap.org for a small area):
    python scripts/build_graph.py --name "Velachery, Chennai" --xml data/raw/velachery.osm \
        --out data/velachery.json

  Straight from an Overpass API server:
    python scripts/build_graph.py --name "Velachery, Chennai" --lat 12.98 --lon 80.22 --dist 900 \
        --out data/velachery.json
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import networkx as nx
import osmnx as ox

SHELTER_TAGS = {"amenity": ["school", "college", "community_centre", "townhall"]}
ROAD_CLASSES = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
}
NO_ENTRY = {"no", "private"}


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def first(value):
    """OSMnx stores merged attributes as lists; take the first one."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def road_class(highway) -> str:
    tag = (first(highway) or "").replace("_link", "")
    return tag if tag in ROAD_CLASSES else "residential"


def drivable_only(xml_path: Path) -> Path:
    """Copy an .osm file keeping every node but only the ways a vehicle can use."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    for way in root.findall("way"):
        tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
        highway = (tags.get("highway") or "").replace("_link", "")
        keep = (
            highway in ROAD_CLASSES
            and tags.get("area") != "yes"
            and tags.get("access") not in NO_ENTRY
            and tags.get("motor_vehicle") not in NO_ENTRY
        )
        if not keep:
            root.remove(way)
    for relation in root.findall("relation"):
        root.remove(relation)
    out = Path(tempfile.mkdtemp()) / "roads.osm"
    tree.write(out, encoding="utf-8", xml_declaration=True)
    return out


def xml_center(xml_path: Path) -> tuple[float, float]:
    bounds = ET.parse(xml_path).getroot().find("bounds")
    lat = (float(bounds.get("minlat")) + float(bounds.get("maxlat"))) / 2
    lon = (float(bounds.get("minlon")) + float(bounds.get("maxlon"))) / 2
    return round(lat, 6), round(lon, 6)


def spread_out(candidates: list[dict], count: int) -> list[dict]:
    """Pick `count` candidates far apart from each other (farthest-point sampling)."""
    if len(candidates) <= count:
        return candidates
    chosen = [candidates[0]]
    while len(chosen) < count:
        best = max(
            (c for c in candidates if c not in chosen),
            key=lambda c: min(haversine_m((c["lat"], c["lon"]), (s["lat"], s["lon"])) for s in chosen),
        )
        chosen.append(best)
    return chosen


def to_area(name: str, center: tuple[float, float], graph, features, shelters: int) -> dict:
    graph = ox.convert.to_undirected(graph)
    # Keep the largest connected piece so no junction starts out cut off by the map's edge.
    graph = graph.subgraph(max(nx.connected_components(graph), key=len)).copy()

    osm_ids = sorted(graph.nodes)
    index = {osm_id: i for i, osm_id in enumerate(osm_ids)}
    nodes = [
        {"id": index[n], "lat": round(graph.nodes[n]["y"], 6), "lon": round(graph.nodes[n]["x"], 6)} for n in osm_ids
    ]

    # Keep the shortest edge between each pair of junctions.
    best: dict[tuple[int, int], dict] = {}
    for u, v, data in graph.edges(data=True):
        a, b = sorted((index[u], index[v]))
        if a == b:
            continue
        length = float(data.get("length", 0.0))
        if (a, b) in best and best[(a, b)]["length_m"] <= length:
            continue
        if "geometry" in data:
            coords = [(round(y, 6), round(x, 6)) for x, y in data["geometry"].coords]
        else:
            coords = [(nodes[a]["lat"], nodes[a]["lon"]), (nodes[b]["lat"], nodes[b]["lon"])]
        # Store geometry running from a to b.
        start = (nodes[a]["lat"], nodes[a]["lon"])
        if haversine_m(coords[0], start) > haversine_m(coords[-1], start):
            coords.reverse()
        best[(a, b)] = {
            "u": a,
            "v": b,
            "name": first(data.get("name")) or "Unnamed road",
            "kind": road_class(data.get("highway")),
            "length_m": round(length, 1),
            "geometry": [list(p) for p in coords],
        }
    roads = [{"id": f"r{i}", **road} for i, road in enumerate(sorted(best.values(), key=lambda r: (r["u"], r["v"])))]

    candidates = []
    for _, row in features.iterrows():
        label = row.get("name")
        if not isinstance(label, str) or not label.strip():
            continue
        point = row.geometry.centroid
        candidates.append({"name": label.strip(), "kind": row.get("amenity"), "lat": point.y, "lon": point.x})
    candidates.sort(key=lambda c: c["name"])
    picked = spread_out(candidates, shelters)

    shelter_list = []
    for i, c in enumerate(picked, start=1):
        node = min(nodes, key=lambda n: haversine_m((n["lat"], n["lon"]), (c["lat"], c["lon"])))
        shelter_list.append(
            {
                "id": f"s{i}",
                "name": c["name"],
                "kind": c["kind"],
                "lat": round(c["lat"], 6),
                "lon": round(c["lon"], 6),
                "node": node["id"],
            }
        )

    return {
        "area": {
            "name": name,
            "center": list(center),
            "source": "© OpenStreetMap contributors (ODbL)",
            "fetched": date.today().isoformat(),
            "note": "Shelters are mapped schools, colleges and halls picked as sample relief camps, "
            "not official designations.",
        },
        "nodes": nodes,
        "roads": roads,
        "shelters": shelter_list,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help='area label, e.g. "Velachery, Chennai"')
    parser.add_argument("--xml", type=Path, help="a local .osm file exported from openstreetmap.org")
    parser.add_argument("--lat", type=float, help="centre latitude (Overpass mode)")
    parser.add_argument("--lon", type=float, help="centre longitude (Overpass mode)")
    parser.add_argument("--dist", type=int, default=900, help="radius in metres (Overpass mode)")
    parser.add_argument("--shelters", type=int, default=5)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--overpass", help="Overpass API endpoint to use instead of the OSMnx default")
    args = parser.parse_args()

    if args.xml:
        center = xml_center(args.xml)
        graph = ox.graph_from_xml(drivable_only(args.xml), simplify=True, retain_all=False)
        features = ox.features_from_xml(args.xml, tags=SHELTER_TAGS)
    elif args.lat is not None and args.lon is not None:
        if args.overpass:
            ox.settings.overpass_url = args.overpass
        center = (args.lat, args.lon)
        graph = ox.graph_from_point(center, dist=args.dist, network_type="drive", simplify=True)
        features = ox.features_from_point(center, tags=SHELTER_TAGS, dist=args.dist)
    else:
        parser.error("give either --xml or both --lat and --lon")

    area = to_area(args.name, center, graph, features, args.shelters)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(area, separators=(",", ":")), encoding="utf-8")
    print(
        f"Wrote {args.out}: {len(area['nodes'])} junctions, {len(area['roads'])} roads, "
        f"{len(area['shelters'])} shelters"
    )


if __name__ == "__main__":
    main()
