"""SafeRoute: real-time evacuation routing around blocked roads."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .alerts import Alerter, AlertWorker, Messenger
from .api import bp as api_bp
from .api import route_body
from .compression import add_compression
from .feeds import WeatherFeed
from .network import RoadNetwork
from .security import AttemptLimiter, add_security_headers, load_secrets
from .store import Store
from .story import StoryRunner

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AREA = ROOT / "data" / "velachery.json"
DEFAULT_SENSORS = ROOT / "data" / "velachery.sensors.json"
DEFAULT_SHELTERS = ROOT / "data" / "velachery.shelters.json"


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def create_app(overrides: dict | None = None) -> Flask:
    overrides = overrides or {}
    if not overrides.get("TESTING"):
        load_dotenv(ROOT / ".env")  # local settings; tests never read them
    app = Flask(__name__, instance_path=str(ROOT / "instance"))
    instance = Path(app.instance_path)
    instance.mkdir(exist_ok=True)

    secrets_ = load_secrets(instance) if not overrides.get("TESTING") else {}
    app.config.update(
        # Behind a hosting proxy (Render, Cloud Run): trust its X-Forwarded-* headers so the
        # sign-in rate limit sees real client addresses, and only send cookies over HTTPS.
        TRUST_PROXY=env_flag("SAFEROUTE_TRUST_PROXY"),
        SESSION_COOKIE_SECURE=env_flag("SAFEROUTE_SECURE_COOKIES"),
    )
    app.config.update(
        SECRET_KEY=secrets_.get("secret_key"),
        RESPONDER_PASSCODE=secrets_.get("responder_passcode"),
        RESPONDER_ID=None
        if overrides.get("TESTING")
        else (os.environ.get("SAFEROUTE_RESPONDER_ID", "").strip() or None),
        SENSOR_KEY=secrets_.get("sensor_key"),
        SMS_KEY=secrets_.get("sms_key"),
        DATABASE=instance / "saferoute.db",
        AREA_FILE=DEFAULT_AREA,
        SENSORS_FILE=DEFAULT_SENSORS,
        SHELTERS_FILE=DEFAULT_SHELTERS,
        WEATHER_ENABLED=not overrides.get("TESTING") and not env_flag("SAFEROUTE_NO_WEATHER"),
        STORY_STEP_SECONDS=float(os.environ.get("SAFEROUTE_STORY_STEP_SECONDS", "9")),
        SMS_OUTBOUND_URL=os.environ.get("SAFEROUTE_SMS_OUTBOUND_URL") or None,
        ALERTS_IN_BACKGROUND=not overrides.get("TESTING"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_NAME="saferoute_session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    app.config.update(overrides)
    app.json.sort_keys = False
    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    net = RoadNetwork.load(app.config["AREA_FILE"], app.config["SENSORS_FILE"], app.config["SHELTERS_FILE"])
    db = Store(app.config["DATABASE"])
    db.init()
    lat, lon = net.meta["center"]
    feed = WeatherFeed(db, lat, lon, enabled=app.config["WEATHER_ENABLED"], fetch=app.config.get("WEATHER_FETCH"))
    app.extensions["saferoute.weather"] = feed
    app.extensions["saferoute.story"] = StoryRunner(
        db, net, feed, app.config["STORY_STEP_SECONDS"], app.config["SECRET_KEY"] or ""
    )
    app.extensions["saferoute.network"] = net
    area_json = json.dumps(net.client_payload(), separators=(",", ":"))
    app.extensions["saferoute.area_json"] = (area_json, hashlib.sha256(area_json.encode()).hexdigest()[:20])
    app.extensions["saferoute.store"] = db
    app.extensions["saferoute.login_limiter"] = AttemptLimiter(limit=5, window_s=300)
    app.extensions["saferoute.reports_limiter"] = AttemptLimiter(limit=6, window_s=600)
    app.extensions["saferoute.arrivals_limiter"] = AttemptLimiter(limit=10, window_s=600)
    app.extensions["saferoute.sms_limiter"] = AttemptLimiter(limit=20, window_s=600)
    app.extensions["saferoute.signup_limiter"] = AttemptLimiter(limit=5, window_s=600)
    app.extensions["saferoute.safe_limiter"] = AttemptLimiter(limit=5, window_s=600)

    # SMS out, and alerts when subscribers' routes are blocked or their shelter is lost.
    messenger = Messenger(db, app.config["SMS_OUTBOUND_URL"], app.config["SMS_KEY"])
    alerter = Alerter(db, net, lambda lat, lon: route_body(lat, lon, "foot", "safest")[0], messenger)
    app.extensions["saferoute.messenger"] = messenger
    app.extensions["saferoute.alerter"] = alerter
    worker = AlertWorker(app, alerter) if app.config["ALERTS_IN_BACKGROUND"] else None
    app.extensions["saferoute.alerts_worker"] = worker
    if worker is not None:
        app.extensions["saferoute.story"].on_change = worker.kick

    app.register_blueprint(api_bp)
    # Registered first so it runs last: compress the final body, after headers are set.
    add_compression(app)
    add_security_headers(app)

    @app.get("/")
    def index():
        return render_template("index.html", area=net.meta)

    @app.get("/manifest.webmanifest")
    def manifest():
        response = send_from_directory(app.static_folder, "manifest.webmanifest")
        response.mimetype = "application/manifest+json"
        return response

    @app.get("/sw.js")
    def service_worker():
        # Served from the root so it can control the whole app; browsers re-check it on every visit.
        response = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript", max_age=0)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/healthz")
    def health():
        return jsonify(ok=True, junctions=len(net.coords), roads=len(net.roads))

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return error

    return app
