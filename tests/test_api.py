"""The HTTP API: residents, responders, planners and sensors, including the security checks."""

from __future__ import annotations

import json

from saferoute import create_app

from .conftest import SENSOR_KEY, build_area

NEAR_JUNCTION_0 = {"lat": 13.0, "lon": 80.0001}


def test_page_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"SafeRoute" in response.data
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_area_is_revalidated_with_an_etag(client):
    first = client.get("/api/area")
    assert first.status_code == 200
    assert {s["id"] for s in first.json["shelters"]} == {"sA", "sB"}
    again = client.get("/api/area", headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304


def test_route(client):
    response = client.post("/api/route", json={**NEAR_JUNCTION_0, "mode": "foot"})
    assert response.status_code == 200
    data = response.json
    assert data["start"]["node"] == 0
    assert data["route"]["shelter"]["id"] == "sB"
    assert [o["id"] for o in data["options"]] == ["sB", "sA"]


def test_route_input_checks(client):
    assert client.post("/api/route", data="lat=1").status_code == 400  # not JSON
    assert client.post("/api/route", json={"lat": "x", "lon": 80}).status_code == 400
    assert client.post("/api/route", json={"lat": True, "lon": 80}).status_code == 400
    assert client.post("/api/route", json={**NEAR_JUNCTION_0, "mode": "boat"}).status_code == 400
    far = client.post("/api/route", json={"lat": 13.5, "lon": 80.5})
    assert far.status_code == 422 and "outside the mapped area" in far.json["error"]


def test_state_polling_returns_204_when_nothing_changed(client):
    state = client.get("/api/state").json
    assert state["closures"] == []
    assert {sid: s["status"] for sid, s in state["shelters"].items()} == {"sA": "open", "sB": "open"}
    assert state["shelters"]["sA"] == {
        "status": "open",
        "set_status": "open",
        "capacity": 200,
        "occupancy": 0,
        "en_route": 0,
    }
    assert client.get(f"/api/state?since={state['version']}").status_code == 204
    assert client.get("/api/state").headers["Cache-Control"] == "no-store"


def test_changing_the_map_requires_a_responder(client):
    assert client.post("/api/closures", json={"road": "r03", "reason": "Flooded"}).status_code == 401
    assert client.delete("/api/closures/r03").status_code == 401
    assert client.post("/api/shelters/sA", json={"status": "full"}).status_code == 401
    assert client.post("/api/reset", json={}).status_code == 401


def test_responder_changes_need_the_csrf_token(responder):
    client, _ = responder
    closure = {"road": "r03", "reason": "Flooded"}
    assert client.post("/api/closures", json=closure).status_code == 403
    assert client.post("/api/closures", json=closure, headers={"X-CSRF-Token": "wrong"}).status_code == 403


def test_wrong_name_and_rate_limit(app, client):
    app.config["RESPONDER_ID"] = "admin"  # sign-in limited to one name, so a wrong name is refused
    for _ in range(5):
        response = client.post("/api/responder/login", json={"name": "Someone"})
        assert response.status_code == 401 and response.json["code"] == "name_not_allowed"
    blocked = client.post("/api/responder/login", json={"name": "admin"})
    assert blocked.status_code == 429  # even the right name waits once blocked


def test_login_needs_a_name(client):
    response = client.post("/api/responder/login", json={"name": ""})
    assert response.status_code == 400


def test_closing_a_road_reroutes_everyone(responder):
    client, headers = responder
    before = client.post("/api/route", json=NEAR_JUNCTION_0).json
    assert before["route"]["shelter"]["id"] == "sB"

    response = client.post("/api/closures", json={"road": "r03", "reason": "Flooded"}, headers=headers)
    assert response.json["changed"] is True
    after = client.post("/api/route", json=NEAR_JUNCTION_0).json
    assert "r03" not in after["route"]["roads"]
    assert after["route"]["shelter"]["id"] == "sA"

    state = client.get("/api/state").json
    assert state["closures"][0]["reported_by"] == "NDRF Team 4"
    assert state["events"][0]["detail"] == "West Lane: Flooded"

    assert client.delete("/api/closures/r03", headers=headers).json["changed"] is True
    assert client.post("/api/route", json=NEAR_JUNCTION_0).json["route"]["shelter"]["id"] == "sB"


def test_closure_input_checks(responder):
    client, headers = responder
    assert client.post("/api/closures", json={"road": "nope", "reason": "Flooded"}, headers=headers).status_code == 400
    assert client.post("/api/closures", json={"road": "r03", "reason": "Aliens"}, headers=headers).status_code == 400
    assert client.delete("/api/closures/nope", headers=headers).status_code == 404


def test_shelter_status(responder):
    client, headers = responder
    assert client.post("/api/shelters/sB", json={"status": "full"}, headers=headers).json["changed"] is True
    route = client.post("/api/route", json=NEAR_JUNCTION_0).json
    assert route["route"]["shelter"]["id"] == "sA"
    assert client.post("/api/shelters/sB", json={"status": "gone"}, headers=headers).status_code == 400
    assert client.post("/api/shelters/zz", json={"status": "open"}, headers=headers).status_code == 404


def test_logout_ends_responder_access(responder):
    client, headers = responder
    client.post("/api/responder/logout", json={})
    assert client.post("/api/closures", json={"road": "r03", "reason": "Flooded"}, headers=headers).status_code == 401


def test_reset(responder):
    client, headers = responder
    client.post("/api/closures", json={"road": "r03", "reason": "Flooded"}, headers=headers)
    client.post("/api/reset", json={}, headers=headers)
    assert client.get("/api/state").json["closures"] == []


def test_planner_what_if_publishes_nothing(client):
    response = client.post("/api/impact", json={"mode": "foot", "closures": ["r01", "r03"], "shelters_closed": []})
    assert response.status_code == 200
    assert response.json["newly_cut_off"] == 1
    assert client.get("/api/state").json["closures"] == []


def test_planner_input_checks(client):
    assert client.post("/api/impact", json={"closures": ["nope"]}).status_code == 400
    assert client.post("/api/impact", json={"closures": [1, 2]}).status_code == 400
    assert client.post("/api/impact", json={"closures": "r01"}).status_code == 400
    assert client.post("/api/impact", json={"shelters_closed": ["zz"]}).status_code == 400
    assert client.post("/api/impact", json={"closures": ["r01"] * 301}).status_code == 400


def test_sensor_reading_closes_and_reopens_its_roads(client):
    key = {"X-Device-Key": SENSOR_KEY}
    rising = client.post("/api/sensors/FS-1/readings", json={"level_cm": 36}, headers=key)
    assert rising.json == {"band": "flooded", "closed": 1, "reopened": 0, "version": rising.json["version"]}
    state = client.get("/api/state").json
    assert state["closures"][0]["source"] == "sensor:FS-1"
    assert state["sensors"]["FS-1"]["band"] == "flooded"

    falling = client.post("/api/sensors/FS-1/readings", json={"level_cm": 9}, headers=key)
    assert falling.json["reopened"] == 1
    assert client.get("/api/state").json["closures"] == []


def test_sensor_authentication_and_input(client):
    reading = {"level_cm": 40}
    assert client.post("/api/sensors/FS-1/readings", json=reading).status_code == 401
    assert client.post("/api/sensors/FS-1/readings", json=reading, headers={"X-Device-Key": "x"}).status_code == 401
    key = {"X-Device-Key": SENSOR_KEY}
    assert client.post("/api/sensors/FS-9/readings", json=reading, headers=key).status_code == 404
    assert client.post("/api/sensors/FS-1/readings", json={"level_cm": -1}, headers=key).status_code == 400
    assert client.post("/api/sensors/FS-1/readings", json={"level_cm": 9999}, headers=key).status_code == 400
    assert client.post("/api/sensors/FS-1/readings", json={"level_cm": "deep"}, headers=key).status_code == 400


def test_unknown_api_paths_answer_in_json(client):
    response = client.get("/api/nothing-here")
    assert response.status_code == 404 and "error" in response.json
    assert client.get("/api/route").status_code == 405


def test_oversized_requests_are_refused(client):
    response = client.post("/api/impact", data="x" * 100_000, content_type="application/json")
    assert response.status_code == 413


def test_hosted_mode_trusts_the_proxy_and_secures_cookies(monkeypatch, tmp_path):
    monkeypatch.setenv("SAFEROUTE_TRUST_PROXY", "1")
    monkeypatch.setenv("SAFEROUTE_SECURE_COOKIES", "1")
    area_file = tmp_path / "area.json"
    area_file.write_text(json.dumps(build_area()), encoding="utf-8")
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "s",
            "SENSOR_KEY": SENSOR_KEY,
            "DATABASE": tmp_path / "h.db",
            "AREA_FILE": area_file,
            "SENSORS_FILE": tmp_path / "none.json",
        }
    )
    client = app.test_client()
    page = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert page.headers["Strict-Transport-Security"].startswith("max-age=")
    login = client.post(
        "/api/responder/login",
        json={"name": "Team"},
        headers={"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"},
    )
    assert "Secure" in login.headers["Set-Cookie"]
    app.extensions["saferoute.store"].close()


