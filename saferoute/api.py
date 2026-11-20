"""HTTP API for the map, responders, planners, flood sensors and the SMS gateway."""

from __future__ import annotations

import csv
import io
import math
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape

from flask import Blueprint, Response, current_app, jsonify, request, session

from . import sensors as sensor_rules
from .feeds import WeatherFeed
from .network import MODES, RoadNetwork
from .reports import REPORT_KINDS, client_id, dismiss_report, make_report, verify_report
from .risk import Risk, road_risk
from .routing import PREFERENCES, SHELTER_STATUSES, effective_status, find_route, impact
from .security import AttemptLimiter, csrf_token, responder_required, same
from .sms import SmsServices, say
from .sms import handle as handle_sms
from .store import Store
from .story import StoryRunner
from .subscriptions import is_demo, join_by_sms, mask, normalise_phone, start_signup, verify_signup

bp = Blueprint("api", __name__, url_prefix="/api")

CLOSURE_REASONS = ("Flooded", "Fallen tree", "Debris or collapse", "Fire", "Accident", "Other")
MAX_SCENARIO_ITEMS = 300
ARRIVAL_MINUTES = 45
MAX_PARTY = 20


def network() -> RoadNetwork:
    return current_app.extensions["saferoute.network"]


def store() -> Store:
    return current_app.extensions["saferoute.store"]


def weather() -> WeatherFeed:
    return current_app.extensions["saferoute.weather"]


def story() -> StoryRunner:
    return current_app.extensions["saferoute.story"]


def limiter(name: str) -> AttemptLimiter:
    return current_app.extensions[f"saferoute.{name}_limiter"]


def messenger():
    return current_app.extensions["saferoute.messenger"]


def alerter():
    return current_app.extensions["saferoute.alerter"]


def kick() -> None:
    """Ask the alert worker to look at subscribers' routes soon (after a change to the map)."""
    worker = current_app.extensions.get("saferoute.alerts_worker")
    if worker is not None:
        worker.kick()


def client() -> str:
    return client_id(current_app.config["SECRET_KEY"] or "", request.remote_addr or "unknown")


def bad(message: str, status: int = 400, code: str | None = None):
    return jsonify(error=message, code=code), status


def body() -> dict | None:
    data = request.get_json(silent=True) if request.is_json else None
    return data if isinstance(data, dict) else None


def number(value, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) and low <= value <= high else None


def road_label(road_id: str) -> str:
    road = network().roads[road_id]
    return road.name if road.name != "Unnamed road" else f"Unnamed road ({road.id})"


# ---- the live picture -------------------------------------------------------------------------


@dataclass
class Live:
    closures: dict
    statuses: dict[str, str]  # what responders set
    effective: dict[str, str]  # counting occupancy against capacity
    occupancy: dict[str, int]
    en_route: dict[str, int]
    readings: dict[str, dict]
    reports: list[dict]
    weather: dict
    risk: dict[str, Risk]


def live() -> Live:
    net, db = network(), store()
    closures = db.closures()
    statuses, occupancy = db.shelter_status(), db.occupancy()
    readings = db.sensor_state()
    reports = db.reports("open")
    current_weather = weather().current()
    levels = {sid: r["level_cm"] for sid, r in readings.items()}
    return Live(
        closures=closures,
        statuses=statuses,
        effective={
            sid: effective_status(statuses.get(sid, "open"), occupancy.get(sid, 0), shelter.capacity)
            for sid, shelter in net.shelters.items()
        },
        occupancy=occupancy,
        en_route=db.en_route(),
        readings=readings,
        reports=reports,
        weather=current_weather,
        risk=road_risk(net, levels, reports, current_weather.get("level"), closures.keys(), net.history_roads),
    )


