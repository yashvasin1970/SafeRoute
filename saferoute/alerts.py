"""Sending SMS, and alerting subscribers when their route is blocked or their shelter is lost.

After every change to the map, the alerter works out each subscriber's route again (on foot, safest)
and compares it with the one they were last told about. It texts them when:

* a road on their route was closed ("blocked"),
* their shelter filled up, closed or was cut off ("shelter_full", "shelter_closed", "shelter_cut"),
* no open shelter can be reached any more ("none"), or a route opened up again after that ("back").

A route that merely gets a little faster, or moves because of risk or load balancing, does not
trigger a text: alerts are for problems, so people keep reading them. At most one alert per
subscriber every five minutes; one that is held back goes out on the next check. People who
said they are safe get no more alerts.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from . import sms
from .network import RoadNetwork
from .store import Store, now_iso
from .subscriptions import is_demo

MIN_GAP = timedelta(minutes=5)


class Messenger:
    """Queues every SMS in the outbox. With a gateway configured, real numbers are also posted to it."""

    def __init__(self, store: Store, outbound_url: str | None = None, key: str | None = None, post=None):
        self.store = store
        self.outbound_url = outbound_url
        self.key = key
        self.post = post or self._post

    def send(self, phone: str, text: str, kind: str) -> int:
        live = bool(self.outbound_url) and not is_demo(phone)
        message_id = self.store.queue_sms(phone, text, kind, "queued" if live else "simulated")
        if live:
            threading.Thread(target=self._deliver, args=(message_id, phone, text), daemon=True).start()
        return message_id

    def _deliver(self, message_id: int, phone: str, text: str) -> None:
        try:
            self.post(phone, text)
            self.store.set_sms_status(message_id, "sent")
        except Exception:  # the gateway is down: keep the message, mark it
            self.store.set_sms_status(message_id, "failed")

    def _post(self, phone: str, text: str) -> None:
        request = urllib.request.Request(
            self.outbound_url,
            data=json.dumps({"to": phone, "text": text}).encode(),
            headers={"Content-Type": "application/json", "X-Gateway-Key": self.key or ""},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10):
            pass


class Alerter:
    def __init__(
        self,
        store: Store,
        network: RoadNetwork,
        route_for: Callable[[float, float], dict],
        messenger: Messenger,
        now: Callable[[], datetime] | None = None,
    ):
        self.store, self.network, self.route_for, self.messenger = store, network, route_for, messenger
        self.now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    def _route(self, street: str) -> dict:
        lat, lon = self.network.street_point(street)
        return self.route_for(lat, lon)

    def _summary(self, route: dict, lang: str) -> dict:
        shelter = self.network.shelters[route["shelter"]["id"]]
        return {"shelter": shelter.sms_name, "eta": sms.minutes(route["eta_s"]), "steps": sms.steps_text(route, lang)}

    def baseline(self, sub: dict) -> dict:
        """Record a new subscriber's current route and return the welcome text."""
        data = self._route(sub["street"])
        route = data.get("route")
        sub.update(shelter_id=route["shelter"]["id"] if route else None, route_roads=route["roads"] if route else [])
        self.store.save_subscriber(sub)
        if route:
            return sms.say("welcome", sub["lang"], street=sub["street"], **self._summary(route, sub["lang"]))
        return sms.say("alert_none", sub["lang"], street=sub["street"])

    def check(self) -> int:
        """Look at every subscriber once. Returns how many alerts were sent."""
        with self._lock:
            closed = set(self.store.closures())
            sent = 0
            for sub in self.store.subscribers():
                if sub["safe_at"] or sub["street"] not in self.network.streets:
                    continue
                if sub["route_roads"] is None:
                    self.baseline(sub)
                    continue
                data = self._route(sub["street"])
                route = data.get("route")
                kind = self._why(sub, route, data, closed)
                recent = sub["last_alert_at"] and self.now() - datetime.fromisoformat(sub["last_alert_at"]) < MIN_GAP
                if kind and recent:
                    continue  # held back; the next check sends it
                if kind:
                    self.messenger.send(sub["phone"], self._text(kind, sub, route), f"alert_{kind}")
                    sub["last_alert_at"] = now_iso()
                    sent += 1
                sub.update(
                    shelter_id=route["shelter"]["id"] if route else None, route_roads=route["roads"] if route else []
                )
                self.store.save_subscriber(sub)
            return sent

    def _why(self, sub: dict, route: dict | None, data: dict, closed: set[str]) -> str | None:
        old_shelter, old_roads = sub["shelter_id"], set(sub["route_roads"])
        if route is None:
            return "none" if old_shelter else None
        if old_shelter is None:
            return "back"
        options = {o["id"]: o for o in data.get("options", [])}
        if route["shelter"]["id"] != old_shelter:
            old = options.get(old_shelter)
            if old and old["status"] in ("full", "closed"):
                return f"shelter_{old['status']}"
            if old and not old["reachable"]:
                return "shelter_cut"
        if old_roads & closed:
            return "blocked"
        return None

    def _text(self, kind: str, sub: dict, route: dict | None) -> str:
        lang, street = sub["lang"], sub["street"]
        if kind == "none":
            return sms.say("alert_none", lang, street=street)
        details = self._summary(route, lang)
        if kind.startswith("shelter_"):
            old = self.network.shelters[sub["shelter_id"]].sms_name
            return sms.say("alert_shelter", lang, old=old, state=sms.say(f"state_{kind[8:]}", lang), **details)
        return sms.say(f"alert_{kind}", lang, street=street, **details)


class AlertWorker:
    """Runs the alerter in the background shortly after each change, and every minute anyway."""

    def __init__(self, app, alerter: Alerter, every_s: float = 60):
        self.app, self.alerter, self.every_s = app, alerter, every_s
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def kick(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while True:
            self._wake.wait(self.every_s)
            self._wake.clear()
            time.sleep(1)  # let a burst of changes settle
            try:
                with self.app.app_context():
                    self.alerter.check()
            except Exception as error:  # keep the worker alive whatever happens
                self.app.logger.warning("alert check failed: %s", error)
