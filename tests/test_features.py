"""Risk-weighted routing, shelter load balancing, crowd reports, weather, SMS and the flood story."""

from __future__ import annotations

import pytest

from saferoute import DEFAULT_AREA, DEFAULT_SENSORS, DEFAULT_SHELTERS
from saferoute.feeds import WeatherFeed, parse_open_meteo, rain_level
from saferoute.network import RoadNetwork
from saferoute.reports import dismiss_report, make_report, verify_report
from saferoute.risk import MAX_RISK, road_risk
from saferoute.routing import effective_status, find_route, load_penalty_s
from saferoute.sms import SmsServices, find_street, handle, parse
from saferoute.store import Store
from saferoute.story import STEPS, StoryRunner


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "f.db")
    db.init()
    yield db
    db.close()


FLOOD_REPORT_ON_R45 = {"kind": "flooding", "roads": ["r45"], "status": "open"}


# ---- risk ------------------------------------------------------------------------------------


def test_risk_sources_add_up_and_are_explained(network):
    risk = road_risk(network, {"FS-1": 22}, [FLOOD_REPORT_ON_R45], "heavy")
    r45 = risk["r45"]
    assert r45.factor == pytest.approx(1.0 + 1.5 + 0.5)
    assert {reason.type for reason in r45.reasons} == {"rising", "report", "rain"}
    assert set(risk) == {"r45"}  # nothing else is near the sensor or the report


def test_risk_ignores_closed_roads_rescue_reports_and_dry_weather(network):
    assert road_risk(network, {"FS-1": 22}, [], None, closed={"r45"}) == {}
    assert road_risk(network, {}, [{"kind": "rescue", "roads": ["r45"], "status": "open"}], "light") == {}
    assert road_risk(network, {"FS-1": 5}, [{**FLOOD_REPORT_ON_R45, "status": "dismissed"}], "moderate") == {}


def test_risk_is_capped(network):
    many = [FLOOD_REPORT_ON_R45] * 5
    assert road_risk(network, {"FS-1": 22}, many, "extreme")["r45"].factor == MAX_RISK


def test_safest_route_avoids_risky_roads_and_says_what_it_cost(network):
    # From junction 5 to shelter B (A closed), the fast way runs along r45.
    risk = road_risk(network, {"FS-1": 22}, [FLOOD_REPORT_ON_R45], None)
    status = {"sA": "closed"}
    fastest = find_route(network, 5, (), status, "foot", risk=risk, prefer="fastest").route
    safest = find_route(network, 5, (), status, "foot", risk=risk, prefer="safest").route
    assert "r45" in fastest.roads and fastest.risky[0]["road"] == "r45"
    assert "r45" not in safest.roads and safest.risky == []
    assert safest.eta_s > safest.fastest_eta_s == pytest.approx(fastest.eta_s)
    assert {a["type"] for a in safest.avoided} == {"rising", "report"}


def test_mild_risk_is_worth_a_short_detour_only(network):
    risk = road_risk(network, {"FS-1": 22}, [], None)  # rising water only (x2 cost)
    route = find_route(network, 5, (), {"sA": "closed"}, "foot", risk=risk, prefer="safest").route
    assert "r45" in route.roads  # the detour would double the trip, so it keeps r45 ...
    assert route.risky[0]["reasons"][0]["type"] == "rising"  # ... and warns about it


def test_eta_is_real_travel_time_not_the_risk_weighted_cost(network):
    risk = road_risk(network, {"FS-1": 22}, [FLOOD_REPORT_ON_R45] * 3, None)
    route = find_route(network, 5, (), {"sA": "closed"}, "foot", risk=risk, prefer="safest").route
    assert route.eta_s == pytest.approx(route.distance_m / (4.0 / 3.6))


# ---- shelter load ----------------------------------------------------------------------------


def test_load_penalty_curve():
    assert load_penalty_s(0.5) == 0
    assert load_penalty_s(0.7) == 0
    assert load_penalty_s(1.0) == pytest.approx(600)
    assert load_penalty_s(3.0) == 900


def test_effective_status():
    assert effective_status("open", 199, 200) == "open"
    assert effective_status("open", 200, 200) == "full"
    assert effective_status("closed", 0, 200) == "closed"


def test_nearly_full_shelter_sends_people_to_one_with_space(network):
    quiet = find_route(network, 0, (), {}, "foot").route
    assert quiet.shelter.id == "sB" and quiet.balanced_from is None

    busy = find_route(network, 0, (), {}, "foot", occupancy={"sB": 190}).route
    assert busy.shelter.id == "sA"
    assert busy.balanced_from["shelter"]["id"] == "sB"
    assert busy.balanced_from["load_pct"] == 95
    assert busy.balanced_from["extra_s"] > 0


