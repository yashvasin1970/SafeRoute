"""Shared fixtures: a tiny six-junction town that every rule can be checked against by hand.

    0 ----- 1 ----- 2 (shelter A)
    |       |       |
    3 ----- 4 ----- 5
 (shelter B)

Junctions sit about 110 m apart. Road 0-1-2 is "Main Road" (primary); the rest are residential.
Flood sensor FS-1 sits in the middle of road 4-5 and watches only that road (30 m radius).
"""

from __future__ import annotations

import json

import pytest

from saferoute import create_app
from saferoute.network import RoadNetwork, distance_m

NODES = {
    0: (13.000, 80.000),
    1: (13.000, 80.001),
    2: (13.000, 80.002),
    3: (12.999, 80.000),
    4: (12.999, 80.001),
    5: (12.999, 80.002),
}
ROADS = [
    ("r01", 0, 1, "Main Road", "primary"),
    ("r12", 1, 2, "Main Road", "primary"),
    ("r03", 0, 3, "West Lane", "residential"),
    ("r14", 1, 4, "Middle Lane", "residential"),
    ("r25", 2, 5, "East Lane", "residential"),
    ("r34", 3, 4, "South Street", "residential"),
    ("r45", 4, 5, "South Street", "residential"),
]
SENSOR_KEY = "test-device-key"
SMS_KEY = "test-sms-key"


def build_area() -> dict:
    return {
        "area": {"name": "Testville", "center": [12.9995, 80.001], "source": "test", "fetched": "2026-11-18"},
        "nodes": [{"id": n, "lat": lat, "lon": lon} for n, (lat, lon) in NODES.items()],
        "roads": [
            {
                "id": rid,
                "u": u,
                "v": v,
                "name": name,
                "kind": kind,
                "length_m": round(distance_m(NODES[u], NODES[v]), 1),
                "geometry": [list(NODES[u]), list(NODES[v])],
            }
            for rid, u, v, name, kind in ROADS
        ],
        "shelters": [
            {"id": "sA", "name": "Shelter A", "kind": "school", "lat": 13.0001, "lon": 80.002, "node": 2},
            {"id": "sB", "name": "Shelter B", "kind": "community_centre", "lat": 12.9989, "lon": 80.000, "node": 3},
        ],
    }


SENSORS = [{"id": "FS-1", "road": "r45", "label": "South Street", "lat": 12.999, "lon": 80.0015, "radius_m": 30}]


@pytest.fixture
def network() -> RoadNetwork:
    return RoadNetwork(build_area(), SENSORS)


@pytest.fixture
def app(tmp_path):
    area_file = tmp_path / "area.json"
    sensors_file = tmp_path / "sensors.json"
    area_file.write_text(json.dumps(build_area()), encoding="utf-8")
    sensors_file.write_text(json.dumps({"sensors": SENSORS}), encoding="utf-8")
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SENSOR_KEY": SENSOR_KEY,
            "SMS_KEY": SMS_KEY,
            "STORY_STEP_SECONDS": 0,
            "DATABASE": tmp_path / "test.db",
            "AREA_FILE": area_file,
            "SENSORS_FILE": sensors_file,
        }
    )
    yield app
    app.extensions["saferoute.store"].close()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def responder(client):
    """A signed-in responder client. Returns (client, headers with the CSRF token)."""
    response = client.post("/api/responder/login", json={"name": "NDRF Team 4"})
    assert response.status_code == 200
    return client, {"X-CSRF-Token": response.json["csrf"]}
