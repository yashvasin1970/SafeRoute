"""Live weather for the area, from Open-Meteo (free, no key), with a drill override for demos.

Rain is rated by the forecast for the next three hours:

    dry       under 0.5 mm      moderate  7.5 to 20 mm      extreme   50 mm or more
    light     0.5 to 7.5 mm     heavy     20 to 50 mm

Heavy and extreme rain raise the risk of every sensor's low-lying stretch (see ``risk.py``), so
routes start avoiding the places that flood first before any water is reported.

The feed refreshes itself in the background at most every ten minutes, when someone asks for the
state. If the weather service can't be reached, the app keeps working and says so. During a flood
story or drill, a simulated reading overrides the live one.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable

from .store import Store, now_iso

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
REFRESH_S = 600
LEVELS = ("dry", "light", "moderate", "heavy", "extreme")


def rain_level(next3h_mm: float) -> str:
    if next3h_mm < 0.5:
        return "dry"
    if next3h_mm < 7.5:
        return "light"
    if next3h_mm < 20:
        return "moderate"
    if next3h_mm < 50:
        return "heavy"
    return "extreme"


def parse_open_meteo(payload: dict) -> dict:
    hourly = [float(x or 0) for x in payload.get("hourly", {}).get("precipitation", [])[:3]]
    next3h = round(sum(hourly), 1)
    return {
        "source": "open-meteo",
        "rain_now_mm": round(float(payload.get("current", {}).get("precipitation") or 0), 1),
        "next3h_mm": next3h,
        "level": rain_level(next3h),
        "updated_at": now_iso(),
    }


def fetch_open_meteo(lat: float, lon: float, timeout: float = 8.0) -> dict:
    query = urllib.parse.urlencode(
        {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "current": "precipitation",
            "hourly": "precipitation",
            "forecast_hours": 4,
            "timezone": "auto",
        }
    )
    request = urllib.request.Request(f"{OPEN_METEO}?{query}", headers={"User-Agent": "SafeRoute/0.2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_open_meteo(json.loads(response.read()))


class WeatherFeed:
    """Cached weather for one point. ``fetch`` is injectable so tests never touch the network."""

    def __init__(
        self,
        store: Store,
        lat: float,
        lon: float,
        enabled: bool = True,
        fetch: Callable[[float, float], dict] | None = None,
    ):
        self.store = store
        self.lat, self.lon = lat, lon
        self.enabled = enabled
        self.fetch = fetch or fetch_open_meteo
        self._lock = threading.Lock()
        self._last_try = 0.0

    def current(self) -> dict:
        """The drill reading if one is running, else the cached live reading (refreshing if stale)."""
        drill = self.store.feed("weather_drill")
        if drill:
            return drill
        cached = self.store.feed("weather")
        if self.enabled and time.monotonic() - self._last_try > REFRESH_S:
            threading.Thread(target=self.refresh, daemon=True).start()
        return cached or {"source": "unavailable" if not self.enabled else "loading", "level": None}

    def refresh(self) -> dict | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            self._last_try = time.monotonic()
            try:
                reading = self.fetch(self.lat, self.lon)
            except Exception:  # any network or parsing problem: keep the last reading
                return None
            previous = self.store.feed("weather") or {}
            changed = previous.get("level") != reading["level"]
            self.store.set_feed(
                "weather",
                reading,
                actor="Weather feed" if changed else "",
                action="weather" if changed and previous else "",
                detail=f"rain {reading['level']}: {reading['next3h_mm']} mm expected in the next 3 hours",
            )
            return reading
        finally:
            self._lock.release()

    def start_drill(self, next3h_mm: float, rain_now_mm: float, actor: str) -> dict:
        reading = {
            "source": "drill",
            "rain_now_mm": rain_now_mm,
            "next3h_mm": next3h_mm,
            "level": rain_level(next3h_mm),
            "updated_at": now_iso(),
        }
        self.store.set_feed(
            "weather_drill",
            reading,
            actor=actor,
            action="weather",
            detail=f"rain {reading['level']}: {next3h_mm:g} mm expected in the next 3 hours (drill)",
        )
        return reading

    def stop_drill(self) -> None:
        self.store.set_feed("weather_drill", None)