def test_people_on_their_way_count_towards_load(network):
    route = find_route(network, 0, (), {}, "foot", occupancy={"sB": 100}, en_route={"sB": 90}).route
    assert route.shelter.id == "sA"


def test_a_full_shelter_is_never_suggested(network):
    result = find_route(network, 0, (), {"sB": "full"}, "foot", occupancy={"sB": 200})
    assert result.route.shelter.id == "sA"
    shelter_b = next(o for o in result.options if o.shelter.id == "sB")
    assert not shelter_b.usable and shelter_b.load == 1.0


def test_capacities_come_from_the_shelters_file():
    network = RoadNetwork.load(DEFAULT_AREA, DEFAULT_SENSORS, DEFAULT_SHELTERS)
    assert network.shelters["s2"].capacity == 300
    assert "capacity" in network.client_payload()["shelters"][0]


# ---- store additions -------------------------------------------------------------------------


def test_occupancy_and_arrivals(store):
    assert store.set_occupancy("s1", 40, "Team", "Hall", 100)
    assert not store.set_occupancy("s1", 40, "Team", "Hall", 100)
    assert store.occupancy() == {"s1": 40}

    for _ in range(3):
        assert store.add_arrival("s1", 2, "client-a", minutes=30)
    assert not store.add_arrival("s1", 2, "client-a", minutes=30)  # three trips per client
    assert store.add_arrival("s1", 5, "client-b", minutes=-1)  # already expired
    assert store.en_route() == {"s1": 6}


def test_reports_and_feeds_survive_until_reset_except_weather(store):
    report_id = store.add_report(
        {"kind": "flooding", "lat": 1, "lon": 2, "place": "Main Road", "source": "app", "client": "c", "roads": ["r1"]},
        "Resident",
    )
    assert store.report(report_id)["roads"] == ["r1"]
    store.set_feed("weather", {"level": "heavy"})
    store.set_feed("story", {"step": 1})
    store.reset("Team")
    assert store.reports(None) == [] and store.feed("story") is None
    assert store.feed("weather") == {"level": "heavy"}


# ---- crowd reports ---------------------------------------------------------------------------


def test_make_report_snaps_to_the_nearest_road(network):
    report = make_report(network, "flooding", 12.999, 80.0015, "  knee\x00 deep  water ", "app", "c")
    assert report["road_id"] == "r45" and report["place"] == "South Street"
    assert "r45" in report["roads"]
    assert report["note"] == "knee deep water"
    assert make_report(network, "rescue", 12.999, 80.0015, "", "app", "c")["roads"] == []
    assert make_report(network, "flooding", 13.5, 80.5, "", "app", "c") is None


def test_verifying_a_flood_report_closes_the_road(network, store):
    report_id = store.add_report(make_report(network, "flooding", 12.999, 80.0015, "", "app", "c"), "Resident")
    assert verify_report(store, network, store.report(report_id), "Team")
    assert "verified report" in store.closures()["r45"].reason
    assert not verify_report(store, network, store.report(report_id), "Team")  # only once


def test_dismissing_a_report_closes_nothing(network, store):
    report_id = store.add_report(make_report(network, "blocked", 12.999, 80.0015, "", "app", "c"), "Resident")
    assert dismiss_report(store, store.report(report_id), "Team")
    assert store.closures() == {} and store.reports("open") == []


# ---- weather ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mm", "level"), [(0, "dry"), (0.5, "light"), (7.5, "moderate"), (20, "heavy"), (49.9, "heavy"), (50, "extreme")]
)
def test_rain_levels(mm, level):
    assert rain_level(mm) == level


def test_parse_open_meteo():
    reading = parse_open_meteo({"current": {"precipitation": 1.24}, "hourly": {"precipitation": [2, 3, 4, 9]}})
    assert reading["next3h_mm"] == 9.0 and reading["level"] == "moderate" and reading["rain_now_mm"] == 1.2


def test_weather_feed_refresh_drill_and_failures(store):
    readings = iter([{"level": "light", "next3h_mm": 2, "rain_now_mm": 0}, RuntimeError("offline")])

    def fake_fetch(lat, lon):
        item = next(readings)
        if isinstance(item, Exception):
            raise item
        return {"source": "open-meteo", **item}

    feed = WeatherFeed(store, 13.0, 80.0, enabled=True, fetch=fake_fetch)
    assert feed.refresh()["level"] == "light"
    assert feed.refresh() is None  # service down: keep the last reading
    assert feed.current()["level"] == "light"

    feed.start_drill(45, 12, "Story")
    assert feed.current()["source"] == "drill" and feed.current()["level"] == "heavy"
    feed.stop_drill()
    assert feed.current()["level"] == "light"


