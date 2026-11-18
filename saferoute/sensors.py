"""Water-level sensors that close and reopen roads by themselves.

Each sensor sits at a low point and reports a water depth in centimetres. Water at a low point
floods the stretch around it, so a sensor watches every road segment within its radius (80 m by
default), not just the one it is bolted to. The thresholds follow common flood-safety guidance:
about 15 cm of moving water can knock a person over, and about 30 cm can float a car.

* At 30 cm or more, the sensor closes its roads, except any that are already closed.
* Below 15 cm, the sensor reopens roads, but only the ones it closed itself. It never reopens a
  road that a responder closed.
* The gap between the two levels stops a road flickering open and shut as water hovers at the line.
* A responder can reopen a road a sensor closed (for example, if the sensor is faulty). The sensor
  then stays quiet until the water drops below 15 cm again. People have the final say.
"""

from __future__ import annotations

CAUTION_CM = 15
CLOSE_CM = 30
MAX_LEVEL_CM = 500

CLOSE = "close"
REOPEN = "reopen"


def band(level_cm: float) -> str:
    if level_cm >= CLOSE_CM:
        return "flooded"
    if level_cm >= CAUTION_CM:
        return "caution"
    return "dry"


def decide(level_cm: float, armed: bool, closure: str | None) -> tuple[str | None, bool]:
    """What a new reading should do to one of the sensor's roads.

    ``closure`` is None when the road is open, "self" when this sensor closed it and "other" when a
    responder or another sensor did. Returns the action (CLOSE, REOPEN or None) and whether the
    sensor is armed afterwards.
    """
    if level_cm < CAUTION_CM:
        return (REOPEN if closure == "self" else None), True
    if level_cm >= CLOSE_CM and armed and closure is None:
        return CLOSE, True
    return None, armed
