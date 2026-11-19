"""Plain-text SMS, for people with no data connection or a basic phone.

    ROUTE <street>   the way to the best open shelter, on foot
    JOIN <street>    alerts for your street (STOP to end)
    SHELTERS         which shelters are open and how full they are
    SAFE             tell responders you are safe (also pauses alerts)
    FLOOD <street>   report flooding (unverified until a responder checks it)
    HELP <street>    ask to be rescued
    STATUS           roads blocked and the rain forecast

Commands also work in Tamil and Hindi, and the reply comes back in the same language. Street
names are matched loosely, so "sastri rd" finds Sastri Road.

Messages in Tamil or Hindi travel as Unicode SMS, which fit 70 characters instead of 160, so every
text here is kept short: replies and alerts aim for two Unicode segments at most. Shelters use their
short SMS names for the same reason.
"""

from __future__ import annotations

import difflib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass

from .network import RoadNetwork

KEYWORDS = {
    "route": {"ROUTE", "WAY", "வழி", "रास्ता"},
    "join": {"JOIN", "START", "சேர்", "जुड़ें"},
    "stop": {"STOP", "நிறுத்து", "रोकें"},
    "safe": {"SAFE", "பாதுகாப்பு", "सुरक्षित"},
    "shelters": {"SHELTERS", "SHELTER", "CAMPS", "முகாம்", "முகாம்கள்", "शिविर"},
    "flood": {"FLOOD", "WATER", "வெள்ளம்", "बाढ़"},
    "help": {"HELP", "RESCUE", "SOS", "உதவி", "मदद", "बचाओ"},
    "status": {"STATUS", "நிலை", "स्थिति"},
}
LANG_PREFIX = {"EN": "en", "TA": "ta", "HI": "hi"}
ABBREVIATIONS = {"rd": "road", "st": "street", "ext": "extension", "ave": "avenue", "mn": "main"}
STEPS_PER_LANG = {"en": 3, "ta": 2, "hi": 2}

