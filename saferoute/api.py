"""HTTP API used by the map, by responders and by the flood sensors."""

from __future__ import annotations

import math
import secrets

from flask import Blueprint, Response, current_app, jsonify, request, session

from . import sensors as sensor_rules
from .network import MODES, RoadNetwork
from .routing import SHELTER_STATUSES, find_route, impact
from .security import AttemptLimiter, csrf_token, responder_required, same
from .store import Store

bp = Blueprint("api", __name__, url_prefix="/api")

CLOSURE_REASONS = ("Flooded", "Fallen tree", "Debris or collapse", "Fire", "Accident", "Other")
MAX_SCENARIO_ITEMS = 300


def network() -> RoadNetwork:
    return current_app.extensions["saferoute.network"]


def store() -> Store:
    return current_app.extensions["saferoute.store"]


def login_limiter() -> AttemptLimiter:
    return current_app.extensions["saferoute.login_limiter"]


def bad(message: str, status: int = 400):
    return jsonify(error=message), status


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


# ---- public reads --------------------------------------------------------------------------------


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
    closures = store().closures()
    readings = store().sensor_state()
    statuses = store().shelter_status()
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
            for c in closures.values()
            if c.road_id in net.roads
        ],
        shelters={s: statuses.get(s, "open") for s in net.shelters},
        sensors={
            sid: {
                "level_cm": readings.get(sid, {}).get("level_cm", 0.0),
                "band": sensor_rules.band(readings.get(sid, {}).get("level_cm", 0.0)),
                "armed": readings.get(sid, {}).get("armed", True),
                "updated_at": readings.get(sid, {}).get("updated_at"),
            }
            for sid in net.sensors
        },
        events=store().events(15),
    )


@bp.post("/route")
def route():
    data = body()
    if data is None:
        return bad("Send JSON with lat, lon and mode.")
    lat, lon = number(data.get("lat"), -90, 90), number(data.get("lon"), -180, 180)
    mode = data.get("mode", "foot")
    if lat is None or lon is None:
        return bad("lat and lon must be numbers.")
    if mode not in MODES:
        return bad(f"mode must be one of {', '.join(MODES)}.")

    net = network()
    snapped = net.nearest_node(lat, lon)
    if snapped is None:
        return bad("That spot is outside the mapped area. Tap somewhere on the roads shown.", 422)
    node, snap_m = snapped

    version = store().version()
    result = find_route(net, node, store().closures().keys(), store().shelter_status(), mode, snap_m)
    route = result.route
    return jsonify(
        version=version,
        mode=mode,
        start={"node": node, "lat": net.coords[node][0], "lon": net.coords[node][1], "snap_m": round(snap_m)},
        route=None
        if route is None
        else {
            "shelter": {"id": route.shelter.id, "name": route.shelter.name},
            "distance_m": round(route.distance_m),
            "eta_s": round(route.eta_s),
            "steps": route.steps,
            "roads": route.roads,
            "geometry": route.geometry,
        },
        options=[
            {
                "id": o.shelter.id,
                "name": o.shelter.name,
                "status": o.status,
                "reachable": o.reachable,
                "eta_s": round(o.eta_s) if o.eta_s is not None else None,
                "distance_m": round(o.distance_m) if o.distance_m is not None else None,
            }
            for o in result.options
        ],
    )


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
        or any(r not in net.roads for r in extra_roads if isinstance(r, str))
        or not all(isinstance(r, str) for r in extra_roads)
    ):
        return bad("closures must be a list of road ids from this map.")
    if not isinstance(extra_shelters, list) or not all(
        isinstance(s, str) and s in net.shelters for s in extra_shelters
    ):
        return bad("shelters_closed must be a list of shelter ids from this map.")

    result = impact(net, store().closures().keys(), extra_roads, store().shelter_status(), extra_shelters, mode)
    return jsonify(version=store().version(), **result)


# ---- responders ----------------------------------------------------------------------------------


@bp.get("/session")
def who():
    return jsonify(responder=session.get("responder"), csrf=csrf_token(), reasons=CLOSURE_REASONS)


@bp.post("/responder/login")
def login():
    data = body()
    if data is None:
        return bad("Send JSON with name and passcode.")
    key = request.remote_addr or "unknown"
    if login_limiter().blocked(key):
        return bad("Too many wrong passcodes. Wait five minutes and try again.", 429)

    name = str(data.get("name", "")).strip()
    passcode = str(data.get("passcode", "")).strip()
    if not 2 <= len(name) <= 60:
        return bad("Enter your name or unit (2 to 60 characters).")
    if not passcode or not same(passcode, current_app.config["RESPONDER_PASSCODE"]):
        login_limiter().fail(key)
        return bad("That passcode is not right.", 401)

    login_limiter().clear(key)
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
    return jsonify(changed=changed, version=store().version())


@bp.delete("/closures/<road_id>")
@responder_required
def reopen_road(road_id: str):
    if road_id not in network().roads:
        return bad("Unknown road.", 404)
    closure = store().reopen_road(road_id, session["responder"], road_label(road_id))
    return jsonify(changed=closure is not None, version=store().version())


@bp.post("/shelters/<shelter_id>")
@responder_required
def shelter_status(shelter_id: str):
    shelter = network().shelters.get(shelter_id)
    if shelter is None:
        return bad("Unknown shelter.", 404)
    status = (body() or {}).get("status")
    if status not in SHELTER_STATUSES:
        return bad(f"status must be one of: {', '.join(SHELTER_STATUSES)}.")
    changed = store().set_shelter(shelter_id, status, session["responder"], shelter.name)
    return jsonify(changed=changed, version=store().version())


@bp.post("/reset")
@responder_required
def reset():
    store().reset(session["responder"])
    return jsonify(version=store().version())


# ---- sensors -------------------------------------------------------------------------------------


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
    return jsonify(
        band=sensor_rules.band(level),
        closed=len(changes["closed"]),
        reopened=len(changes["reopened"]),
        version=store().version(),
    )
