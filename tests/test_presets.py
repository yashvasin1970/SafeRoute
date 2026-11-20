"""Responder test cases: one click sets up a known situation on the real Velachery map."""

import pytest

from saferoute import create_app


@pytest.fixture
def real_app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SENSOR_KEY": "k",
            "SMS_KEY": "s",
            "DATABASE": tmp_path / "presets.db",
        }
    )
    yield app
    app.extensions["saferoute.store"].close()


@pytest.fixture
def signed_in(real_app):
    client = real_app.test_client()
    login = client.post("/api/responder/login", json={"name": "NDRF Team 4"})
    assert login.status_code == 200
    return client, {"X-CSRF-Token": login.json["csrf"]}


def closed_roads(client) -> set[str]:
    return {c["road"] for c in client.get("/api/state").json["closures"]}


def test_lists_every_preset(real_app):
    presets = real_app.test_client().get("/api/scenario/presets").json["presets"]
    assert presets == ["calm", "rain", "closure", "shelter_full", "rescue", "rescue_group", "all_blocked"]


def test_all_blocked_preset_leaves_no_safe_route(signed_in):
    client, headers = signed_in
    assert client.post("/api/scenario/preset/all_blocked", json={}, headers=headers).status_code == 200
    assert len(closed_roads(client)) > 100  # every road on the map is closed
    route = client.post("/api/route", json={"lat": 12.98, "lon": 80.22, "mode": "foot", "prefer": "safest"})
    assert route.status_code == 200 and route.json["route"] is None


def test_closure_preset_closes_the_sensor_road(signed_in):
    client, headers = signed_in
    assert client.post("/api/scenario/preset/closure", json={}, headers=headers).status_code == 200
    assert "r281" in closed_roads(client)  # Velachery Main Road, where FS-01 sits


def test_calm_preset_clears_everything(signed_in):
    client, headers = signed_in
    client.post("/api/scenario/preset/closure", json={}, headers=headers)
    assert client.post("/api/scenario/preset/calm", json={}, headers=headers).status_code == 200
    assert closed_roads(client) == set()


def test_rain_preset_warns_without_closing(signed_in):
    client, headers = signed_in
    assert client.post("/api/scenario/preset/rain", json={}, headers=headers).status_code == 200
    assert closed_roads(client) == set()


def test_rescue_preset_adds_a_report(signed_in):
    client, headers = signed_in
    client.post("/api/scenario/preset/rescue", json={}, headers=headers)
    kinds = [r["kind"] for r in client.get("/api/state").json.get("reports", [])]
    assert "rescue" in kinds


def test_unknown_preset_is_refused(signed_in):
    client, headers = signed_in
    response = client.post("/api/scenario/preset/nope", json={}, headers=headers)
    assert response.status_code == 404


def test_presets_need_a_responder(real_app):
    client = real_app.test_client()
    assert client.post("/api/scenario/preset/calm", json={}).status_code == 401


def test_rescue_group_preset_counts_four_people(signed_in):
    client, headers = signed_in
    assert client.post("/api/scenario/preset/rescue_group", json={}, headers=headers).status_code == 200
    reports = client.get("/api/state").json["reports"]
    group = [r for r in reports if r["kind"] == "rescue" and r["people"] == 4]
    assert len(group) == 1
