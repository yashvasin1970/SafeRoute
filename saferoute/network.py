"""The road network: junctions, roads, shelters and sensors loaded from an area file.

An area file is produced by ``scripts/build_graph.py`` from OpenStreetMap data. Roads are
undirected: during an evacuation, responders control traffic, so a road that is open is treated as
usable in both directions.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import networkx as nx

MODES = ("foot", "vehicle")

# Speeds used for ETAs, in km/h. Vehicle speeds sit well below normal limits because roads are
# crowded and visibility is poor during an evacuation.
WALK_KMH = 4.0
DRIVE_KMH = {
    "motorway": 40,
    "trunk": 40,
    "primary": 30,
    "secondary": 25,
    "tertiary": 22,
    "unclassified": 18,
    "residential": 15,
    "living_street": 10,
}
# A tap further than this from every junction is outside the mapped area.
MAX_SNAP_M = 400


@dataclass(frozen=True)
class Road:
    id: str
    u: int
    v: int
    name: str
    kind: str
    length_m: float
    geometry: tuple[tuple[float, float], ...]


DEFAULT_CAPACITY = 200


@dataclass(frozen=True)
class Shelter:
    id: str
    name: str
    kind: str
    lat: float
    lon: float
    node: int
    capacity: int = DEFAULT_CAPACITY


@dataclass(frozen=True)
class Sensor:
    """A water-level sensor. It watches the low-lying stretch around it, not just one segment."""

    id: str
    road: str
    label: str
    lat: float
    lon: float
    radius_m: float
    roads: tuple[str, ...]


DEFAULT_SENSOR_RADIUS_M = 80


def seconds(length_m: float, kmh: float) -> float:
    return length_m / (kmh / 3.6)


def drive_kmh(kind: str) -> float:
    return DRIVE_KMH.get(kind, DRIVE_KMH["residential"])


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def distance_to_line_m(point: tuple[float, float], line: tuple[tuple[float, float], ...]) -> float:
    """Shortest distance from a point to a polyline, in metres (flat-earth, fine at street scale)."""
    lat0 = math.radians(point[0])
    scale = (111_320 * math.cos(lat0), 110_540)  # metres per degree of longitude, latitude

    def xy(p):
        return ((p[1] - point[1]) * scale[0], (p[0] - point[0]) * scale[1])

    best = math.inf
    for a, b in pairwise(line):
        (ax, ay), (bx, by) = xy(a), xy(b)
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length2))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


class RoadNetwork:
    """An immutable road graph. Closures never modify it; routing filters a view of it instead."""

    def __init__(self, area: dict, sensors: list[dict] | None = None, shelter_info: dict | None = None):
        self.meta: dict = area["area"]
        self.coords: dict[int, tuple[float, float]] = {n["id"]: (n["lat"], n["lon"]) for n in area["nodes"]}
        self.roads: dict[str, Road] = {
            r["id"]: Road(
                id=r["id"],
                u=r["u"],
                v=r["v"],
                name=r["name"],
                kind=r["kind"],
                length_m=float(r["length_m"]),
                geometry=tuple((p[0], p[1]) for p in r["geometry"]),
            )
            for r in area["roads"]
        }
        shelter_info = shelter_info or {}
        self.shelters: dict[str, Shelter] = {}
        for s in area["shelters"]:
            capacity = int(shelter_info.get(s["id"], {}).get("capacity", s.get("capacity", DEFAULT_CAPACITY)))
            if capacity <= 0:
                raise ValueError(f"shelter {s['id']} needs a positive capacity")
            self.shelters[s["id"]] = Shelter(**{**s, "capacity": capacity})
        self.sensors: dict[str, Sensor] = {}
        for s in sensors or []:
            if s["road"] not in self.roads:
                raise ValueError(f"sensor {s['id']} watches unknown road {s['road']}")
            radius = float(s.get("radius_m", DEFAULT_SENSOR_RADIUS_M))
            point = (s["lat"], s["lon"])
            nearby = [r.id for r in self.roads.values() if distance_to_line_m(point, r.geometry) <= radius]
            self.sensors[s["id"]] = Sensor(
                id=s["id"],
                road=s["road"],
                label=s["label"],
                lat=s["lat"],
                lon=s["lon"],
                radius_m=radius,
                roads=tuple(sorted({s["road"], *nearby})),
            )

        self.graph = nx.Graph()
        self.graph.add_nodes_from(self.coords)
        for road in self.roads.values():
            self.graph.add_edge(
                road.u,
                road.v,
                road=road.id,
                foot=seconds(road.length_m, WALK_KMH),
                vehicle=seconds(road.length_m, drive_kmh(road.kind)),
            )

        for shelter in self.shelters.values():
            if shelter.node not in self.coords:
                raise ValueError(f"shelter {shelter.id} sits on unknown junction {shelter.node}")

        # Street name -> its segments, longest first (for SMS and "pick your street").
        self.streets: dict[str, list[str]] = {}
        for road in sorted(self.roads.values(), key=lambda r: -r.length_m):
            if road.name != "Unnamed road":
                self.streets.setdefault(road.name, []).append(road.id)

    @classmethod
    def load(cls, area_file: Path, sensors_file: Path | None = None, shelters_file: Path | None = None) -> RoadNetwork:
        area = json.loads(Path(area_file).read_text(encoding="utf-8"))
        sensors, shelter_info = [], {}
        if sensors_file and Path(sensors_file).exists():
            sensors = json.loads(Path(sensors_file).read_text(encoding="utf-8"))["sensors"]
        if shelters_file and Path(shelters_file).exists():
            shelter_info = json.loads(Path(shelters_file).read_text(encoding="utf-8"))["shelters"]
        return cls(area, sensors, shelter_info)

    def nearest_node(self, lat: float, lon: float) -> tuple[int, float] | None:
        """The closest junction to a point and how far away it is, or None if outside the area."""
        node, dist = min(
            ((n, distance_m((lat, lon), c)) for n, c in self.coords.items()),
            key=lambda pair: pair[1],
        )
        return (node, dist) if dist <= MAX_SNAP_M else None

    def nearest_road(self, lat: float, lon: float, max_m: float) -> tuple[str, float] | None:
        """The closest road to a point, or None if none is within max_m metres."""
        road, dist = min(
            ((r.id, distance_to_line_m((lat, lon), r.geometry)) for r in self.roads.values()),
            key=lambda pair: pair[1],
        )
        return (road, dist) if dist <= max_m else None

    def roads_within(self, lat: float, lon: float, radius_m: float) -> list[str]:
        return [r.id for r in self.roads.values() if distance_to_line_m((lat, lon), r.geometry) <= radius_m]

    def street_point(self, name: str) -> tuple[float, float]:
        """A point in the middle of a street's longest segment."""
        geometry = self.roads[self.streets[name][0]].geometry
        return geometry[len(geometry) // 2]

    def client_payload(self) -> dict:
        """Static data the map needs. It never changes while the app runs, so browsers cache it."""
        return {
            "area": self.meta,
            "nodes": [[n, *self.coords[n]] for n in sorted(self.coords)],
            "roads": [
                {
                    "id": r.id,
                    "u": r.u,
                    "v": r.v,
                    "name": r.name,
                    "kind": r.kind,
                    "length_m": r.length_m,
                    "geometry": r.geometry,
                }
                for r in self.roads.values()
            ],
            "shelters": [
                {
                    "id": s.id,
                    "name": s.name,
                    "kind": s.kind,
                    "lat": s.lat,
                    "lon": s.lon,
                    "node": s.node,
                    "capacity": s.capacity,
                }
                for s in self.shelters.values()
            ],
            "sensors": [
                {
                    "id": s.id,
                    "label": s.label,
                    "lat": s.lat,
                    "lon": s.lon,
                    "radius_m": s.radius_m,
                    "roads": list(s.roads),
                }
                for s in self.sensors.values()
            ],
        }
