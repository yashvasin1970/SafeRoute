"""Past cyclones and floods: data loads, roads get a small caution, and the record is served."""

import pytest

from saferoute import DEFAULT_AREA, DEFAULT_HISTORY, DEFAULT_SENSORS, DEFAULT_SHELTERS, create_app
from saferoute.network import RoadNetwork
from saferoute.risk import HISTORY, road_risk


@pytest.fixture
def network():
    return RoadNetwork.load(DEFAULT_AREA, DEFAULT_SENSORS, DEFAULT_SHELTERS, DEFAULT_HISTORY)


def test_velachery_main_road_has_a_flood_record(network):
    assert network.history_roads
    assert set(network.history_roads.values()) == {"Velachery Main Road"}
    for road_id in network.history_roads:
        assert network.roads[road_id].name == "Velachery Main Road"


def test_past_flooding_is_a_small_caution_not_a_closure(network):
    road_id = next(iter(network.history_roads))
    risk = road_risk(network, {}, [], None, history=network.history_roads)
    assert risk[road_id].factor == HISTORY
    assert risk[road_id].reasons[0].type == "history"
    assert risk[road_id].reasons[0].place == "Velachery Main Road"


def test_past_flooding_skipped_for_closed_roads(network):
    road_id = next(iter(network.history_roads))
    risk = road_risk(network, {}, [], None, closed={road_id}, history=network.history_roads)
    assert road_id not in risk


def test_missing_history_file_is_harmless(tmp_path):
    network = RoadNetwork.load(DEFAULT_AREA, DEFAULT_SENSORS, DEFAULT_SHELTERS, tmp_path / "none.json")
    assert network.history_roads == {}


def test_history_endpoint_serves_the_events(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SENSOR_KEY": "k",
            "SMS_KEY": "s",
            "DATABASE": tmp_path / "history.db",
        }
    )
    try:
        body = app.test_client().get("/api/history").json
        names = [event["name"] for event in body["events"]]
        assert any("2015" in name for name in names)
        assert any("Michaung" in name for name in names)
        for event in body["events"]:
            assert event["sources"], f"{event['id']} needs a source"
    finally:
        app.extensions["saferoute.store"].close()
