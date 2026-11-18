"""Checks on the real Velachery sample built from OpenStreetMap."""

from __future__ import annotations

from saferoute import DEFAULT_AREA, DEFAULT_SENSORS
from saferoute.network import RoadNetwork
from saferoute.routing import coverage


def test_sample_area_loads_and_every_junction_can_reach_a_shelter():
    network = RoadNetwork.load(DEFAULT_AREA, DEFAULT_SENSORS)
    assert len(network.roads) > 1000 and len(network.shelters) == 5
    times = coverage(network, set(), {}, "foot")
    assert all(t is not None for t in times.values())
    # Nobody in the sample area is more than half an hour's walk from a shelter.
    assert max(times.values()) < 30 * 60


def test_every_sensor_watches_its_own_road_and_its_surroundings():
    network = RoadNetwork.load(DEFAULT_AREA, DEFAULT_SENSORS)
    assert len(network.sensors) == 4
    for sensor in network.sensors.values():
        assert sensor.road in sensor.roads
        assert len(sensor.roads) >= 2