def route_body(lat: float, lon: float, mode: str, prefer: str) -> tuple[dict, int]:
    """The /api/route response for a point (also used by SMS)."""
    net = network()
    snapped = net.nearest_node(lat, lon)
    if snapped is None:
        return {
            "error": "That spot is outside the mapped area. Tap somewhere on the roads shown.",
            "code": "outside_area",
        }, 422
    node, snap_m = snapped
    state = live()
    version = store().version()
    result = find_route(
        net,
        node,
        state.closures.keys(),
        state.effective,
        mode,
        snap_m,
        occupancy=state.occupancy,
        en_route=state.en_route,
        risk=state.risk,
        prefer=prefer,
    )
    route = result.route
    return {
        "version": version,
        "mode": mode,
        "prefer": prefer,
        "start": {"node": node, "lat": net.coords[node][0], "lon": net.coords[node][1], "snap_m": round(snap_m)},
        "route": None
        if route is None
        else {
            "shelter": {"id": route.shelter.id, "name": route.shelter.name},
            "distance_m": round(route.distance_m),
            "eta_s": round(route.eta_s),
            "steps": route.steps,
            "roads": route.roads,
            "geometry": route.geometry,
            "risky": route.risky,
            "fastest_eta_s": round(route.fastest_eta_s) if route.fastest_eta_s is not None else None,
            "avoided": route.avoided,
            "balanced_from": route.balanced_from,
        },
        "options": [
            {
                "id": o.shelter.id,
                "name": o.shelter.name,
                "status": o.status,
                "reachable": o.reachable,
                "eta_s": round(o.eta_s) if o.eta_s is not None else None,
                "distance_m": round(o.distance_m) if o.distance_m is not None else None,
                "capacity": o.capacity,
                "occupancy": o.occupancy,
                "en_route": o.en_route,
                "load_pct": round(100 * o.load),
            }
            for o in result.options
        ],
    }, 200


# ---- public reads -----------------------------------------------------------------------------


@bp.get("/area")
def area():
    """Static map data. Browsers keep a copy and revalidate it, so a reload costs a 304."""
    payload, etag = current_app.extensions["saferoute.area_json"]
    response = Response(payload, mimetype="application/json")
    response.set_etag(etag)
    response.headers["Cache-Control"] = "no-cache"
    return response.make_conditional(request)


@bp.get("/state")
def state():
    version = store().version()
    since = request.args.get("since", type=int)
    if since is not None and since == version:
        return Response(status=204)

    net = network()
    now = live()
    responder = bool(session.get("responder"))
    reports = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "lat": r["lat"],
            "lon": r["lon"],
            "place": r["place"],
            "at": r["created_at"],
            **({"note": r["note"], "source": r["source"], "people": r["people"]} if responder else {}),
        }
        for r in now.reports
        if responder or r["kind"] != "rescue"  # rescue requests (and all notes) are for responders only
    ]
    return jsonify(
        version=version,
        closures=[
            {
                "road": c.road_id,
                "name": road_label(c.road_id),
                "reason": c.reason,
                "source": c.source,
                "reported_by": c.reported_by,
                "at": c.created_at,
            }
            for c in now.closures.values()
            if c.road_id in net.roads
        ],
        shelters={
            sid: {
                "status": now.effective[sid],
                "set_status": now.statuses.get(sid, "open"),
                "capacity": shelter.capacity,
                "occupancy": now.occupancy.get(sid, 0),
                "en_route": now.en_route.get(sid, 0),
            }
            for sid, shelter in net.shelters.items()
        },
        sensors={
            sid: {
                "level_cm": now.readings.get(sid, {}).get("level_cm", 0.0),
                "band": sensor_rules.band(now.readings.get(sid, {}).get("level_cm", 0.0)),
                "armed": now.readings.get(sid, {}).get("armed", True),
                "updated_at": now.readings.get(sid, {}).get("updated_at"),
            }
            for sid in net.sensors
        },
        risk={
            road_id: {"factor": r.factor, "reasons": [reason.as_dict() for reason in r.reasons]}
            for road_id, r in now.risk.items()
        },
        reports=reports,
        weather=now.weather,
        story=store().feed("story"),
        events=store().events(15),
    )


