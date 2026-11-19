"""How risky each open road is right now, from the live feeds.

Closed roads are removed from the graph entirely. Risk is for roads that are still open but
worrying. In "safest" mode, routing multiplies a road's travel time by (1 + risk), so a route
prefers other roads unless the detour is long.

Where risk comes from:

* **Water rising at a sensor** (caution band, 15 to 30 cm): every road the sensor watches, +1.0.
* **Unverified resident reports** of flooding or a blocked road: roads within 60 m, +1.5.
  Reports alone never close a road; only responders and sensors do. They make routes avoid the
  spot until a responder verifies or dismisses them.
* **Heavy rain** from the weather feed: every sensor's low-lying stretch, +0.5, because those
  are the places that flood first.

Risk is capped at 4, so a risky road costs at most five times its normal travel time.
Each reason is kept in a structured form so the app can explain it in any language.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from . import sensors
from .network import RoadNetwork

RISING = 1.0
REPORTED = 1.5
HEAVY_RAIN = 0.5
MAX_RISK = 4.0
REPORT_RADIUS_M = 60
HEAVY_RAIN_LEVELS = frozenset({"heavy", "extreme"})
REPORT_KINDS_THAT_AFFECT_ROUTES = frozenset({"flooding", "blocked"})


@dataclass(frozen=True)
class Reason:
    type: str  # "rising", "report" or "rain"
    place: str  # a sensor's area label, or the report kind
    value: float | None = None  # water depth for "rising"

    def as_dict(self) -> dict:
        return {"type": self.type, "place": self.place, "value": self.value}


@dataclass(frozen=True)
class Risk:
    factor: float
    reasons: tuple[Reason, ...]


def road_risk(
    network: RoadNetwork,
    sensor_levels: Mapping[str, float],
    reports: Iterable[Mapping],
    rain_level: str | None,
    closed: Iterable[str] = (),
) -> dict[str, Risk]:
    """Risk for every open road that has any. Roads with no risk are left out."""
    closed = frozenset(closed)
    totals: dict[str, float] = {}
    reasons: dict[str, list[Reason]] = {}

    def add(road_id: str, amount: float, reason: Reason) -> None:
        if road_id in closed:
            return
        totals[road_id] = totals.get(road_id, 0.0) + amount
        bucket = reasons.setdefault(road_id, [])
        if reason not in bucket:
            bucket.append(reason)

    for sensor in network.sensors.values():
        level = float(sensor_levels.get(sensor.id, 0.0))
        if sensors.band(level) == "caution":
            for road_id in sensor.roads:
                add(road_id, RISING, Reason("rising", sensor.label, level))
        if rain_level in HEAVY_RAIN_LEVELS:
            for road_id in sensor.roads:
                add(road_id, HEAVY_RAIN, Reason("rain", sensor.label))

    for report in reports:
        if report.get("status", "open") != "open" or report["kind"] not in REPORT_KINDS_THAT_AFFECT_ROUTES:
            continue
        for road_id in report.get("roads", ()):
            add(road_id, REPORTED, Reason("report", report["kind"]))

    return {road_id: Risk(min(MAX_RISK, total), tuple(reasons[road_id])) for road_id, total in totals.items()}
