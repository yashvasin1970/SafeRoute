"""Shelter check-in with a live headcount, and the one-click flood scenario."""

from __future__ import annotations

from saferoute import DEFAULT_AREA, DEFAULT_SENSORS, DEFAULT_SHELTERS
from saferoute.feeds import WeatherFeed
from saferoute.network import RoadNetwork
from saferoute.store import Store
from saferoute.story import StoryRunner

NEAR_JUNCTION_0 = {"lat": 13.0, "lon": 80.0001}


def test_check_in_raises_the_headcount_and_counts_people_safe(client, responder):
    client.post("/api/arrivals", json={"shelter": "sB", "people": 3})
    arrived = client.post("/api/checkin", json={"shelter": "sB", "people": 3}).json
    assert arrived["inside"] == 3 and not arrived["full"]
    state = client.get("/api/state").json["shelters"]["sB"]
    assert state["occupancy"] == 3 and state["en_route"] == 0  # the trip became a headcount

    signed_in, _ = responder
    numbers = signed_in.get("/api/stats").json
    assert numbers["safe"]["at_shelters"] == {"sB": 1}


def test_a_shelter_fills_itself_and_new_arrivals_are_redirected(app, client, responder):
    signed_in, headers = responder
    signed_in.post("/api/shelters/sB", json={"occupancy": 195}, headers=headers)
    others = [app.test_client() for _ in range(2)]
    others[0].post("/api/checkin", json={"shelter": "sB", "people": 4})
    last = others[1].post("/api/checkin", json={"shelter": "sB", "people": 1}).json
    assert last == {**last, "inside": 200, "full": True}
    assert client.get("/api/state").json["shelters"]["sB"]["status"] == "full"
    assert client.post("/api/route", json=NEAR_JUNCTION_0).json["route"]["shelter"]["id"] == "sA"


def test_check_in_validation(client):
    assert client.post("/api/checkin", json={"shelter": "zz"}).status_code == 404
    assert client.post("/api/checkin", json={"shelter": "sA", "people": 0}).status_code == 400
    assert client.post("/api/checkin", json={"shelter": "sA", "people": 21}).status_code == 400


def test_flood_scenario_needs_a_responder(app, responder):
    assert app.test_client().post("/api/scenario/flood").status_code == 401
    signed_in, headers = responder
    assert signed_in.post("/api/scenario/flood", headers=headers).json["loaded"] is True
    assert signed_in.get("/api/scenario/flood/roads").json == {"roads": []}  # the test town has no such sensors


def test_flood_scenario_on_the_real_map(tmp_path):
    network = RoadNetwork.load(DEFAULT_AREA, DEFAULT_SENSORS, DEFAULT_SHELTERS)
    db = Store(tmp_path / "scenario.db")
    db.init()
    runner = StoryRunner(db, network, WeatherFeed(db, 12.98, 80.22, enabled=False), 0, "secret")
    runner.flood_scenario("Tester")
    closed = db.closures()
    expected = set(runner.flood_roads())
    assert expected and expected <= set(closed)  # the three flooded stretches are closed
    assert db.feed("weather_drill")["level"] == "extreme"
    assert {r["kind"] for r in db.reports()} == {"flooding", "rescue"}
    assert db.occupancy()["s2"] == 240
    db.close()