@bp.post("/route")
def route():
    data = body()
    if data is None:
        return bad("Send JSON with lat, lon and mode.")
    lat, lon = number(data.get("lat"), -90, 90), number(data.get("lon"), -180, 180)
    mode = data.get("mode", "foot")
    prefer = data.get("prefer", "safest")
    if lat is None or lon is None:
        return bad("lat and lon must be numbers.")
    if mode not in MODES:
        return bad(f"mode must be one of {', '.join(MODES)}.")
    if prefer not in PREFERENCES:
        return bad(f"prefer must be one of {', '.join(PREFERENCES)}.")
    payload, status = route_body(lat, lon, mode, prefer)
    return jsonify(payload), status


@bp.post("/impact")
def what_if():
    """Planner tool: compare coverage now with hypothetical closures. Publishes nothing."""
    data = body()
    if data is None:
        return bad("Send JSON with mode, closures and shelters_closed.")
    mode = data.get("mode", "foot")
    extra_roads = data.get("closures", [])
    extra_shelters = data.get("shelters_closed", [])
    if mode not in MODES:
        return bad(f"mode must be one of {', '.join(MODES)}.")
    net = network()
    if (
        not isinstance(extra_roads, list)
        or len(extra_roads) > MAX_SCENARIO_ITEMS
        or not all(isinstance(r, str) and r in net.roads for r in extra_roads)
    ):
        return bad("closures must be a list of road ids from this map.")
    if not isinstance(extra_shelters, list) or not all(
        isinstance(s, str) and s in net.shelters for s in extra_shelters
    ):
        return bad("shelters_closed must be a list of shelter ids from this map.")

    now = live()
    result = impact(net, now.closures.keys(), extra_roads, now.effective, extra_shelters, mode)
    return jsonify(version=store().version(), **result)


# ---- residents --------------------------------------------------------------------------------


@bp.post("/arrivals")
def arrivals():
    """'I'm heading here': counts people on their way, so the next people are spread out."""
    data = body() or {}
    shelter = network().shelters.get(data.get("shelter"))
    people = number(data.get("people", 1), 1, MAX_PARTY)
    if shelter is None:
        return bad("Unknown shelter.", 404)
    if people is None or people != int(people):
        return bad(f"people must be a whole number from 1 to {MAX_PARTY}.")
    if not limiter("arrivals").allow(client()):
        return bad("Too many requests. Try again in a few minutes.", 429, "rate_limited")
    if live().effective[shelter.id] != "open":
        return bad("That shelter is not taking people right now.", 409, "shelter_unavailable")
    if not store().add_arrival(shelter.id, int(people), client(), ARRIVAL_MINUTES):
        return bad("You have already told us about three trips.", 429, "too_many_trips")
    return jsonify(ok=True, minutes=ARRIVAL_MINUTES, version=store().version())


@bp.post("/reports")
def new_report():
    """Anyone can report flooding, a blocked road or a rescue need. Reports never close roads."""
    data = body() or {}
    kind = data.get("kind")
    lat, lon = number(data.get("lat"), -90, 90), number(data.get("lon"), -180, 180)
    note = data.get("note", "")
    if kind not in REPORT_KINDS:
        return bad(f"kind must be one of {', '.join(REPORT_KINDS)}.")
    if lat is None or lon is None:
        return bad("lat and lon must be numbers.")
    if not isinstance(note, str) or len(note) > 200:
        return bad("note must be text of at most 200 characters.")
    people = number(data.get("people", 1), 1, MAX_PARTY)
    if people is None or people != int(people):
        return bad(f"people must be a whole number from 1 to {MAX_PARTY}.")
    if not limiter("reports").allow(client()):
        return bad("Too many reports. Try again in a few minutes.", 429, "rate_limited")
    report = make_report(network(), kind, lat, lon, note, "app", client(), int(people))
    if report is None:
        return bad("That spot is outside the mapped area.", 422, "outside_area")
    report_id = store().add_report(report, "Resident")
    return jsonify(id=report_id, place=report["place"], version=store().version())


