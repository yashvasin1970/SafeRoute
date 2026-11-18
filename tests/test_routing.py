"""The routing rules from the proposal, checked on the six-junction test town (see conftest)."""

from __future__ import annotations

import pytest

from saferoute.network import RoadNetwork
from saferoute.routing import coverage, find_route, impact, open_roads, summarize

from .conftest import NODES, build_area


def route(network, start=0, closed=(), status=None, mode="foot", snap_m=0.0):
    return find_route(network, start, closed, status or {}, mode, snap_m)


def test_goes_to_the_nearest_shelter(network):
    result = route(network)
    assert result.route.shelter.id == "sB"
    assert result.route.nodes == [0, 3]
    assert result.route.roads == ["r03"]


def test_avoids_a_closed_road_and_picks_the_next_nearest_shelter(network):
    result = route(network, closed={"r03"})
    assert "r03" not in result.route.roads
    assert result.route.shelter.id == "sA"
    assert result.route.nodes == [0, 1, 2]


def test_never_suggests_an_unreachable_shelter(network):
    # Close the only two roads out of junction 0.
    result = route(network, closed={"r01", "r03"})
    assert result.route is None
    assert all(not option.reachable for option in result.options)


def test_skips_full_and_closed_shelters(network):
    result = route(network, status={"sB": "full"})
    assert result.route.shelter.id == "sA"
    shelter_b = next(o for o in result.options if o.shelter.id == "sB")
    assert shelter_b.reachable and shelter_b.status == "full" and not shelter_b.usable

    result = route(network, status={"sA": "closed", "sB": "closed"})
    assert result.route is None


def test_reports_shelters_cut_off_by_closures(network):
    # Close everything around junction 2 so shelter A is unreachable but B is fine.
    result = route(network, closed={"r12", "r25"})
    shelter_a = next(o for o in result.options if o.shelter.id == "sA")
    assert not shelter_a.reachable and shelter_a.eta_s is None
    assert result.route.shelter.id == "sB"


def test_vehicles_are_faster_than_walking(network):
    walking = route(network, closed={"r03"}, mode="foot").route
    driving = route(network, closed={"r03"}, mode="vehicle").route
    assert driving.eta_s < walking.eta_s
    assert driving.distance_m == pytest.approx(walking.distance_m)


def test_walking_eta_matches_walking_speed(network):
    result = route(network).route
    assert result.eta_s == pytest.approx(result.distance_m / (4.0 / 3.6), rel=1e-6)


def test_the_walk_to_the_first_junction_counts(network):
    plain = route(network).route
    snapped = route(network, snap_m=50).route
    assert snapped.distance_m == pytest.approx(plain.distance_m + 50)
    assert snapped.eta_s > plain.eta_s


def test_steps_merge_consecutive_roads_with_the_same_name(network):
    result = route(network, closed={"r03"}).route
    assert [step["name"] for step in result.steps] == ["Main Road"]
    assert result.steps[0]["length_m"] == round(result.distance_m)


def test_route_geometry_runs_from_start_to_shelter(network):
    # Start at junction 2 and go west, against the stored direction of each road.
    result = route(network, start=2, status={"sA": "closed"}).route
    assert tuple(result.geometry[0]) == NODES[2]
    assert tuple(result.geometry[-1]) == NODES[3]


def test_closures_never_modify_the_network(network):
    edges_before = network.graph.number_of_edges()
    view = open_roads(network, {"r01", "r03"})
    assert view.number_of_edges() == edges_before - 2
    assert network.graph.number_of_edges() == edges_before


def test_coverage_reports_cut_off_junctions(network):
    times = coverage(network, {"r01", "r03"}, {}, "foot")
    assert times[0] is None
    assert all(times[n] is not None for n in (1, 2, 3, 4, 5))
    assert times[2] == 0 and times[3] == 0  # shelters themselves


def test_coverage_with_every_shelter_closed(network):
    times = coverage(network, set(), {"sA": "closed", "sB": "full"}, "foot")
    assert set(times.values()) == {None}


def test_summary_counts(network):
    summary = summarize(coverage(network, {"r01", "r03"}, {}, "foot"))
    assert summary["junctions"] == 6
    assert summary["cut_off"] == 1
    assert summary["cut_off_pct"] == pytest.approx(16.7)
    assert summary["worst_eta_s"] >= summary["p90_eta_s"] >= summary["mean_eta_s"] > 0


def test_impact_compares_a_scenario_with_the_live_map(network):
    result = impact(network, set(), {"r01", "r03"}, {}, (), "foot")
    assert result["before"]["cut_off"] == 0
    assert result["after"]["cut_off"] == 1
    assert result["newly_cut_off"] == 1
    assert result["eta_by_node"]["0"] is None


def test_impact_can_close_shelters_in_a_scenario(network):
    result = impact(network, set(), set(), {}, ["sB"], "foot")
    assert result["slower_by_a_minute_or_more"] >= 1  # junction 3's people now walk to A
    assert result["after"]["cut_off"] == 0


def test_impact_builds_on_live_closures(network):
    result = impact(network, {"r01"}, {"r03"}, {}, (), "foot")
    assert result["before"]["cut_off"] == 0
    assert result["newly_cut_off"] == 1


def test_sensor_watches_roads_within_its_radius(network):
    assert network.sensors["FS-1"].roads == ("r45",)
    wide = RoadNetwork(build_area(), [{**_sensor(), "radius_m": 120}])
    assert set(wide.sensors["FS-1"].roads) >= {"r45", "r14", "r25", "r34"}


def test_tap_far_outside_the_area_is_rejected(network):
    assert network.nearest_node(13.0, 80.0005)[0] in (0, 1)
    assert network.nearest_node(13.1, 80.1) is None


def test_bad_area_data_is_refused():
    area = build_area()
    area["shelters"][0]["node"] = 99
    with pytest.raises(ValueError):
        RoadNetwork(area)
    with pytest.raises(ValueError):
        RoadNetwork(build_area(), [{**_sensor(), "road": "nope"}])


def _sensor():
    return {"id": "FS-1", "road": "r45", "label": "South Street", "lat": 12.999, "lon": 80.0015}
