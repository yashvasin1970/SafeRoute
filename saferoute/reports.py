"""Resident reports (crowd data): flooding, blocked roads and rescue requests.

Anyone can report, from the app or by SMS. A report never closes a road by itself, which keeps
the proposal's rule that only authorised responders (and sensors) change the map. Instead:

* flooding and blocked-road reports make nearby roads *risky*, so "safest" routes avoid them
  straight away (see ``risk.py``);
* a responder can **verify** a report, which closes the nearest road, or **dismiss** it;
* rescue requests, with their notes, are shown to responders only.
"""

from __future__ import annotations

import hashlib

from .network import RoadNetwork
from .risk import REPORT_RADIUS_M
from .store import Store

REPORT_KINDS = ("flooding", "blocked", "rescue")
MAX_REPORT_DISTANCE_M = 150


def client_id(secret: str, address: str) -> str:
    """A stable, anonymous id for rate limiting: a salted hash, never the address itself."""
    return hashlib.sha256(f"{secret}|{address}".encode()).hexdigest()[:16]


def place_name(network: RoadNetwork, road_id: str) -> str:
    name = network.roads[road_id].name
    return name if name != "Unnamed road" else "an unnamed lane"


def make_report(
    network: RoadNetwork, kind: str, lat: float, lon: float, note: str, source: str, client: str
) -> dict | None:
    """Build a report snapped to the nearest road, or None if the spot is off the map."""
    nearest = network.nearest_road(lat, lon, MAX_REPORT_DISTANCE_M)
    if nearest is None:
        return None
    road_id = nearest[0]
    clean_note = " ".join("".join(ch for ch in note if ch.isprintable()).split())[:200]
    return {
        "kind": kind,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "road_id": road_id,
        "roads": network.roads_within(lat, lon, REPORT_RADIUS_M) if kind != "rescue" else [],
        "place": place_name(network, road_id),
        "note": clean_note,
        "source": source,
        "client": client,
    }


def verify_report(store: Store, network: RoadNetwork, report: dict, actor: str) -> bool:
    """Confirm a report. Flooding and blocked-road reports close the nearest road."""
    if report["status"] != "open":
        return False
    if report["kind"] in ("flooding", "blocked") and report["road_id"] in network.roads:
        reason = "Flooded" if report["kind"] == "flooding" else "Blocked"
        store.close_road(
            report["road_id"],
            f"{reason} (verified report #{report['id']})",
            "responder",
            actor,
            place_name(network, report["road_id"]),
        )
    what = "rescue request" if report["kind"] == "rescue" else f"{report['kind']} report"
    return store.handle_report(report["id"], "verified", actor, f"{what} #{report['id']} near {report['place']}")


def dismiss_report(store: Store, report: dict, actor: str) -> bool:
    return store.handle_report(report["id"], "dismissed", actor, f"report #{report['id']} near {report['place']}")