# ---- responders -------------------------------------------------------------------------------


@bp.get("/session")
def who():
    return jsonify(responder=session.get("responder"), csrf=csrf_token(), reasons=CLOSURE_REASONS)


@bp.post("/responder/login")
def login():
    data = body()
    if data is None:
        return bad("Send JSON with your name.")
    key = request.remote_addr or "unknown"
    if limiter("login").blocked(key):
        return bad("Too many tries. Wait five minutes and try again.", 429, "rate_limited")

    name = str(data.get("name", "")).strip()
    if not 2 <= len(name) <= 60:
        return bad("Enter your name or unit (2 to 60 characters).", 400, "name_needed")
    # Responders sign in with a name alone — there is no passcode. A deployment may limit sign-in to
    # one name or unit with SAFEROUTE_RESPONDER_ID; left unset (the default), any name is accepted.
    expected_id = current_app.config.get("RESPONDER_ID")
    if expected_id and not same(name.lower(), expected_id.lower()):
        limiter("login").fail(key)
        return bad("That name isn't recognised for responder access.", 401, "name_not_allowed")

    limiter("login").clear(key)
    session.clear()
    session.permanent = True
    session["responder"] = name
    session["csrf"] = secrets.token_urlsafe(32)
    return jsonify(responder=name, csrf=session["csrf"])


@bp.post("/responder/logout")
def logout():
    session.clear()
    return jsonify(responder=None, csrf=csrf_token())


@bp.post("/closures")
@responder_required
def close_road():
    data = body() or {}
    road_id, reason = data.get("road"), data.get("reason")
    if road_id not in network().roads:
        return bad("Unknown road.")
    if reason not in CLOSURE_REASONS:
        return bad(f"reason must be one of: {', '.join(CLOSURE_REASONS)}.")
    changed = store().close_road(road_id, reason, "responder", session["responder"], road_label(road_id))
    kick()
    return jsonify(changed=changed, version=store().version())


@bp.delete("/closures/<road_id>")
@responder_required
def reopen_road(road_id: str):
    if road_id not in network().roads:
        return bad("Unknown road.", 404)
    closure = store().reopen_road(road_id, session["responder"], road_label(road_id))
    kick()
    return jsonify(changed=closure is not None, version=store().version())


@bp.post("/shelters/<shelter_id>")
@responder_required
def update_shelter(shelter_id: str):
    """Set a shelter's status, how many people are inside, or both."""
    shelter = network().shelters.get(shelter_id)
    if shelter is None:
        return bad("Unknown shelter.", 404)
    data = body() or {}
    if "status" not in data and "occupancy" not in data:
        return bad("Send status and/or occupancy.")
    if "status" in data and data["status"] not in SHELTER_STATUSES:
        return bad(f"status must be one of: {', '.join(SHELTER_STATUSES)}.")
    people = None
    if "occupancy" in data:
        people = number(data["occupancy"], 0, shelter.capacity * 2)
        if people is None or people != int(people):
            return bad(f"occupancy must be a whole number from 0 to {shelter.capacity * 2}.")
    changed = False
    if "status" in data:
        changed |= store().set_shelter(shelter_id, data["status"], session["responder"], shelter.name)
    if people is not None:
        changed |= store().set_occupancy(shelter_id, int(people), session["responder"], shelter.name, shelter.capacity)
    kick()
    return jsonify(changed=changed, version=store().version())


@bp.post("/reports/<int:report_id>/<action>")
@responder_required
def handle_report(report_id: int, action: str):
    report = store().report(report_id)
    if report is None:
        return bad("Unknown report.", 404)
    if action == "verify":
        changed = verify_report(store(), network(), report, session["responder"])
    elif action == "dismiss":
        changed = dismiss_report(store(), report, session["responder"])
    else:
        return bad("action must be verify or dismiss.", 404)
    kick()
    return jsonify(changed=changed, version=store().version())


