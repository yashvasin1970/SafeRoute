"""The new endpoints: crowd reports, arrivals, shelter load, risk in routes, SMS and the story."""

from __future__ import annotations

from .conftest import SMS_KEY

ON_SOUTH_STREET = {"lat": 12.999, "lon": 80.0015}  # middle of r45
NEAR_JUNCTION_0 = {"lat": 13.0, "lon": 80.0001}


def test_state_has_the_live_feeds(client):
    state = client.get("/api/state").json
    for key in ("risk", "reports", "weather", "story", "shelters"):
        assert key in state
    assert state["weather"]["source"] == "unavailable"  # tests never call the weather service


def test_route_validates_preference_and_explains_itself(client):
    assert client.post("/api/route", json={**NEAR_JUNCTION_0, "prefer": "scenic"}).status_code == 400
    data = client.post("/api/route", json=NEAR_JUNCTION_0).json
    assert data["prefer"] == "safest"
    for key in ("risky", "avoided", "fastest_eta_s", "balanced_from"):
        assert key in data["route"]
    assert {"capacity", "occupancy", "en_route", "load_pct"} <= set(data["options"][0])


def test_resident_flood_report_makes_the_road_risky_but_not_closed(client):
    made = client.post("/api/reports", json={**ON_SOUTH_STREET, "kind": "flooding", "note": "Ankle deep"})
    assert made.status_code == 200 and made.json["place"] == "South Street"
    state = client.get("/api/state").json
    assert state["closures"] == []
    assert state["risk"]["r45"]["reasons"][0] == {"type": "report", "place": "flooding", "value": None}
    public = state["reports"][0]
    assert public["kind"] == "flooding" and "note" not in public  # notes are for responders only


def test_rescue_requests_are_only_shown_to_responders(client, responder):
    client.post("/api/reports", json={**ON_SOUTH_STREET, "kind": "rescue", "note": "Two people on the roof"})
    signed_in, _ = responder
    reports = signed_in.get("/api/state").json["reports"]
    assert reports[0]["kind"] == "rescue" and reports[0]["note"] == "Two people on the roof"


def test_public_does_not_see_rescue_requests(app):
    reporter, viewer = app.test_client(), app.test_client()
    reporter.post("/api/reports", json={**ON_SOUTH_STREET, "kind": "rescue", "note": "help"})
    assert viewer.get("/api/state").json["reports"] == []


def test_report_validation_and_rate_limit(client):
    assert client.post("/api/reports", json={**ON_SOUTH_STREET, "kind": "aliens"}).status_code == 400
    assert client.post("/api/reports", json={"lat": 13.5, "lon": 80.5, "kind": "flooding"}).status_code == 422
    assert (
        client.post("/api/reports", json={**ON_SOUTH_STREET, "kind": "blocked", "note": "x" * 201}).status_code == 400
    )
    codes = [client.post("/api/reports", json={**ON_SOUTH_STREET, "kind": "blocked"}).status_code for _ in range(7)]
    assert codes[-1] == 429 and codes.count(200) == 5  # one of the six allowed went to the 422 above


def test_responders_verify_or_dismiss_reports(app, client, responder):
    first = client.post("/api/reports", json={**ON_SOUTH_STREET, "kind": "flooding"}).json["id"]
    second = client.post("/api/reports", json={**ON_SOUTH_STREET, "kind": "blocked"}).json["id"]
    signed_in, headers = responder
    assert app.test_client().post(f"/api/reports/{first}/verify").status_code == 401  # needs a responder
    assert signed_in.post(f"/api/reports/{first}/verify", headers=headers).json["changed"] is True
    assert signed_in.post(f"/api/reports/{second}/dismiss", headers=headers).json["changed"] is True
    assert signed_in.post("/api/reports/999/verify", headers=headers).status_code == 404
    state = signed_in.get("/api/state").json
    assert [c["road"] for c in state["closures"]] == ["r45"] and state["reports"] == []