def test_disabled_weather_feed_says_unavailable(store):
    assert WeatherFeed(store, 13.0, 80.0, enabled=False).current() == {"source": "unavailable", "level": None}


# ---- SMS -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ROUTE Main Road", ("route", "en", "Main Road")),
        ("route  west lane", ("route", "en", "west lane")),
        ("வழி Main Road", ("route", "ta", "Main Road")),
        ("TA SHELTERS", ("shelters", "ta", "")),
        ("मदद East Lane", ("help", "hi", "East Lane")),
        ("बाढ़ South Street", ("flood", "hi", "South Street")),
        ("status", ("status", "en", "")),
        ("hello there", (None, "en", "hello there")),
    ],
)
def test_parse_sms(text, expected):
    assert parse(text) == expected


@pytest.mark.parametrize(
    ("query", "street"),
    [("main rd", "Main Road"), ("SOUTH ST", "South Street"), ("mian road", "Main Road"), ("west", "West Lane")],
)
def test_street_matching_is_forgiving(network, query, street):
    assert find_street(network, query) == street


def test_unknown_street(network):
    assert find_street(network, "Marina Beach") is None


def fake_services(network, route=None):
    made = []
    return SmsServices(
        network=network,
        route=lambda lat, lon: {"route": route},
        report=lambda kind, lat, lon, note, street: made.append((kind, street)) or 7,
        shelters=lambda: [
            {"name": "Shelter A", "status": "open", "load_pct": 40},
            {"name": "Shelter B", "status": "full", "load_pct": 100},
        ],
        status=lambda: {"closed": 3, "rain_level": "heavy"},
    ), made


ROUTE = {
    "shelter": {"name": "Shelter B"},
    "eta_s": 200,
    "distance_m": 220,
    "steps": [{"name": "West Lane"}, {"name": "Unnamed road"}],
}


def test_sms_route_reply_in_three_languages(network):
    services, _ = fake_services(network, ROUTE)
    english, lang = handle("ROUTE west lane", services)
    assert lang == "en" and english.startswith("SafeRoute: Walk to Shelter B, 3 min (220 m). Way: West Lane.")
    tamil, lang = handle("வழி west lane", services)
    assert lang == "ta" and "Shelter B" in tamil and "நிமிடம்" in tamil
    hindi, lang = handle("रास्ता west lane", services)
    assert lang == "hi" and "मिनट" in hindi


def test_sms_no_route_help_flood_shelters_status(network):
    services, made = fake_services(network, None)
    assert "No open shelter can be reached from West Lane" in handle("ROUTE West Lane", services)[0]
    assert "#7" in handle("HELP South Street", services)[0]
    assert "#7" in handle("FLOOD main rd", services)[0]
    assert made == [("rescue", "South Street"), ("flooding", "Main Road")]
    assert handle("SHELTERS", services)[0] == (
        "SafeRoute shelters: Shelter A 40% full; Shelter B full. Reply ROUTE <your street> for directions."
    )
    assert "3 road segments blocked. Rain: heavy" in handle("status", services)[0]
    assert "Couldn't find 'Marina'" in handle("ROUTE Marina", services)[0]
    assert handle("ROUTE", services)[0].startswith("SafeRoute: Send ROUTE")
    assert handle("what", services)[0].startswith("SafeRoute: Send ROUTE")


# ---- the flood story -------------------------------------------------------------------------


def test_the_flood_story_drives_the_real_system(tmp_path):
    network = RoadNetwork.load(DEFAULT_AREA, DEFAULT_SENSORS, DEFAULT_SHELTERS)
    db = Store(tmp_path / "story.db")
    db.init()
    feed = WeatherFeed(db, 12.98, 80.22, enabled=False)
    seen = []

    def watch(_seconds):
        seen.append({"closed": len(db.closures()), "rain": (db.feed("weather_drill") or {}).get("level")})
        return False

    assert StoryRunner(db, network, feed, 0, "secret").start("Tester", sync=True, sleep=watch)
    story = db.feed("story")
    assert story["step"] == story["total"] == len(STEPS) and not story["active"]
    assert set(story["text"]) == {"en", "ta", "hi"}

    assert seen[0]["rain"] == "heavy"  # step 1: heavy rain warning
    assert max(s["closed"] for s in seen) > 10  # sensors closed whole stretches mid-story
    remaining = db.closures().values()
    assert not any(c.source.startswith("sensor:") for c in remaining)  # water receded: sensors reopened
    assert any("verified report" in c.reason for c in remaining)  # the verified report stays closed
    assert db.occupancy()["s1"] == 150  # Akshaya full
    kinds = {(r["kind"], r["status"]) for r in db.reports(None)}
    assert ("flooding", "verified") in kinds and ("rescue", "open") in kinds
    assert db.feed("weather_drill") is None  # the drill ends with the story
    db.close()