TEXT = {
    "en": {
        "route": "SafeRoute: Walk to {shelter}, {eta} min ({dist}) via {steps}. Rescue: HELP {street}",
        "no_route": "SafeRoute: No safe route from {street} now. Stay high, call 100. Rescue: HELP {street}",
        "help": "SafeRoute: Rescue request #{id} near {street} sent to responders. Stay high, keep phone on. 100",
        "flood": "SafeRoute: Flooding near {street} logged (#{id}). Routes now avoid it.",
        "shelters": "SafeRoute shelters: {list}. Directions: ROUTE <street>",
        "status": "SafeRoute: {closed} roads blocked. Rain: {rain}. Directions: ROUTE <street>",
        "unknown_street": "SafeRoute: Can't find '{query}'. Try a main road, e.g. ROUTE Sastri Road",
        "usage": "SafeRoute: Send ROUTE <street>, JOIN <street>, SHELTERS, SAFE or HELP <street>",
        "need_phone": "SafeRoute: Send this from your own phone.",
        "welcome": "SafeRoute alerts on for {street}. Now: {shelter}, {eta} min. Reply SAFE when safe, STOP to end.",
        "stopped": "SafeRoute: Alerts stopped. JOIN <street> to restart.",
        "not_joined": "SafeRoute: You had no alerts. JOIN <street> to start.",
        "safe": "SafeRoute: Marked safe. Thank you. HELP <street> if that changes.",
        "otp": "SafeRoute code {code}. Valid 10 min. Didn't ask? Ignore this.",
        "alert_blocked": "SafeRoute ALERT: a road on your route is blocked. Go to {shelter}, {eta} min via {steps}.",
        "alert_shelter": "SafeRoute ALERT: {old} is {state}. Go to {shelter}, {eta} min via {steps}.",
        "alert_none": "SafeRoute ALERT: no safe route from {street}. Stay high, call 100. HELP {street}",
        "alert_back": "SafeRoute: a route is open again. Go to {shelter}, {eta} min via {steps}.",
        "state_full": "full",
        "state_closed": "closed",
        "state_cut": "cut off",
        "full": "{name} full",
        "load": "{name} {pct}%",
        "closed": "{name} closed",
        "rain": {"dry": "none", "light": "light", "moderate": "moderate", "heavy": "heavy", "extreme": "very heavy"},
        "rain_unknown": "unknown",
    },
    "ta": {
        "route": "SafeRoute: {shelter}, நடந்து {eta} நிமி ({dist}). வழி: {steps}. மீட்பு: உதவி {street}",
        "no_route": "SafeRoute: {street} இலிருந்து வழி இல்லை. உயரத்தில் இருங்கள். 100. மீட்பு: உதவி {street}",
        "help": "SafeRoute: மீட்பு கோரிக்கை #{id} அனுப்பப்பட்டது. மேல் தளத்தில் இருங்கள். 100",
        "flood": "SafeRoute: வெள்ளம் பதிவானது (#{id}). வழிகள் தவிர்க்கும்.",
        "shelters": "SafeRoute முகாம்கள்: {list}",
        "status": "SafeRoute: {closed} சாலைகள் மூடல். மழை: {rain}",
        "unknown_street": "SafeRoute: '{query}' இல்லை. எ.கா. வழி Sastri Road",
        "usage": "SafeRoute: வழி/சேர் <தெரு>, முகாம், SAFE, உதவி <தெரு>",
        "need_phone": "SafeRoute: உங்கள் போனிலிருந்து அனுப்பவும்.",
        "welcome": "SafeRoute: {street} எச்சரிக்கை இயக்கம். முகாம் {shelter}, {eta} நிமி. நிறுத்த STOP",
        "stopped": "SafeRoute: எச்சரிக்கை நிறுத்தப்பட்டது.",
        "not_joined": "SafeRoute: எச்சரிக்கை இல்லை. சேர் <தெரு>",
        "safe": "SafeRoute: பாதுகாப்பு பதிவானது. நன்றி.",
        "otp": "SafeRoute குறியீடு {code}. 10 நிமி செல்லும்.",
        "alert_blocked": "SafeRoute எச்சரிக்கை: வழி மூடல். {shelter}, {eta} நிமி: {steps}",
        "alert_shelter": "SafeRoute எச்சரிக்கை: {old} {state}. {shelter}, {eta} நிமி: {steps}",
        "alert_none": "SafeRoute எச்சரிக்கை: {street} இலிருந்து வழி இல்லை. உயரத்தில் இருங்கள். 100",
        "alert_back": "SafeRoute: வழி திறந்தது. {shelter}, {eta} நிமி: {steps}",
        "state_full": "நிரம்பியது",
        "state_closed": "மூடப்பட்டது",
        "state_cut": "துண்டிப்பு",
        "full": "{name} நிரம்பியது",
        "load": "{name} {pct}%",
        "closed": "{name} மூடல்",
        "rain": {"dry": "இல்லை", "light": "லேசான", "moderate": "மிதமான", "heavy": "கனமழை", "extreme": "மிகக் கனமழை"},
        "rain_unknown": "தெரியவில்லை",
    },
    "hi": {
        "route": "SafeRoute: {shelter}, पैदल {eta} मिनट ({dist})। रास्ता: {steps}। बचाव: मदद {street}",
        "no_route": "SafeRoute: {street} से रास्ता नहीं। ऊँचाई पर रहें। 100। बचाव: मदद {street}",
        "help": "SafeRoute: बचाव अनुरोध #{id} भेजा गया। ऊपर रहें, फ़ोन चालू रखें। 100",
        "flood": "SafeRoute: बाढ़ दर्ज (#{id})। रास्ते इससे बचेंगे।",
        "shelters": "SafeRoute शिविर: {list}",
        "status": "SafeRoute: {closed} सड़कें बंद। बारिश: {rain}",
        "unknown_street": "SafeRoute: '{query}' नहीं मिला। जैसे रास्ता Sastri Road",
        "usage": "SafeRoute: रास्ता/जुड़ें <गली>, शिविर, SAFE, मदद <गली>",
        "need_phone": "SafeRoute: अपने फ़ोन से भेजें।",
        "welcome": "SafeRoute: {street} अलर्ट चालू। शिविर {shelter}, {eta} मिनट। बंद: STOP",
        "stopped": "SafeRoute: अलर्ट बंद।",
        "not_joined": "SafeRoute: अलर्ट नहीं थे। जुड़ें <गली>",
        "safe": "SafeRoute: सुरक्षित दर्ज। धन्यवाद।",
        "otp": "SafeRoute कोड {code}। 10 मिनट मान्य।",
        "alert_blocked": "SafeRoute अलर्ट: रास्ता बंद। {shelter}, {eta} मिनट: {steps}",
        "alert_shelter": "SafeRoute अलर्ट: {old} {state}। {shelter}, {eta} मिनट: {steps}",
        "alert_none": "SafeRoute अलर्ट: {street} से रास्ता नहीं। ऊँचाई पर रहें। 100",
        "alert_back": "SafeRoute: रास्ता खुला। {shelter}, {eta} मिनट: {steps}",
        "state_full": "भरा",
        "state_closed": "बंद",
        "state_cut": "कटा",
        "full": "{name} भरा",
        "load": "{name} {pct}%",
        "closed": "{name} बंद",
        "rain": {"dry": "नहीं", "light": "हल्की", "moderate": "मध्यम", "heavy": "भारी", "extreme": "बहुत भारी"},
        "rain_unknown": "पता नहीं",
    },
}

# The GSM 03.38 basic alphabet: text using only these characters fits 160 per SMS.
GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)


