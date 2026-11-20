"""A rescue request carries how many people need help, and only responders see it."""

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
            "DATABASE": tmp_path / "rescue.db",
        }
    )
    yield app
    app.extensions["saferoute.store"].close()


def test_rescue_request_keeps_the_number_of_people(real_app):
    client = real_app.test_client()
    sent = client.post("/api/reports", json={"lat": 12.98045, "lon": 80.21840, "kind": "rescue", "people": 3})
    assert sent.status_code == 200
    login = client.post("/api/responder/login", json={"name": "NDRF Team 4"})
    assert login.status_code == 200
    reports = client.get("/api/state").json["reports"]
    rescue = next(r for r in reports if r["id"] == sent.json["id"])
    assert rescue["people"] == 3


def test_people_must_be_a_small_whole_number(real_app):
    client = real_app.test_client()
    for bad in (0, 21, 2.5, "many"):
        response = client.post("/api/reports", json={"lat": 12.98045, "lon": 80.21840, "kind": "rescue", "people": bad})
        assert response.status_code == 400, bad