def test_arrivals_spread_people_and_are_limited(client, responder):
    assert client.post("/api/arrivals", json={"shelter": "sB", "people": 4}).status_code == 200
    assert client.get("/api/state").json["shelters"]["sB"]["en_route"] == 4
    assert client.post("/api/arrivals", json={"shelter": "zz"}).status_code == 404
    assert client.post("/api/arrivals", json={"shelter": "sB", "people": 0}).status_code == 400
    assert client.post("/api/arrivals", json={"shelter": "sB", "people": 2.5}).status_code == 400
    client.post("/api/arrivals", json={"shelter": "sB"})
    client.post("/api/arrivals", json={"shelter": "sB"})
    too_many = client.post("/api/arrivals", json={"shelter": "sB"})
    assert too_many.status_code == 429 and too_many.json["code"] == "too_many_trips"

    signed_in, headers = responder
    signed_in.post("/api/shelters/sA", json={"occupancy": 200}, headers=headers)
    full = client.post("/api/arrivals", json={"shelter": "sA"})
    assert full.status_code == 409 and full.json["code"] == "shelter_unavailable"


def test_occupancy_drives_load_balancing(client, responder):
    signed_in, headers = responder
    assert signed_in.post("/api/shelters/sB", json={"occupancy": 190}, headers=headers).json["changed"]
    route = client.post("/api/route", json=NEAR_JUNCTION_0).json["route"]
    assert route["shelter"]["id"] == "sA" and route["balanced_from"]["load_pct"] == 95

    signed_in.post("/api/shelters/sB", json={"occupancy": 200}, headers=headers)
    assert client.get("/api/state").json["shelters"]["sB"]["status"] == "full"
    assert signed_in.post("/api/shelters/sB", json={"occupancy": 1000}, headers=headers).status_code == 400
    assert signed_in.post("/api/shelters/sB", json={}, headers=headers).status_code == 400


def test_sms_simulator_in_three_languages(client):
    english = client.post("/api/sms/simulate", json={"text": "ROUTE west lane"}).json
    assert english["lang"] == "en" and english["reply"].startswith("SafeRoute: Walk to Shelter B")
    tamil = client.post("/api/sms/simulate", json={"text": "வழி West Lane"}).json
    assert tamil["lang"] == "ta" and "Shelter B" in tamil["reply"]
    help_reply = client.post("/api/sms/simulate", json={"text": "HELP south street"}).json["reply"]
    assert "Rescue request logged near South Street" in help_reply
    assert client.post("/api/sms/simulate", json={"text": ""}).status_code == 400


def test_sms_gateway_needs_its_key_and_speaks_twiml(client):
    assert client.post("/api/sms/inbound", json={"text": "STATUS"}).status_code == 401
    as_json = client.post("/api/sms/inbound", json={"text": "STATUS"}, headers={"X-Gateway-Key": SMS_KEY})
    assert as_json.json["reply"].startswith("SafeRoute: 0 road segments blocked")
    as_form = client.post(
        "/api/sms/inbound", data={"Body": "SHELTERS", "From": "+910000"}, headers={"X-Gateway-Key": SMS_KEY}
    )
    assert as_form.mimetype == "text/xml" and b"<Response><Message>SafeRoute shelters" in as_form.data


def test_story_needs_a_responder_and_runs(app, client, responder):
    assert app.test_client().post("/api/story/start").status_code == 401
    signed_in, headers = responder
    assert signed_in.post("/api/story/start", headers=headers).json["started"] is True
    app.extensions["saferoute.story"]._thread.join(timeout=30)
    story = signed_in.get("/api/state").json["story"]
    assert story["step"] == story["total"]  # the test map lacks the story's sensors; it still plays through
    assert signed_in.post("/api/story/stop", headers=headers).json["stopped"] is True
    assert signed_in.get("/api/state").json["story"] is None
