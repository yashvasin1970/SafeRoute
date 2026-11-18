"""Flood sensor rules and the live-state store."""

from __future__ import annotations

import pytest

from saferoute import sensors
from saferoute.store import Store


@pytest.mark.parametrize(
    ("level", "band"), [(0, "dry"), (14.9, "dry"), (15, "caution"), (29.9, "caution"), (30, "flooded")]
)
def test_bands(level, band):
    assert sensors.band(level) == band


@pytest.mark.parametrize(
    ("level", "armed", "closure", "expected"),
    [
        (35, True, None, ("close", True)),  # rising water closes an open road
        (35, True, "other", (None, True)),  # already closed by a responder: leave it
        (35, True, "self", (None, True)),  # already closed by this sensor
        (35, False, None, (None, False)),  # a responder overrode this sensor: stay quiet
        (20, True, "self", (None, True)),  # between the lines: no flicker
        (20, True, None, (None, True)),
        (10, True, "self", ("reopen", True)),  # water receded: reopen its own closure
        (10, True, "other", (None, True)),  # never reopen a responder's closure
        (10, False, None, (None, True)),  # dry again: re-arm
    ],
)
def test_decide(level, armed, closure, expected):
    assert sensors.decide(level, armed, closure) == expected


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "s.db")
    db.init()
    yield db
    db.close()


def test_every_change_bumps_the_version_and_logs_an_event(store):
    assert store.version() == 0
    assert store.close_road("r1", "Flooded", "responder", "Team A", "Main Road")
    assert not store.close_road("r1", "Fire", "responder", "Team B", "Main Road")  # already closed
    assert store.version() == 1
    assert store.closures()["r1"].reported_by == "Team A"
    assert store.events()[0] == {
        "at": store.events()[0]["at"],
        "actor": "Team A",
        "action": "closed",
        "detail": "Main Road: Flooded",
    }


def test_reopen(store):
    assert store.reopen_road("r1", "Team A", "Main Road") is None
    store.close_road("r1", "Flooded", "responder", "Team A", "Main Road")
    assert store.reopen_road("r1", "Team B", "Main Road").reason == "Flooded"
    assert store.closures() == {}


def test_shelter_status(store):
    assert not store.set_shelter("s1", "open", "Team A", "Hall")  # already open by default
    assert store.set_shelter("s1", "full", "Team A", "Hall")
    assert store.shelter_status() == {"s1": "full"}


def test_sensor_closes_and_reopens_all_its_roads(store):
    roads = ("r1", "r2", "r3")
    assert store.record_reading("FS-1", roads, 12, "Low Street") == {"closed": [], "reopened": []}
    assert store.record_reading("FS-1", roads, 34, "Low Street")["closed"] == ["r1", "r2", "r3"]
    assert {c.source for c in store.closures().values()} == {"sensor:FS-1"}
    assert store.record_reading("FS-1", roads, 40, "Low Street") == {"closed": [], "reopened": []}
    assert store.record_reading("FS-1", roads, 8, "Low Street")["reopened"] == ["r1", "r2", "r3"]
    assert store.closures() == {}


def test_sensor_never_reopens_a_responder_closure(store):
    store.close_road("r2", "Fallen tree", "responder", "Team A", "Low Street")
    changes = store.record_reading("FS-1", ("r1", "r2"), 34, "Low Street")
    assert changes["closed"] == ["r1"]
    store.record_reading("FS-1", ("r1", "r2"), 5, "Low Street")
    assert list(store.closures()) == ["r2"]


def test_responder_override_silences_the_sensor_until_it_dries(store):
    store.record_reading("FS-1", ("r1",), 34, "Low Street")
    store.reopen_road("r1", "Team A", "Low Street")  # e.g. the sensor is faulty
    assert store.sensor_state()["FS-1"]["armed"] is False
    assert store.record_reading("FS-1", ("r1",), 45, "Low Street")["closed"] == []
    store.record_reading("FS-1", ("r1",), 5, "Low Street")  # dries out, re-arms
    assert store.sensor_state()["FS-1"]["armed"] is True
    assert store.record_reading("FS-1", ("r1",), 31, "Low Street")["closed"] == ["r1"]


def test_band_changes_are_logged_but_steady_readings_are_not(store):
    store.record_reading("FS-1", ("r1",), 5, "Low Street")
    store.record_reading("FS-1", ("r1",), 6, "Low Street")
    store.record_reading("FS-1", ("r1",), 18, "Low Street")
    actions = [e["action"] for e in store.events()]
    assert actions == ["water level"]


def test_reset(store):
    store.close_road("r1", "Flooded", "responder", "Team A", "Main Road")
    store.set_shelter("s1", "closed", "Team A", "Hall")
    store.record_reading("FS-1", ("r9",), 50, "Low Street")
    store.reset("Team A")
    assert store.closures() == {} and store.shelter_status() == {} and store.sensor_state() == {}