def test_installable_app_files(client):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.mimetype == "application/manifest+json"
    data = manifest.json if manifest.is_json else json.loads(manifest.data)
    assert data["display"] == "standalone" and data["start_url"] == "/"
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200

    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert worker.mimetype == "text/javascript"
    assert worker.headers["Cache-Control"] == "no-cache"
    page = client.get("/").data
    assert b'rel="manifest"' in page and b"apple-touch-icon" in page


def test_text_responses_are_gzipped_for_slow_connections(client):
    import gzip

    plain = client.get("/api/area")
    zipped = client.get("/api/area", headers={"Accept-Encoding": "gzip, deflate, br"})
    assert zipped.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in zipped.headers["Vary"]
    assert json.loads(gzip.decompress(zipped.data)) == plain.json
    assert len(zipped.data) < len(plain.data)

    script = client.get("/static/app.js", headers={"Accept-Encoding": "gzip"})
    assert script.headers["Content-Encoding"] == "gzip"
    assert b"SafeRoute map client" in gzip.decompress(script.data)

    tiny = client.get("/healthz", headers={"Accept-Encoding": "gzip"})  # under 1 KB: left alone
    assert "Content-Encoding" not in tiny.headers
    icon = client.get("/static/icons/icon-192.png", headers={"Accept-Encoding": "gzip"})  # already compressed
    assert "Content-Encoding" not in icon.headers


def test_configured_responder_id_must_match(app, client):
    app.config["RESPONDER_ID"] = "admin"
    wrong_id = client.post("/api/responder/login", json={"name": "someone"})
    assert wrong_id.status_code == 401 and wrong_id.json["code"] == "name_not_allowed"
    ok = client.post("/api/responder/login", json={"name": "ADMIN"})  # match is case-insensitive
    assert ok.status_code == 200 and ok.json["responder"] == "ADMIN"


def test_passwordless_responder_signin_by_default(app):
    """With no responder ID configured, a name alone signs a responder in."""
    app.config["RESPONDER_ID"] = None
    client = app.test_client()
    too_short = client.post("/api/responder/login", json={"name": "A"})
    assert too_short.status_code == 400 and too_short.json["code"] == "name_needed"
    ok = client.post("/api/responder/login", json={"name": "NDRF Team 4"})
    assert ok.status_code == 200 and ok.json["responder"] == "NDRF Team 4"
    # ...and they can change the map without ever sending a passcode.
    headers = {"X-CSRF-Token": ok.json["csrf"]}
    assert client.post("/api/closures", json={"road": "r03", "reason": "Flooded"}, headers=headers).status_code == 200