def segments(text: str) -> int:
    """How many SMS a text needs: 160/153 characters each in GSM-7, 70/67 in Unicode."""
    if set(text) <= GSM7:
        return 1 if len(text) <= 160 else math.ceil(len(text) / 153)
    return 1 if len(text) <= 70 else math.ceil(len(text) / 67)


def say(key: str, lang: str, **values) -> str:
    return TEXT.get(lang, TEXT["en"])[key].format(**values)


def minutes(seconds: float) -> int:
    return max(1, round(seconds / 60))


def distance(metres: float) -> str:
    return f"{round(metres / 10) * 10} m" if metres < 1000 else f"{metres / 1000:.1f} km"


def steps_text(route: dict, lang: str) -> str:
    names = [s["name"] for s in route["steps"] if s["name"] != "Unnamed road"]
    return " > ".join(list(dict.fromkeys(names))[: STEPS_PER_LANG.get(lang, 3)]) or "-"


@dataclass
class SmsServices:
    """What the SMS handler needs from the rest of the app."""

    network: RoadNetwork
    route: Callable[[float, float], dict]  # lat, lon -> the /api/route response body
    report: Callable[[str, float, float, str, str], int]  # kind, lat, lon, note, street -> report id
    shelters: Callable[[], list[dict]]  # [{name, status, load_pct}]
    status: Callable[[], dict]  # {closed, rain_level}
    phone: str | None = None  # the sender, when known
    join: Callable[[str, str, str], str] | None = None  # phone, street, lang -> welcome text
    stop: Callable[[str], bool] | None = None  # phone -> was subscribed
    safe: Callable[[str], None] | None = None  # phone


def script_language(word: str) -> str:
    if re.search(r"[஀-௿]", word):
        return "ta"
    if re.search(r"[ऀ-ॿ]", word):
        return "hi"
    return "en"


def normalise(text: str) -> str:
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    return " ".join(ABBREVIATIONS.get(w, w) for w in words)


def find_street(network: RoadNetwork, query: str) -> str | None:
    wanted = normalise(query)
    if not wanted:
        return None
    names = {normalise(name): name for name in network.streets}
    if wanted in names:
        return names[wanted]
    containing = [name for key, name in names.items() if len(wanted) >= 3 and wanted in key]
    if containing:
        return min(containing, key=len)
    close = difflib.get_close_matches(wanted, list(names), n=1, cutoff=0.8)  # typos yes, other words no
    return names[close[0]] if close else None


def parse(text: str) -> tuple[str | None, str, str]:
    """Returns (command, language, rest of the message)."""
    words = text.strip().split()
    lang = None
    if words and words[0].upper() in LANG_PREFIX:
        lang = LANG_PREFIX[words.pop(0).upper()]
    if not words:
        return None, lang or "en", ""
    first = words[0]
    for command, keys in KEYWORDS.items():
        if first.upper() in keys or first in keys:
            return command, lang or script_language(first), " ".join(words[1:])
    return None, lang or script_language(text), " ".join(words)


def handle(text: str, services: SmsServices) -> tuple[str, str]:
    """Answer one incoming message. Returns (reply, language)."""
    command, lang, rest = parse(text[:320])
    t = TEXT[lang]

    if command == "shelters":
        items = []
        for shelter in services.shelters():
            key = {"closed": "closed", "full": "full"}.get(shelter["status"], "load")
            items.append(t[key].format(name=shelter["name"], pct=shelter["load_pct"]))
        return t["shelters"].format(list="; ".join(items)), lang

    if command == "status":
        info = services.status()
        rain = t["rain"].get(info.get("rain_level") or "", t["rain_unknown"])
        return t["status"].format(closed=info["closed"], rain=rain), lang

    if command in ("stop", "safe"):
        if not services.phone:
            return t["need_phone"], lang
        if command == "stop":
            return t["stopped" if services.stop(services.phone) else "not_joined"], lang
        services.safe(services.phone)
        return t["safe"], lang

    if command in ("route", "flood", "help", "join"):
        street = find_street(services.network, rest)
        if street is None:
            return (t["unknown_street"] if rest else t["usage"]).format(query=rest[:40]), lang
        if command == "join":
            if not services.phone:
                return t["need_phone"], lang
            return services.join(services.phone, street, lang), lang
        lat, lon = services.network.street_point(street)
        if command == "route":
            route = services.route(lat, lon).get("route")
            if not route:
                return t["no_route"].format(street=street), lang
            shelter = services.network.shelters[route["shelter"]["id"]]
            reply = t["route"].format(
                shelter=shelter.sms_name,
                eta=minutes(route["eta_s"]),
                dist=distance(route["distance_m"]),
                steps=steps_text(route, lang),
                street=street,
            )
            return reply, lang
        kind = "flooding" if command == "flood" else "rescue"
        report_id = services.report(kind, lat, lon, text.strip()[:200], street)
        return t["flood" if command == "flood" else "help"].format(street=street, id=report_id), lang

    return t["usage"], lang
