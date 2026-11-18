"""Closure-aware evacuation routing.

The method from the proposal, step by step:

1. Load road network  -> ``RoadNetwork`` (built once at startup)
2. Mark blocked roads -> the ``closed`` set of road ids passed in
3. Remove closed edges -> ``open_roads`` returns a filtered *view* of the graph, so the original is
   never modified and nothing has to be copied
4. Shortest path to shelter -> Dijkstra over travel time from the person's junction
5. Show route + ETA -> ``RouteResult`` carries the path, its geometry and the ETA

Everything here is a pure function of its inputs, so every rule can be tested without a server.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from statistics import mean

import networkx as nx

from .network import WALK_KMH, RoadNetwork, Shelter, drive_kmh, seconds

OPEN = "open"
SHELTER_STATUSES = ("open", "full", "closed")


def open_roads(network: RoadNetwork, closed: Iterable[str]) -> nx.Graph:
    """The road graph with closed roads removed (a read-only view, not a copy)."""
    closed = frozenset(closed)
    graph = network.graph
    if not closed:
        return graph
    return nx.subgraph_view(graph, filter_edge=lambda u, v: graph.edges[u, v]["road"] not in closed)


@dataclass(frozen=True)
class ShelterOption:
    shelter: Shelter
    status: str
    reachable: bool
    eta_s: float | None
    distance_m: float | None

    @property
    def usable(self) -> bool:
        return self.status == OPEN and self.reachable


@dataclass(frozen=True)
class Route:
    shelter: Shelter
    nodes: list[int]
    roads: list[str]
    distance_m: float
    eta_s: float
    steps: list[dict]
    geometry: list[tuple[float, float]]


@dataclass(frozen=True)
class RouteResult:
    start_node: int
    snap_m: float
    route: Route | None
    options: list[ShelterOption] = field(default_factory=list)


def _road_between(network: RoadNetwork, u: int, v: int):
    return network.roads[network.graph.edges[u, v]["road"]]


def _path_details(network: RoadNetwork, path: list[int]):
    road_ids, steps, geometry = [], [], []
    distance = 0.0
    for u, v in pairwise(path):
        road = _road_between(network, u, v)
        road_ids.append(road.id)
        distance += road.length_m
        coords = list(road.geometry) if road.u == u else list(reversed(road.geometry))
        geometry.extend(coords if not geometry else coords[1:])
        if steps and steps[-1]["name"] == road.name:
            steps[-1]["length_m"] += road.length_m
        else:
            steps.append({"name": road.name, "length_m": road.length_m})
    for step in steps:
        step["length_m"] = round(step["length_m"])
    if not geometry and path:
        geometry = [network.coords[path[0]]]
    return road_ids, distance, steps, geometry


def _first_leg_seconds(snap_m: float, mode: str) -> float:
    """Time to get from the tapped point to the nearest junction."""
    return seconds(snap_m, WALK_KMH if mode == "foot" else drive_kmh("residential"))


def find_route(
    network: RoadNetwork,
    start_node: int,
    closed: Iterable[str],
    shelter_status: Mapping[str, str],
    mode: str,
    snap_m: float = 0.0,
) -> RouteResult:
    """Fastest route from a junction to the nearest open shelter that can still be reached.

    Shelters that are full, closed or cut off by closures are reported but never suggested.
    """
    view = open_roads(network, closed)
    times, paths = nx.single_source_dijkstra(view, start_node, weight=mode)
    lead = _first_leg_seconds(snap_m, mode)

    options = []
    for shelter in network.shelters.values():
        status = shelter_status.get(shelter.id, OPEN)
        reachable = shelter.node in times
        distance = None
        if reachable:
            _, distance, _, _ = _path_details(network, paths[shelter.node])
            distance += snap_m
        options.append(
            ShelterOption(
                shelter=shelter,
                status=status,
                reachable=reachable,
                eta_s=times[shelter.node] + lead if reachable else None,
                distance_m=distance,
            )
        )
    options.sort(key=lambda o: (not o.usable, o.eta_s is None, o.eta_s or 0, o.shelter.name))

    best = next((o for o in options if o.usable), None)
    route = None
    if best is not None:
        path = paths[best.shelter.node]
        road_ids, distance, steps, geometry = _path_details(network, path)
        route = Route(
            shelter=best.shelter,
            nodes=path,
            roads=road_ids,
            distance_m=distance + snap_m,
            eta_s=best.eta_s,
            steps=steps,
            geometry=geometry,
        )
    return RouteResult(start_node=start_node, snap_m=snap_m, route=route, options=options)


def coverage(
    network: RoadNetwork,
    closed: Iterable[str],
    shelter_status: Mapping[str, str],
    mode: str,
) -> dict[int, float | None]:
    """Seconds from every junction to its nearest open shelter (None when cut off).

    One multi-source Dijkstra from all open shelters at once. Roads are undirected, so the time
    from a shelter to a junction equals the time from that junction to the shelter.
    """
    sources = {s.node for s in network.shelters.values() if shelter_status.get(s.id, OPEN) == OPEN}
    if not sources:
        return dict.fromkeys(network.graph.nodes)
    times = nx.multi_source_dijkstra_path_length(open_roads(network, closed), sources, weight=mode)
    return {node: times.get(node) for node in network.graph.nodes}


def summarize(times: Mapping[int, float | None]) -> dict:
    reached = sorted(t for t in times.values() if t is not None)
    total = len(times)
    return {
        "junctions": total,
        "reachable": len(reached),
        "cut_off": total - len(reached),
        "cut_off_pct": round(100 * (total - len(reached)) / total, 1) if total else 0.0,
        "mean_eta_s": round(mean(reached)) if reached else None,
        "p90_eta_s": round(reached[int(0.9 * (len(reached) - 1))]) if reached else None,
        "worst_eta_s": round(reached[-1]) if reached else None,
    }


def impact(
    network: RoadNetwork,
    live_closed: Iterable[str],
    extra_closed: Iterable[str],
    shelter_status: Mapping[str, str],
    extra_shelters_closed: Iterable[str],
    mode: str,
) -> dict:
    """Compare evacuation coverage now with a what-if scenario (planner tool).

    The scenario adds hypothetical road closures and shelter closures on top of the live state.
    Nothing is published; the live map is untouched.
    """
    live_closed = frozenset(live_closed)
    scenario_closed = live_closed | frozenset(extra_closed)
    scenario_status = dict(shelter_status)
    for shelter_id in extra_shelters_closed:
        scenario_status[shelter_id] = "closed"

    before = coverage(network, live_closed, shelter_status, mode)
    after = coverage(network, scenario_closed, scenario_status, mode)
    newly_cut_off = [n for n in after if after[n] is None and before[n] is not None]
    slower = [n for n in after if after[n] is not None and before[n] is not None and after[n] - before[n] >= 60]
    return {
        "mode": mode,
        "before": summarize(before),
        "after": summarize(after),
        "newly_cut_off": len(newly_cut_off),
        "slower_by_a_minute_or_more": len(slower),
        "eta_by_node": {str(n): (round(t) if t is not None else None) for n, t in after.items()},
    }
