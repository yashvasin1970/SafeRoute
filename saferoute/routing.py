"""Closure-aware evacuation routing.

The method from the proposal, step by step:

1. Load road network  -> ``RoadNetwork`` (built once at startup)
2. Mark blocked roads -> the ``closed`` set of road ids passed in
3. Remove closed edges -> ``open_roads`` returns a filtered *view* of the graph, so the original is
   never modified and nothing has to be copied
4. Shortest path to shelter -> Dijkstra over travel time from the person's junction
5. Show route + ETA -> ``RouteResult`` carries the path, its geometry and the ETA

Two refinements sit on top:

* **Risk-weighted routing.** In "safest" mode each open road's travel time is multiplied by
  (1 + risk) from ``risk.road_risk``, so routes steer around rising water, unverified flood
  reports and low-lying stretches in heavy rain. ETAs are always real travel times, and the result
  says how much longer the safer route is than the fastest one and what it avoids.
* **Shelter load balancing.** A shelter's score is its travel cost plus a penalty that grows once
  it is more than 70% full, counting people already on their way. People spread across shelters
  before any one fills, and a full shelter is never suggested.

Everything here is a pure function of its inputs, so every rule can be tested without a server.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from statistics import mean

import networkx as nx

from .network import WALK_KMH, RoadNetwork, Shelter, drive_kmh, seconds
from .risk import Risk

OPEN = "open"
SHELTER_STATUSES = ("open", "full", "closed")
PREFERENCES = ("safest", "fastest")
BALANCE_FROM = 0.7  # start nudging people elsewhere once a shelter is 70% full
BALANCE_SPAN_S = 600  # ...adding up to 10 minutes of "cost" at 100% (projected)
BALANCE_MAX_S = 900


def load_penalty_s(load: float) -> float:
    """Extra seconds added to a shelter's score as it fills, so people spread out early."""
    if load <= BALANCE_FROM:
        return 0.0
    return min(BALANCE_MAX_S, (load - BALANCE_FROM) / (1 - BALANCE_FROM) * BALANCE_SPAN_S)


def effective_status(status: str, occupancy: int, capacity: int) -> str:
    """A shelter a responder marked open counts as full once it holds its capacity."""
    if status != OPEN:
        return status
    return "full" if occupancy >= capacity else OPEN


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
    occupancy: int = 0
    en_route: int = 0
    score: float | None = None

    @property
    def capacity(self) -> int:
        return self.shelter.capacity

    @property
    def load(self) -> float:
        """Projected load: people inside plus people on their way, over capacity."""
        return (self.occupancy + self.en_route) / self.capacity

    @property
    def usable(self) -> bool:
        return self.status == OPEN and self.reachable and self.occupancy < self.capacity


@dataclass(frozen=True)
class Route:
    shelter: Shelter
    nodes: list[int]
    roads: list[str]
    distance_m: float
    eta_s: float
    steps: list[dict]
    geometry: list[tuple[float, float]]
    prefer: str = "fastest"
    risky: list[dict] = field(default_factory=list)  # risky roads still on the route
    fastest_eta_s: float | None = None  # ETA of the fastest route to the same shelter
    avoided: list[dict] = field(default_factory=list)  # risks the fastest route would have taken
    balanced_from: dict | None = None  # set when a nearer shelter was too full


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


def _path_seconds(network: RoadNetwork, path: list[int], mode: str) -> float:
    return sum(network.graph.edges[u, v][mode] for u, v in pairwise(path))


def _reasons(risk: Mapping[str, Risk], road_ids: Iterable[str]) -> list[dict]:
    seen, out = set(), []
    for road_id in road_ids:
        for reason in risk[road_id].reasons if road_id in risk else ():
            if reason not in seen:
                seen.add(reason)
                out.append(reason.as_dict())
    return out


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
    *,
    occupancy: Mapping[str, int] | None = None,
    en_route: Mapping[str, int] | None = None,
    risk: Mapping[str, Risk] | None = None,
    prefer: str = "fastest",
) -> RouteResult:
    """Route from a junction to the best open shelter that can still be reached.

    Shelters that are full, closed or cut off by closures are reported but never suggested. With
    ``prefer="safest"`` risky roads cost more; with ``"fastest"`` only travel time counts.
    """
    occupancy = occupancy or {}
    en_route = en_route or {}
    risk = risk or {}
    view = open_roads(network, closed)

    if prefer == "safest" and risk:

        def weight(u, v, data):
            road_risk = risk.get(data["road"])
            return data[mode] * (1 + road_risk.factor) if road_risk else data[mode]

    else:
        weight = mode
    costs, paths = nx.single_source_dijkstra(view, start_node, weight=weight)
    lead = _first_leg_seconds(snap_m, mode)

    options = []
    for shelter in network.shelters.values():
        reachable = shelter.node in costs
        people = int(occupancy.get(shelter.id, 0))
        status = effective_status(shelter_status.get(shelter.id, OPEN), people, shelter.capacity)
        eta = distance = None
        if reachable:
            path = paths[shelter.node]
            _, distance, _, _ = _path_details(network, path)
            distance += snap_m
            eta = _path_seconds(network, path, mode) + lead
        option = ShelterOption(
            shelter=shelter,
            status=status,
            reachable=reachable,
            eta_s=eta,
            distance_m=distance,
            occupancy=people,
            en_route=int(en_route.get(shelter.id, 0)),
        )
        if reachable:
            score = costs[shelter.node] + lead + load_penalty_s(option.load)
            option = ShelterOption(**{**option.__dict__, "score": score})
        options.append(option)
    options.sort(key=lambda o: (not o.usable, o.score is None, o.score or 0, o.shelter.name))

    usable = [o for o in options if o.usable]
    best = usable[0] if usable else None
    route = None
    if best is not None:
        path = paths[best.shelter.node]
        road_ids, distance, steps, geometry = _path_details(network, path)

        balanced_from = None
        nearest = min(usable, key=lambda o: o.eta_s)
        if nearest is not best and nearest.load > BALANCE_FROM:
            balanced_from = {
                "shelter": {"id": nearest.shelter.id, "name": nearest.shelter.name},
                "load_pct": round(100 * nearest.load),
                "extra_s": round(best.eta_s - nearest.eta_s),
            }

        fastest_eta = None
        avoided: list[dict] = []
        if prefer == "safest" and risk:
            fastest_path = nx.dijkstra_path(view, start_node, best.shelter.node, weight=mode)
            fastest_eta = _path_seconds(network, fastest_path, mode) + lead
            on_route = set(road_ids)
            avoided = _reasons(risk, [r for r in _path_details(network, fastest_path)[0] if r not in on_route])

        route = Route(
            shelter=best.shelter,
            nodes=path,
            roads=road_ids,
            distance_m=distance + snap_m,
            eta_s=best.eta_s,
            steps=steps,
            geometry=geometry,
            prefer=prefer,
            risky=[
                {"road": r, "name": network.roads[r].name, "reasons": _reasons(risk, [r])}
                for r in dict.fromkeys(road_ids)
                if r in risk
            ],
            fastest_eta_s=fastest_eta,
            avoided=avoided,
            balanced_from=balanced_from,
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
