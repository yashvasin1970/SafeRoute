"""Secrets, responder sign-in, CSRF protection, rate limiting and security headers."""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, session

RESPONDER_ID_ENV = "SAFEROUTE_RESPONDER_ID"  # optional: when set, responders must sign in with this ID

ENV_OVERRIDES = {
    "secret_key": "SAFEROUTE_SECRET_KEY",
    "sensor_key": "SAFEROUTE_SENSOR_KEY",
    "sms_key": "SAFEROUTE_SMS_KEY",
}
# The responder passcode is NOT generated: responders sign in with just a name by default. Set
# SAFEROUTE_RESPONDER_PASSCODE to require a passcode (e.g. in production).


def load_secrets(instance: Path) -> dict[str, str]:
    """Secrets come from the environment, or are generated once and kept in instance/secrets.json."""
    path = instance / "secrets.json"
    stored: dict[str, str] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    makers = {
        "secret_key": lambda: secrets.token_hex(32),
        "sensor_key": lambda: secrets.token_urlsafe(24),
        "sms_key": lambda: secrets.token_urlsafe(24),
    }
    missing = [key for key in makers if key not in stored]
    for key in missing:
        stored[key] = makers[key]()
    if missing:
        path.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):  # not supported on every filesystem
            path.chmod(0o600)
    return {key: os.environ.get(env) or stored[key] for key, env in ENV_OVERRIDES.items()}


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class AttemptLimiter:
    """Allows `limit` failed attempts per key in a sliding window, then blocks until they age out."""

    def __init__(self, limit: int = 5, window_s: float = 300.0):
        self.limit = limit
        self.window_s = window_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque[float]:
        fails = self._fails[key]
        while fails and now - fails[0] > self.window_s:
            fails.popleft()
        return fails

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._trim(key, time.monotonic())) >= self.limit

    def fail(self, key: str) -> None:
        with self._lock:
            self._trim(key, time.monotonic()).append(time.monotonic())

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def allow(self, key: str) -> bool:
        """For plain rate limits: count this attempt and say whether it is within the limit."""
        with self._lock:
            hits = self._trim(key, time.monotonic())
            if len(hits) >= self.limit:
                return False
            hits.append(time.monotonic())
            return True


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def responder_required(view: Callable) -> Callable:
    """Only signed-in responders can change the map, and every change must carry the CSRF token."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("responder"):
            return jsonify(error="Sign in as a responder first."), 401
        token = request.headers.get("X-CSRF-Token", "")
        if not token or not same(token, session.get("csrf", "")):
            return jsonify(error="Your session has expired. Reload the page and try again."), 403
        return view(*args, **kwargs)

    return wrapper


CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: https://tile.openstreetmap.org",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


def add_security_headers(app: Flask) -> None:
    @app.after_request
    def headers(response):
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(self)")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response
