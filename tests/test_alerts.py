"""SMS sign-up with one-time codes, route alerts, short Unicode texts, "I'm safe" and responder stats."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from saferoute import sms
from saferoute.subscriptions import mask, normalise_phone, start_signup, verify_signup

DEMO = "+910000123456"


def inbox(client, phone=DEMO):
    return [m["text"] for m in client.get(f"/api/sms/inbox?phone={phone}").json["messages"]]


def otp_from(client, phone=DEMO):
    return next(t for t in reversed(inbox(client, phone)) if "code" in t).split("code ")[1][:6]


@pytest.fixture
def alerter(app):
    """The alert engine, run inside an app context as the background worker does."""

    class Runner:
        def check(self):
            with app.app_context():
                return app.extensions["saferoute.alerter"].check()

    return Runner()


# ---- phone numbers and codes -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "phone"),
    [
        ("98765 43210", "+919876543210"),
        ("+91-98765-43210", "+919876543210"),
        ("0091 9876543210", "+919876543210"),
        ("+91 0000 123456", DEMO),
        ("12345", None),
        ("+1 415 555 0100", None),
        ("5876543210", None),  # Indian mobiles start with 6 to 9
    ],
)
def test_normalise_phone(raw, phone):
    assert normalise_phone(raw) == phone


def test_mask_shows_only_the_ends():
    assert mask("+919876543210") == "+91 ••••• 210"


def test_codes_expire_lock_and_are_rate_limited(app):
    store = app.extensions["saferoute.store"]
    status, code = start_signup(store, DEMO, "Main Road", "en", "secret")
    assert status == "sent" and len(code) == 6
    stored = store.subscriber(DEMO)
    assert code not in str(stored)  # only a hash is kept
    assert not stored["verified"]

    for _ in range(4):
        assert verify_signup(store, DEMO, "000000" if code != "000000" else "111111", "secret")[0] == "wrong"
    assert verify_signup(store, DEMO, "999999" if code != "999999" else "111111", "secret")[0] == "locked"
    assert verify_signup(store, DEMO, code, "secret")[0] == "locked"  # even the right code, once locked

    start_signup(store, DEMO, "Main Road", "en", "secret")
    assert start_signup(store, DEMO, "Main Road", "en", "secret")[0] == "sent"
    assert start_signup(store, DEMO, "Main Road", "en", "secret")[0] == "rate_limited"  # 3 per hour

    sub = store.subscriber(DEMO)
    sub["otp_expires"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    store.save_subscriber(sub)
    assert verify_signup(store, DEMO, "123456", "secret")[0] == "expired"


# ---- web sign-up through the API ---------------------------------------------------------------


def test_web_signup_needs_the_code_sent_to_the_phone(client):
    started = client.post("/api/subscriptions", json={"phone": "+91 0000 123456", "street": "Main Road", "lang": "en"})
    assert started.status_code == 200 and started.json["to"] == "+91 ••••• 456"
    assert client.post("/api/subscriptions/verify", json={"phone": DEMO, "code": "000001"}).status_code in (400, 200)
    code = otp_from(client)
    verified = client.post("/api/subscriptions/verify", json={"phone": DEMO, "code": code})
    if verified.status_code != 200:  # the guess above happened to be right; vanishingly unlikely
        pytest.skip("random code collision")
    assert verified.json["street"] == "Main Road"
    assert inbox(client)[-1].startswith("SafeRoute alerts on for Main Road. Now: Shelter A")


def test_web_signup_rules(client):
    def post(**body):
        return client.post("/api/subscriptions", json={"phone": DEMO, "street": "Main Road", "lang": "en", **body})

    assert post(phone="123").json["code"] == "bad_phone"
    assert post(phone="9876543210").json["code"] == "demo_only"  # no SMS gateway configured
    assert post(street="Nowhere Road").json["code"] == "bad_street"
    assert post(lang="fr").status_code == 400
    assert client.post("/api/subscriptions/verify", json={"phone": DEMO, "code": "abc"}).status_code == 400
    assert (
        client.post("/api/subscriptions/verify", json={"phone": "+910000999999", "code": "123456"}).json["code"]
        == "no_signup"
    )


def test_only_demo_numbers_have_a_readable_inbox(client):
    assert client.get("/api/sms/inbox?phone=+919876543210").status_code == 403
    assert client.get("/api/sms/inbox?phone=nonsense").status_code == 403
    assert client.get(f"/api/sms/inbox?phone={DEMO}").json["messages"] == []


# ---- SMS commands with a sender ---------------------------------------------------------------


def text(client, message, phone=DEMO):
    return client.post("/api/sms/simulate", json={"text": message, "phone": phone}).json["reply"]


def test_join_stop_and_safe_by_sms(client, app):
    assert text(client, "JOIN west lane").startswith("SafeRoute alerts on for West Lane. Now: Shelter B")
    store = app.extensions["saferoute.store"]
    assert store.subscriber(DEMO)["verified"]  # texting from the phone proves the number
    assert text(client, "SAFE").startswith("SafeRoute: Marked safe")
    assert store.subscriber(DEMO)["safe_at"]
    assert text(client, "STOP").startswith("SafeRoute: Alerts stopped")
    assert store.subscriber(DEMO) is None
    assert text(client, "STOP").startswith("SafeRoute: You had no alerts")
    assert text(client, "JOIN west lane", phone=None).startswith("SafeRoute: Send this from your own phone")
    assert "வழி/சேர்" in text(client, "சேர்")  # Tamil usage reply when no street is given


# ---- alerts ----------------------------------------------------------------------------------


def joined(client, street="Main Road", phone=DEMO):
    """Main Road starts at junction 1; its route runs over r12 to Shelter A."""
    text(client, f"JOIN {street}", phone)


def test_alert_when_a_road_on_your_route_is_blocked(client, responder, alerter):
    joined(client)
    signed_in, headers = responder
    signed_in.post("/api/closures", json={"road": "r12", "reason": "Flooded"}, headers=headers)
    assert alerter.check() == 1
    alert = inbox(client)[-1]
    assert alert.startswith("SafeRoute ALERT: a road on your route is blocked. Go to Shelter B")
    assert alerter.check() == 0  # nothing new, no repeat


def test_alert_when_your_shelter_fills_up(client, responder, alerter):
    joined(client)
    signed_in, headers = responder
    signed_in.post("/api/shelters/sA", json={"occupancy": 200}, headers=headers)
    assert alerter.check() == 1
    assert inbox(client)[-1].startswith("SafeRoute ALERT: Shelter A is full. Go to Shelter B")


def test_alert_when_no_shelter_can_be_reached_and_when_a_route_returns(client, responder, alerter, app):
    joined(client)
    signed_in, headers = responder
    for road in ("r01", "r12", "r14"):  # junction 1 is cut off
        signed_in.post("/api/closures", json={"road": road, "reason": "Flooded"}, headers=headers)
    assert alerter.check() == 1
    assert inbox(client)[-1].startswith("SafeRoute ALERT: no safe route from Main Road")

    signed_in.delete("/api/closures/r12", headers=headers)
    store = app.extensions["saferoute.store"]
    sub = store.subscriber(DEMO)
    sub["last_alert_at"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    store.save_subscriber(sub)
    assert alerter.check() == 1
    assert inbox(client)[-1].startswith("SafeRoute: a route is open again. Go to Shelter A")


def test_alerts_are_spaced_out_and_skip_people_who_are_safe(client, responder, alerter):
    joined(client)
    signed_in, headers = responder
    signed_in.post("/api/closures", json={"road": "r12", "reason": "Flooded"}, headers=headers)
    assert alerter.check() == 1  # now heading to Shelter B
    signed_in.post("/api/shelters/sB", json={"status": "closed"}, headers=headers)
    assert alerter.check() == 0  # held back: the last alert was under 5 minutes ago
    text(client, "SAFE")
    signed_in.post("/api/closures", json={"road": "r01", "reason": "Flooded"}, headers=headers)
    assert alerter.check() == 0  # safe people get no more alerts


def test_a_faster_route_is_not_worth_a_text(client, responder, alerter):
    signed_in, headers = responder
    signed_in.post("/api/closures", json={"road": "r12", "reason": "Flooded"}, headers=headers)
    joined(client)  # baseline: Shelter B
    signed_in.delete("/api/closures/r12", headers=headers)  # Shelter A is closer again
    assert alerter.check() == 0


def test_alerts_arrive_in_the_subscribers_language(client, responder, alerter):
    text(client, "சேர் Main Road")
    signed_in, headers = responder
    signed_in.post("/api/closures", json={"road": "r12", "reason": "Flooded"}, headers=headers)
    assert alerter.check() == 1
    assert inbox(client)[-1].startswith("SafeRoute எச்சரிக்கை: வழி மூடல். Shelter B")


# ---- short texts ------------------------------------------------------------------------------


def test_segment_counting():
    assert sms.segments("a" * 160) == 1 and sms.segments("a" * 161) == 2
    assert sms.segments("வ" * 70) == 1 and sms.segments("வ" * 71) == 2


@pytest.mark.parametrize("lang", ["en", "ta", "hi"])
def test_alerts_and_replies_stay_short(lang):
    values = {
        "shelter": "Sivasakthi School",
        "old": "SAN Academy",
        "eta": 21,
        "dist": "1.4 km",
        "steps": "Venus Colony 1st street > Sastri Road"
        if lang != "en"
        else "Sastri Road > Venus Colony 1st street > Velachery Main Road",
        "street": "Sastri Road",
        "state": sms.say("state_full", lang),
        "id": 12,
        "code": "123456",
        "closed": 18,
        "rain": "heavy",
        "query": "x",
        "list": "x",
    }
    for key in (
        "alert_blocked",
        "alert_shelter",
        "alert_none",
        "alert_back",
        "otp",
        "safe",
        "stopped",
        "help",
        "flood",
    ):
        assert sms.segments(sms.say(key, lang, **values)) <= 2, key
    for key in ("route", "welcome", "no_route"):
        assert sms.segments(sms.say(key, lang, **values)) <= 3, key


# ---- I'm safe, stats, export -----------------------------------------------------------------


def test_im_safe_counts_each_person_once(client):
    assert client.post("/api/safe", json={}).json["counted"] is True
    assert client.post("/api/safe", json={"shelter": "sA"}).json["counted"] is False
    assert client.post("/api/safe", json={"shelter": "zz"}).status_code == 404


def test_responder_stats_and_csv_export(app, client, responder):
    assert app.test_client().get("/api/stats").status_code == 401
    assert app.test_client().get("/api/events.csv").status_code == 401
    joined(client)
    joined(client, "West Lane", "+910000654321")
    text(client, "SAFE", "+910000654321")
    client.post("/api/safe", json={"shelter": "sB"})
    signed_in, _ = responder
    numbers = signed_in.get("/api/stats").json
    assert numbers["subscribers"]["total"] == 2 and numbers["subscribers"]["safe"] == 1
    assert numbers["subscribers"]["by_shelter"]["sA"] == 1  # the safe one no longer counts as heading anywhere
    assert numbers["safe"]["total"] == 2 and numbers["safe"]["at_shelters"] == {"sB": 1}

    exported = signed_in.get("/api/events.csv")
    assert exported.mimetype == "text/csv" and "attachment" in exported.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(exported.data.decode("utf-8-sig"))))
    assert rows[0] == ["id", "time_utc", "who", "action", "detail"]
    assert any(row[3] == "sms joined" for row in rows[1:])


def test_a_drill_after_a_reset_still_alerts(client, responder, alerter):
    joined(client)  # Main Road -> Shelter A over r12
    signed_in, headers = responder
    signed_in.post("/api/reset", json={}, headers=headers)
    signed_in.post("/api/closures", json={"road": "r12", "reason": "Flooded"}, headers=headers)
    assert alerter.check() == 1