@bp.post("/scenario/flood")
@responder_required
def flood_scenario():
    """One click: heavy rain, three flooded stretches, shelters filling, two reports."""
    story().flood_scenario(session["responder"])
    kick()
    return jsonify(loaded=True, version=store().version())


@bp.get("/scenario/flood/roads")
def flood_scenario_roads():
    """The roads the flood scenario would close, so planners can test it without publishing."""
    return jsonify(roads=story().flood_roads())


@bp.get("/history")
def past_floods():
    """Past cyclones and floods in this area, for reference (see data/history.json)."""
    return jsonify(current_app.extensions["saferoute.history"])


@bp.get("/scenario/presets")
def scenario_presets():
    return jsonify(presets=story().preset_ids())


@bp.post("/scenario/preset/<preset_id>")
@responder_required
def scenario_preset(preset_id: str):
    """Set up one known test case: calm, rain, closure, shelter full or rescue request."""
    if not story().preset(preset_id, session["responder"]):
        return bad("Unknown test case.", 404)
    kick()
    return jsonify(loaded=preset_id, version=store().version())


@bp.post("/story/<action>")
@responder_required
def flood_story(action: str):
    """Play (or stop) the scripted flood evening. Starting it resets the demo first."""
    if action == "start":
        return jsonify(started=story().start(session["responder"]), version=store().version())
    if action == "stop":
        story().stop()
        return jsonify(stopped=True, version=store().version())
    return bad("action must be start or stop.", 404)


@bp.post("/reset")
@responder_required
def reset():
    story().stop()
    store().reset(session["responder"])
    kick()
    return jsonify(version=store().version())


# ---- sensors ----------------------------------------------------------------------------------


@bp.post("/sensors/<sensor_id>/readings")
def sensor_reading(sensor_id: str):
    """A water-level sensor reports a reading. Devices authenticate with the shared device key."""
    key = request.headers.get("X-Device-Key", "")
    if not key or not same(key, current_app.config["SENSOR_KEY"]):
        return bad("Unknown device.", 401)
    sensor = network().sensors.get(sensor_id)
    if sensor is None:
        return bad("Unknown sensor.", 404)
    level = number((body() or {}).get("level_cm"), 0, sensor_rules.MAX_LEVEL_CM)
    if level is None:
        return bad(f"level_cm must be a number from 0 to {sensor_rules.MAX_LEVEL_CM}.")
    changes = store().record_reading(sensor.id, sensor.roads, round(level, 1), sensor.label)
    if changes["closed"] or changes["reopened"]:
        kick()
    return jsonify(
        band=sensor_rules.band(level),
        closed=len(changes["closed"]),
        reopened=len(changes["reopened"]),
        version=store().version(),
    )


# ---- SMS ---------------------------------------------------------------------------------------


def sms_services(phone: str | None = None) -> SmsServices:
    def report(kind: str, lat: float, lon: float, note: str, street: str) -> int:
        made = make_report(network(), kind, lat, lon, note, "sms", phone or client())
        return store().add_report(made, "SMS") if made else 0

    def shelters() -> list[dict]:
        now = live()
        return [
            {
                "name": s.sms_name,
                "status": now.effective[sid],
                "load_pct": round(100 * (now.occupancy.get(sid, 0) + now.en_route.get(sid, 0)) / s.capacity),
            }
            for sid, s in network().shelters.items()
        ]

    def join(number: str, street: str, lang: str) -> str:
        sub = join_by_sms(store(), number, street, lang)
        welcome = alerter().baseline(sub)
        kick()
        return welcome

    def stop(number: str) -> bool:
        return store().delete_subscriber(number, "SMS", mask(number))

    def safe(number: str) -> None:
        # An SMS doesn't say where the person is, so no shelter is recorded.
        store().mark_safe(number, None, "sms", mask(number))

    return SmsServices(
        network=network(),
        route=lambda lat, lon: route_body(lat, lon, "foot", "safest")[0],
        report=report,
        shelters=shelters,
        status=lambda: {"closed": len(store().closures()), "rain_level": weather().current().get("level")},
        phone=phone,
        join=join,
        stop=stop,
        safe=safe,
    )


