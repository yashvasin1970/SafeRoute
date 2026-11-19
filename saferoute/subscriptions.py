"""SMS alert subscriptions.

Two ways to sign up for alerts about your street:

* **By SMS:** text ``JOIN <street>``. The message itself comes from the phone, so the number is
  proven and alerts start at once.
* **On the web:** enter a number and a street. SafeRoute texts a 6-digit code to that number and
  the sign-up only takes effect once the code is typed back, so nobody can subscribe someone else.
  Codes are stored as salted hashes, expire after 10 minutes, allow 5 tries, and at most 3 codes are
  sent to a number per hour.

``STOP`` ends alerts; ``SAFE`` marks you as safe, which also pauses alerts.

Numbers are Indian mobiles (+91, starting 6 to 9). Numbers starting +91 0000 are the app's built-in
demo phone: never real, never sent to a gateway, and the only numbers whose messages can be read
back through the API.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from .store import Store

OTP_MINUTES = 10
MAX_ATTEMPTS = 5
MAX_SENDS_PER_HOUR = 3
DEMO_PREFIX = "+910000"


def normalise_phone(raw: str) -> str | None:
    text = re.sub(r"[\s\-().]", "", str(raw or ""))
    if text.startswith("+"):
        body = text[1:]
    elif text.startswith("00"):
        body = text[2:]
    else:
        body = text
    if not body.isdigit():
        return None
    if len(body) == 10:
        body = "91" + body
    if len(body) != 12 or not body.startswith("91"):
        return None
    national = body[2:]
    if national.startswith("0000") or national[0] in "6789":
        return "+" + body
    return None


def is_demo(phone: str) -> bool:
    return phone.startswith(DEMO_PREFIX)


def mask(phone: str) -> str:
    """+91 ••••• 456: enough for a responder to tell numbers apart, no more."""
    return f"{phone[:3]} ••••• {phone[-3:]}"


def _code_hash(secret: str, phone: str, code: str) -> str:
    return hmac.new(secret.encode(), f"{phone}|{code}".encode(), hashlib.sha256).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def start_signup(store: Store, phone: str, street: str, lang: str, secret: str) -> tuple[str, str | None]:
    """Prepare a web sign-up. Returns ("sent", code) or ("rate_limited", None)."""
    now = _now()
    sub = store.subscriber(phone) or {"phone": phone, "street": street, "lang": lang, "verified": False}
    recent = [t for t in sub.get("otp_sends", []) if now - datetime.fromisoformat(t) < timedelta(hours=1)]
    if len(recent) >= MAX_SENDS_PER_HOUR:
        return "rate_limited", None
    code = f"{secrets.randbelow(10**6):06d}"
    sub.update(
        pending_street=street,
        pending_lang=lang,
        otp_hash=_code_hash(secret, phone, code),
        otp_expires=(now + timedelta(minutes=OTP_MINUTES)).isoformat(timespec="seconds"),
        otp_attempts=0,
        otp_sends=[*recent, now.isoformat(timespec="seconds")],
    )
    store.save_subscriber(sub)
    return "sent", code


def verify_signup(store: Store, phone: str, code: str, secret: str) -> tuple[str, dict | None]:
    """Check a code. Returns (outcome, subscriber): verified, wrong, expired, locked or none."""
    sub = store.subscriber(phone)
    if not sub or not sub.get("otp_hash"):
        return "none", None
    if sub["otp_attempts"] >= MAX_ATTEMPTS:
        return "locked", None
    if _now() > datetime.fromisoformat(sub["otp_expires"]):
        return "expired", None
    if not hmac.compare_digest(sub["otp_hash"], _code_hash(secret, phone, str(code).strip())):
        sub["otp_attempts"] += 1
        store.save_subscriber(sub)
        return ("locked" if sub["otp_attempts"] >= MAX_ATTEMPTS else "wrong"), None
    sub.update(
        verified=True,
        street=sub["pending_street"],
        lang=sub["pending_lang"] or "en",
        pending_street=None,
        pending_lang=None,
        otp_hash=None,
        otp_expires=None,
        otp_attempts=0,
        shelter_id=None,
        route_roads=None,
        safe_at=None,
    )
    store.save_subscriber(sub, "Web sign-up", "sms joined", f"alerts for {sub['street']} ({mask(phone)})")
    return "verified", sub


def join_by_sms(store: Store, phone: str, street: str, lang: str) -> dict:
    """JOIN by text: the message came from the phone, so it is verified already."""
    sub = store.subscriber(phone) or {"phone": phone}
    sub.update(street=street, lang=lang, verified=True, shelter_id=None, route_roads=None, safe_at=None)
    store.save_subscriber(sub, "SMS", "sms joined", f"alerts for {street} ({mask(phone)})")
    return sub
