"""Plain-text SMS commands, for people with no data connection or a basic phone.

    ROUTE <street>      the way to the nearest open shelter, on foot
    SHELTERS            which shelters are open and how full they are
    FLOOD <street>      report flooding (unverified until a responder checks it)
    HELP <street>       ask to be rescued
    STATUS              roads blocked and the rain forecast

Commands also work in Tamil and Hindi (வழி / रास्ता, முகாம் / शिविर, வெள்ளம் / बाढ़,
உதவி / मदद, நிலை / स्थिति) and the reply comes back in the same language. Street names
are matched loosely, so "sastri rd" finds Sastri Road.

``handle`` is pure apart from the callbacks it is given, so the same logic serves a real SMS
gateway webhook and the in-app phone simulator.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass

from .network import RoadNetwork

KEYWORDS = {
    "route": {"ROUTE", "SAFE", "WAY", "வழி", "रास्ता"},
    "shelters": {"SHELTERS", "SHELTER", "CAMPS", "முகாம்", "முகாம்கள்", "शिविर"},
    "flood": {"FLOOD", "WATER", "வெள்ளம்", "बाढ़"},
    "help": {"HELP", "RESCUE", "SOS", "உதவி", "मदद", "बचाओ"},
    "status": {"STATUS", "நிலை", "स्थिति"},
}
LANG_PREFIX = {"EN": "en", "TA": "ta", "HI": "hi"}
ABBREVIATIONS = {"rd": "road", "st": "street", "ext": "extension", "ave": "avenue", "mn": "main"}

TEXT = {
    "en": {
        "route": "SafeRoute: Walk to {shelter}, {eta} min ({dist}). Way: {steps}. Need rescue? Reply HELP {street}",
        "no_route": "SafeRoute: No open shelter can be reached from {street} now. Stay on high ground, call 112. "
        "Reply HELP {street} for rescue.",
        "help": "SafeRoute: Rescue request logged near {street} (#{id}). Responders can see it now. Stay on the "
        "highest floor, keep your phone on. Emergency: 112",
        "flood": "SafeRoute: Thank you. Flooding near {street} reported (#{id}). Responders will check it; routes "
        "already avoid it.",
        "shelters": "SafeRoute shelters: {list}. Reply ROUTE <your street> for directions.",
        "status": "SafeRoute: {closed} road segments blocked. Rain: {rain}. Reply ROUTE <your street>.",
        "unknown_street": "SafeRoute: Couldn't find '{query}'. Send a main road near you, e.g. ROUTE Sastri Road",
        "usage": "SafeRoute: Send ROUTE <your street>, SHELTERS, FLOOD <street> or HELP <street>.",
        "full": "{name} full",
        "load": "{name} {pct}% full",
        "closed": "{name} closed",
        "rain": {"dry": "none", "light": "light", "moderate": "moderate", "heavy": "heavy", "extreme": "very heavy"},
        "rain_unknown": "unknown",
    },
    "ta": {
        "route": "SafeRoute: அருகிலுள்ள திறந்த முகாம் {shelter}, நடந்து {eta} நிமிடம் ({dist}). வழி: {steps}. "
        "மீட்பு தேவையா? அனுப்பவும்: உதவி {street}",
        "no_route": "SafeRoute: {street} இலிருந்து இப்போது எந்த முகாமையும் அடைய முடியாது. உயரமான இடத்தில் "
        "இருங்கள், 112 ஐ அழைக்கவும். மீட்புக்கு: உதவி {street}",
        "help": "SafeRoute: {street} அருகே மீட்பு கோரிக்கை பதிவானது (#{id}). மீட்புக் குழுவினர் இதைப் "
        "பார்க்கிறார்கள். மேல் தளத்தில் இருங்கள். அவசரம்: 112",
        "flood": "SafeRoute: நன்றி. {street} அருகே வெள்ளம் பதிவானது (#{id}). மீட்புக் குழுவினர் சரிபார்ப்பார்கள்; "
        "வழிகள் ஏற்கனவே அதைத் தவிர்க்கின்றன.",
        "shelters": "SafeRoute முகாம்கள்: {list}. வழி அறிய: வழி <உங்கள் தெரு>",
        "status": "SafeRoute: {closed} சாலைப் பகுதிகள் மூடப்பட்டுள்ளன. மழை: {rain}. வழி அறிய: வழி <உங்கள் தெரு>",
        "unknown_street": "SafeRoute: '{query}' கிடைக்கவில்லை. அருகிலுள்ள முக்கிய சாலையை அனுப்பவும், எ.கா. வழி Sastri Road",
        "usage": "SafeRoute: அனுப்பவும்: வழி <தெரு>, முகாம், வெள்ளம் <தெரு> அல்லது உதவி <தெரு>",
        "full": "{name} நிரம்பியது",
        "load": "{name} {pct}% நிரம்பியது",
        "closed": "{name} மூடப்பட்டது",
        "rain": {"dry": "இல்லை", "light": "லேசான", "moderate": "மிதமான", "heavy": "கனமழை", "extreme": "மிகக் கனமழை"},
        "rain_unknown": "தெரியவில்லை",
    },
    "hi": {
        "route": "SafeRoute: सबसे नज़दीकी खुला शिविर {shelter}, पैदल {eta} मिनट ({dist})। रास्ता: {steps}। "
        "बचाव चाहिए? भेजें: मदद {street}",
        "no_route": "SafeRoute: {street} से अभी किसी शिविर तक नहीं पहुँचा जा सकता। ऊँची जगह पर रहें, 112 पर "
        "कॉल करें। बचाव के लिए भेजें: मदद {street}",
        "help": "SafeRoute: {street} के पास बचाव अनुरोध दर्ज हुआ (#{id})। बचाव दल इसे अभी देख रहे हैं। सबसे ऊपरी "
        "मंज़िल पर रहें, फ़ोन चालू रखें। आपातकाल: 112",
        "flood": "SafeRoute: धन्यवाद। {street} के पास बाढ़ की सूचना दर्ज हुई (#{id})। बचाव दल इसकी जाँच करेंगे; "
        "रास्ते पहले से इससे बचते हैं।",
        "shelters": "SafeRoute शिविर: {list}। रास्ते के लिए भेजें: रास्ता <आपकी गली>",
        "status": "SafeRoute: {closed} सड़क हिस्से बंद हैं। बारिश: {rain}। रास्ते के लिए भेजें: रास्ता <आपकी गली>",
        "unknown_street": "SafeRoute: '{query}' नहीं मिला। पास की किसी मुख्य सड़क का नाम भेजें, जैसे रास्ता Sastri Road",
        "usage": "SafeRoute: भेजें: रास्ता <गली>, शिविर, बाढ़ <गली> या मदद <गली>",
        "full": "{name} भरा हुआ",
        "load": "{name} {pct}% भरा",
        "closed": "{name} बंद",
        "rain": {"dry": "नहीं", "light": "हल्की", "moderate": "मध्यम", "heavy": "भारी", "extreme": "बहुत भारी"},
        "rain_unknown": "पता नहीं",
    },
}


@dataclass
class SmsServices:
    """What the SMS handler needs from the rest of the app."""

    network: RoadNetwork
    route: Callable[[float, float], dict]  # lat, lon -> the /api/route response body
    report: Callable[[str, float, float, str, str], int]  # kind, lat, lon, note, street -> report id
    shelters: Callable[[], list[dict]]  # [{name, status, load_pct}]
    status: Callable[[], dict]  # {closed, rain_level}


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


def _distance(metres: float) -> str:
    return f"{round(metres / 10) * 10} m" if metres < 1000 else f"{metres / 1000:.1f} km"


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
            if shelter["status"] == "closed":
                items.append(t["closed"].format(name=shelter["name"]))
            elif shelter["status"] == "full":
                items.append(t["full"].format(name=shelter["name"]))
            else:
                items.append(t["load"].format(name=shelter["name"], pct=shelter["load_pct"]))
        return t["shelters"].format(list="; ".join(items)), lang

    if command == "status":
        info = services.status()
        rain = t["rain"].get(info.get("rain_level") or "", t["rain_unknown"])
        return t["status"].format(closed=info["closed"], rain=rain), lang

    if command in ("route", "flood", "help"):
        street = find_street(services.network, rest)
        if street is None:
            return (t["unknown_street"] if rest else t["usage"]).format(query=rest[:40]), lang
        lat, lon = services.network.street_point(street)
        if command == "route":
            result = services.route(lat, lon)
            route = result.get("route")
            if not route:
                return t["no_route"].format(street=street), lang
            names = [s["name"] for s in route["steps"] if s["name"] != "Unnamed road"]
            steps = " > ".join(dict.fromkeys(names[:3])) or street
            reply = t["route"].format(
                shelter=route["shelter"]["name"],
                eta=max(1, round(route["eta_s"] / 60)),
                dist=_distance(route["distance_m"]),
                steps=steps,
                street=street,
            )
            return reply, lang
        kind = "flooding" if command == "flood" else "rescue"
        report_id = services.report(kind, lat, lon, text.strip()[:200], street)
        return t["flood" if command == "flood" else "help"].format(street=street, id=report_id), lang

    return t["usage"], lang
