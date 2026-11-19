"""The flood story: a scripted evening in Velachery that drives the real system, step by step.

Each step changes live state through the same code paths as real sensors, residents and
responders: a rain warning, rising water, a resident report, an automatic closure, shelters
filling, an SMS rescue request, a responder verification, and finally the water receding.
Every open map reacts as it would in a real event, and a narration banner explains what is
happening in English, Tamil or Hindi.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from .feeds import WeatherFeed
from .network import RoadNetwork
from .reports import make_report, verify_report
from .store import Store

STORY_ACTOR = "Flood story"
RESPONDER = "NDRF Team 4 (story)"


@dataclass
class Step:
    en: str
    ta: str
    hi: str
    actions: tuple = ()


STEPS = (
    Step(
        "18:00 · Heavy rain warning: 45 mm of rain is expected in the next 3 hours. Routes start avoiding "
        "the low-lying roads around the four flood sensors.",
        "18:00 · கனமழை எச்சரிக்கை: அடுத்த 3 மணி நேரத்தில் 45 மி.மீ மழை எதிர்பார்க்கப்படுகிறது. நான்கு வெள்ள "
        "உணரிகளைச் சுற்றியுள்ள தாழ்வான சாலைகளை வழிகள் தவிர்க்கத் தொடங்குகின்றன.",
        "18:00 · भारी बारिश की चेतावनी: अगले 3 घंटों में 45 मिमी बारिश की संभावना। रास्ते चारों बाढ़ सेंसरों के "
        "आसपास की निचली सड़कों से बचने लगते हैं।",
        (("weather", 45, 12),),
    ),
    Step(
        "18:10 · Water is rising at Taramani Link Road (22 cm) and Velachery Main Road (17 cm). Routes now "
        "steer around both stretches.",
        "18:10 · தரமணி இணைப்புச் சாலையில் (22 செ.மீ) மற்றும் வேளச்சேரி பிரதான சாலையில் (17 செ.மீ) நீர்மட்டம் "
        "உயர்கிறது. வழிகள் இப்போது இரண்டையும் தவிர்க்கின்றன.",
        "18:10 · तारामणि लिंक रोड (22 सेमी) और वेलाचेरी मेन रोड (17 सेमी) पर पानी बढ़ रहा है। रास्ते अब दोनों हिस्सों से बचते हैं।",
        (("sensor", "FS-02", 22), ("sensor", "FS-01", 17)),
    ),
    Step(
        "18:20 · A resident reports water entering homes near Ram Nagar Main Road. It is not verified yet, "
        "but routes already avoid it.",
        "18:20 · ராம் நகர் பிரதான சாலை அருகே வீடுகளுக்குள் தண்ணீர் புகுவதாக ஒரு குடியிருப்பாளர் தெரிவிக்கிறார். "
        "இது இன்னும் உறுதிசெய்யப்படவில்லை, ஆனால் வழிகள் ஏற்கனவே அதைத் தவிர்க்கின்றன.",
        "18:20 · एक निवासी बताते हैं कि राम नगर मेन रोड के पास घरों में पानी घुस रहा है। यह अभी सत्यापित नहीं है, "
        "पर रास्ते पहले से इससे बचते हैं।",
        (("report", "ram_nagar", "flooding", 12.97305, 80.21650, "Water entering houses, knee deep", "app"),),
    ),
    Step(
        "18:30 · The Taramani Link Road sensor reads 34 cm. Every road within 80 m closes automatically, and "
        "every open map reroutes.",
        "18:30 · தரமணி இணைப்புச் சாலை உணரி 34 செ.மீ காட்டுகிறது. 80 மீட்டருக்குள் உள்ள எல்லாச் சாலைகளும் "
        "தானாக மூடப்பட்டு, திறந்திருக்கும் ஒவ்வொரு வரைபடத்திலும் வழி மாறுகிறது.",
        "18:30 · तारामणि लिंक रोड का सेंसर 34 सेमी दिखाता है। 80 मीटर के भीतर की सभी सड़कें अपने-आप बंद हो "
        "जाती हैं और हर खुला नक्शा नया रास्ता दिखाता है।",
        (("sensor", "FS-02", 34),),
    ),
    Step(
        "18:40 · SAN Academy is 92% full, with 40 more people on the way. New arrivals are sent to shelters "
        "that still have space.",
        "18:40 · SAN Academy 92% நிரம்பியுள்ளது, மேலும் 40 பேர் வந்துகொண்டிருக்கிறார்கள். புதிதாக வருபவர்கள் "
        "இடமுள்ள முகாம்களுக்கு அனுப்பப்படுகிறார்கள்.",
        "18:40 · SAN Academy 92% भर चुका है और 40 और लोग रास्ते में हैं। नए लोगों को उन शिविरों में भेजा जाता है जहाँ अभी जगह है।",
        (
            ("occupancy", "s2", 276),
            ("arrivals", "s2", 40),
            ("occupancy", "s1", 95),
            ("occupancy", "s3", 140),
            ("occupancy", "s4", 160),
            ("occupancy", "s5", 80),
        ),
    ),
    Step(
        "18:45 · A rescue request arrives by SMS from Velachery Bypass Road. Responders see it at once, with "
        "the caller's note.",
        "18:45 · வேளச்சேரி புறவழிச் சாலையிலிருந்து SMS மூலம் மீட்பு கோரிக்கை வருகிறது. மீட்புக் குழுவினர் அதை உடனே பார்க்கிறார்கள்.",
        "18:45 · वेलाचेरी बाईपास रोड से SMS द्वारा बचाव अनुरोध आता है। बचाव दल इसे तुरंत देखते हैं।",
        (("report", "rescue", "rescue", 12.98045, 80.21840, "Family of 4 on first floor, one elderly person", "sms"),),
    ),
    Step(
        "18:50 · A responder verifies the Ram Nagar report, which closes that road. Velachery Main Road also "
        "crosses 30 cm and closes.",
        "18:50 · மீட்புக் குழுவினர் ராம் நகர் தகவலை உறுதிசெய்து அந்தச் சாலையை மூடுகிறார்கள். வேளச்சேரி பிரதான "
        "சாலையும் 30 செ.மீ தாண்டி மூடப்படுகிறது.",
        "18:50 · बचाव दल राम नगर की सूचना की पुष्टि करते हैं, जिससे वह सड़क बंद हो जाती है। वेलाचेरी मेन रोड भी "
        "30 सेमी पार करके बंद हो जाती है।",
        (("verify", "ram_nagar"), ("sensor", "FS-01", 31)),
    ),
    Step(
        "19:10 · Akshaya is now full. It stays on the map but is no longer suggested to anyone.",
        "19:10 · Akshaya நிரம்பிவிட்டது. அது வரைபடத்தில் தெரியும், ஆனால் இனி யாருக்கும் பரிந்துரைக்கப்படாது.",
        "19:10 · Akshaya अब भर गया है। यह नक्शे पर दिखता है, पर अब किसी को सुझाया नहीं जाता।",
        (("occupancy", "s1", 150),),
    ),
    Step(
        "19:40 · The rain eases (5 mm expected). Water drops below 15 cm at Taramani Link Road and Velachery "
        "Main Road, and their roads reopen on their own.",
        "19:40 · மழை குறைகிறது (5 மி.மீ எதிர்பார்ப்பு). தரமணி இணைப்புச் சாலை மற்றும் வேளச்சேரி பிரதான சாலையில் "
        "நீர் 15 செ.மீக்குக் கீழே இறங்கி, சாலைகள் தானாகத் திறக்கின்றன.",
        "19:40 · बारिश थमती है (5 मिमी की संभावना)। तारामणि लिंक रोड और वेलाचेरी मेन रोड पर पानी 15 सेमी से नीचे "
        "आता है और सड़कें अपने-आप खुल जाती हैं।",
        (("weather", 5, 1), ("sensor", "FS-02", 12), ("sensor", "FS-01", 10)),
    ),
    Step(
        "End of the story. Responders can press Reset demo to start again.",
        "கதை முடிந்தது. மீண்டும் தொடங்க, மீட்புக் குழுவினர் 'டெமோவை மீட்டமை' அழுத்தலாம்.",
        "कहानी समाप्त। दोबारा शुरू करने के लिए बचाव दल 'डेमो रीसेट करें' दबा सकते हैं।",
    ),
)


# A one-click heavy-flood scenario for demos and drills, in the spirit of Cyclone Michaung (2023),
# when Velachery was among the worst-hit areas. It is illustrative: it floods the stretches around
# the sample sensors, not a record of the roads that actually closed.
FLOOD_SCENARIO_SENSORS = ("FS-01", "FS-02", "FS-03")
FLOOD_SCENARIO = (
    ("weather", 60, 18),
    ("sensor", "FS-02", 38),
    ("sensor", "FS-01", 34),
    ("sensor", "FS-03", 32),
    ("sensor", "FS-04", 24),
    ("occupancy", "s2", 240),
    ("occupancy", "s1", 120),
    ("occupancy", "s3", 150),
    ("occupancy", "s4", 180),
    ("occupancy", "s5", 90),
    ("report", "ram_nagar", "flooding", 12.97305, 80.21650, "Water entering houses, knee deep", "app"),
    ("report", "rescue", "rescue", 12.98045, 80.21840, "Elderly couple, ground floor flooded", "sms"),
)


class StoryRunner:
    """Plays STEPS one at a time on a background thread (or synchronously in tests)."""

    def __init__(self, store: Store, network: RoadNetwork, weather: WeatherFeed, step_s: float, secret: str):
        self.store, self.network, self.weather = store, network, weather
        self.step_s = step_s
        self.secret = secret
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ids: dict[str, int] = {}
        self.on_change: Callable[[], None] | None = None  # e.g. wake the SMS alerter

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, actor: str, sync: bool = False, sleep: Callable[[float], bool] | None = None) -> bool:
        if self.running:
            return False
        self._stop.clear()
        self.store.reset(actor)
        self.weather.stop_drill()
        if sync:
            self._run(sleep or (lambda s: False))
            return True
        self._thread = threading.Thread(target=self._run, args=(self._stop.wait,), daemon=True)
        self._thread.start()
        return True

    def flood_scenario(self, actor: str) -> None:
        """Load the heavy-flood scenario at once (resets the demo first)."""
        self.stop()
        self.store.reset(actor)
        self._ids = {}
        for action in FLOOD_SCENARIO:
            self._apply(action)
        self.store.set_feed(
            "scenario",
            {"name": "flood"},
            actor=actor,
            action="scenario",
            detail="heavy-flood scenario loaded (Michaung-style, illustrative)",
        )
        if self.on_change:
            self.on_change()

    def flood_roads(self) -> list[str]:
        """The roads the flood scenario closes, for the planner to test without publishing."""
        return sorted(
            {r for s in FLOOD_SCENARIO_SENSORS if s in self.network.sensors for r in self.network.sensors[s].roads}
        )

    def stop(self) -> None:
        self._stop.set()
        self.weather.stop_drill()
        self.store.set_feed("story", None)

    def _run(self, sleep: Callable[[float], bool]) -> None:
        self._ids = {}
        for index, step in enumerate(STEPS):
            if self._stop.is_set():
                return
            for action in step.actions:
                self._apply(action)
            if self.on_change and step.actions:
                self.on_change()
            self.store.set_feed(
                "story",
                {
                    "active": index < len(STEPS) - 1,
                    "step": index + 1,
                    "total": len(STEPS),
                    "text": {"en": step.en, "ta": step.ta, "hi": step.hi},
                },
            )
            if index < len(STEPS) - 1 and sleep(self.step_s):
                return  # stopped while waiting
        self.weather.stop_drill()

    def _apply(self, action: tuple) -> None:
        """Run one action. The story is written for the Velachery sample; on another map, actions
        that name a sensor or shelter the map doesn't have are skipped."""
        kind = action[0]
        if kind == "weather":
            self.weather.start_drill(next3h_mm=action[1], rain_now_mm=action[2], actor=STORY_ACTOR)
        elif kind == "sensor" and action[1] in self.network.sensors:
            sensor = self.network.sensors[action[1]]
            self.store.record_reading(sensor.id, sensor.roads, float(action[2]), sensor.label)
        elif kind == "occupancy" and action[1] in self.network.shelters:
            shelter = self.network.shelters[action[1]]
            self.store.set_occupancy(shelter.id, action[2], RESPONDER, shelter.name, shelter.capacity)
        elif kind == "arrivals" and action[1] in self.network.shelters:
            self.store.add_arrival(action[1], action[2], f"story-{action[1]}", minutes=60, max_active=99)
        elif kind == "report":
            _, key, report_kind, lat, lon, note, source = action
            report = make_report(self.network, report_kind, lat, lon, note, source, "story")
            if report:
                self._ids[key] = self.store.add_report(report, "SMS" if source == "sms" else "Resident")
        elif kind == "verify":
            report = self.store.report(self._ids.get(action[1], -1))
            if report:
                verify_report(self.store, self.network, report, RESPONDER)