@bp.post("/sms/inbound")
def sms_inbound():
    """Webhook for an SMS gateway. Twilio-style form posts get a TwiML reply; JSON gets JSON."""
    key = request.headers.get("X-Gateway-Key", "")
    if not key or not same(key, current_app.config["SMS_KEY"]):
        return bad("Unknown gateway.", 401)
    if request.is_json:
        data = body() or {}
        text, sender = data.get("text"), data.get("from")
    else:
        text, sender = request.form.get("Body"), request.form.get("From")
    if not isinstance(text, str) or not text.strip():
        return bad("Send the message text.")
    reply, lang = handle_sms(text, sms_services(normalise_phone(sender) if sender else None))
    if not request.is_json:
        return Response(f"<Response><Message>{escape(reply)}</Message></Response>", mimetype="text/xml")
    return jsonify(reply=reply, lang=lang)


def demo_phone(value) -> str | None:
    phone = normalise_phone(value) if isinstance(value, str) else None
    return phone if phone and is_demo(phone) else None


@bp.post("/sms/simulate")
def sms_simulate():
    """The in-app phone: same logic as a real SMS. Its number is a demo number (+91 0000 ...)."""
    data = body() or {}
    text = data.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 320:
        return bad("Type a message of up to 320 characters.")
    if not limiter("sms").allow(client()):
        return bad("Too many messages. Try again in a few minutes.", 429, "rate_limited")
    reply, lang = handle_sms(text, sms_services(demo_phone(data.get("phone"))))
    return jsonify(reply=reply, lang=lang, version=store().version())


@bp.get("/sms/inbox")
def sms_inbox():
    """Messages SafeRoute sent to a demo phone (codes and alerts). Real numbers are never readable."""
    phone = demo_phone(request.args.get("phone"))
    if phone is None:
        return bad("Only the in-app demo phone has an inbox here.", 403)
    return jsonify(messages=store().messages_to(phone, request.args.get("after", 0, type=int)))


# ---- alert sign-up, "I'm safe" -----------------------------------------------------------------


@bp.post("/subscriptions")
def subscribe():
    """Web sign-up for SMS alerts. A code goes to the number; nothing starts until it comes back."""
    data = body() or {}
    phone = normalise_phone(data.get("phone", ""))
    street, lang = data.get("street"), data.get("lang", "en")
    if phone is None:
        return bad("Enter a 10-digit Indian mobile number.", 400, "bad_phone")
    if not is_demo(phone) and not current_app.config.get("SMS_OUTBOUND_URL"):
        return bad("This demo can only text the in-app phone.", 422, "demo_only")
    if street not in network().streets:
        return bad("Pick your street from the list.", 400, "bad_street")
    if lang not in ("en", "ta", "hi"):
        return bad("lang must be en, ta or hi.")
    if not limiter("signup").allow(client()):
        return bad("Too many sign-ups. Try again in a few minutes.", 429, "rate_limited")
    outcome, code = start_signup(store(), phone, street, lang, current_app.config["SECRET_KEY"] or "")
    if outcome == "rate_limited":
        return bad("Too many codes sent to that number. Try again in an hour.", 429, "otp_limit")
    messenger().send(phone, say("otp", lang, code=code), "otp")
    return jsonify(sent=True, to=mask(phone))


@bp.post("/subscriptions/verify")
def confirm_subscription():
    data = body() or {}
    phone = normalise_phone(data.get("phone", ""))
    code = str(data.get("code", ""))
    if phone is None or not code.isdigit():
        return bad("Enter the number and the 6-digit code.", 400, "wrong_code")
    outcome, sub = verify_signup(store(), phone, code, current_app.config["SECRET_KEY"] or "")
    if outcome != "verified":
        status = {"wrong": 400, "expired": 400, "locked": 429, "none": 404}[outcome]
        code_name = {"wrong": "wrong_code", "expired": "code_expired", "locked": "code_locked", "none": "no_signup"}
        return bad("That code did not work.", status, code_name[outcome])
    messenger().send(phone, alerter().baseline(sub), "welcome")
    kick()
    return jsonify(verified=True, street=sub["street"], version=store().version())


@bp.post("/checkin")
def check_in():
    """'I've reached the shelter': the shelter's headcount goes up and the person counts as safe."""
    data = body() or {}
    shelter = network().shelters.get(data.get("shelter"))
    people = number(data.get("people", 1), 1, MAX_PARTY)
    if shelter is None:
        return bad("Unknown shelter.", 404)
    if people is None or people != int(people):
        return bad(f"people must be a whole number from 1 to {MAX_PARTY}.")
    if not limiter("safe").allow(client()):
        return bad("Too many requests. Try again in a few minutes.", 429, "rate_limited")
    inside = store().check_in(shelter.id, int(people), client(), shelter.name, shelter.capacity)
    kick()
    return jsonify(inside=inside, capacity=shelter.capacity, full=inside >= shelter.capacity, version=store().version())


@bp.post("/safe")
def im_safe():
    """A resident says they are safe. Counted once per person, for responders' numbers."""
    data = body() or {}
    shelter_id = data.get("shelter")
    if shelter_id is not None and shelter_id not in network().shelters:
        return bad("Unknown shelter.", 404)
    if not limiter("safe").allow(client()):
        return bad("Too many requests. Try again in a few minutes.", 429, "rate_limited")
    label = network().shelters[shelter_id].name if shelter_id else "a resident"
    new = store().mark_safe(f"web:{client()}", shelter_id, "app", label)
    return jsonify(counted=new, version=store().version())


# ---- responder numbers and export --------------------------------------------------------------


@bp.get("/stats")
def stats():
    if not session.get("responder"):
        return bad("Sign in as a responder first.", 401)
    net = network()
    subs = store().subscribers(verified_only=False)
    verified = [s for s in subs if s["verified"]]
    heading = Counter(s["shelter_id"] for s in verified if not s["safe_at"])
    marks = store().safe_marks()
    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    reports = store().reports("open")
    sms_counts = store().outbox_counts()
    return jsonify(
        subscribers={
            "total": len(verified),
            "pending": len(subs) - len(verified),
            "safe": sum(1 for s in verified if s["safe_at"]),
            "no_route": heading.get(None, 0),
            "by_shelter": {sid: heading.get(sid, 0) for sid in net.shelters},
        },
        safe={
            "total": len(marks),
            "last_hour": sum(1 for m in marks if datetime.fromisoformat(m["at"]) >= hour_ago),
            "by_source": dict(Counter(m["source"] for m in marks)),
            "at_shelters": dict(Counter(m["shelter_id"] for m in marks if m["shelter_id"])),
        },
        reports={"open": len(reports), "rescue": sum(1 for r in reports if r["kind"] == "rescue")},
        en_route=sum(store().en_route().values()),
        sms={
            "total": sum(sms_counts.values()),
            "alerts": sum(n for k, n in sms_counts.items() if k.startswith("alert_")),
        },
    )


@bp.get("/events.csv")
def export_events():
    """The whole activity log as a spreadsheet, for reports and hand-overs."""
    if not session.get("responder"):
        return bad("Sign in as a responder first.", 401)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "time_utc", "who", "action", "detail"])
    for event in store().all_events():
        writer.writerow([event["id"], event["at"], event["actor"], event["action"], event["detail"]])
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return Response(
        "﻿" + out.getvalue(),  # BOM so Excel reads Tamil and Hindi correctly
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="saferoute-events-{stamp}.csv"'},
    )
