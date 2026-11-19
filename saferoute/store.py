"""Live state in SQLite: road closures, shelter status, sensor readings and the activity log.

Every change bumps a version number in the same transaction. Browsers poll that number, so they
fetch the full state only when something actually changed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import sensors as sensor_rules

SCHEMA = """
CREATE TABLE IF NOT EXISTS closures (
    road_id     TEXT PRIMARY KEY,
    reason      TEXT NOT NULL,
    source      TEXT NOT NULL,          -- 'responder' or 'sensor:<id>'
    reported_by TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shelters (
    shelter_id TEXT PRIMARY KEY,
    status     TEXT NOT NULL CHECK (status IN ('open', 'full', 'closed')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sensors (
    sensor_id  TEXT PRIMARY KEY,
    level_cm   REAL NOT NULL,
    armed      INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('version', 0);
CREATE TABLE IF NOT EXISTS occupancy (
    shelter_id TEXT PRIMARY KEY,
    people     INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS arrivals (          -- people who said "I'm heading here"
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    shelter_id TEXT NOT NULL,
    people     INTEGER NOT NULL,
    client     TEXT NOT NULL,                  -- salted hash of the sender's address
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (           -- resident reports, by app or SMS
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK (kind IN ('flooding', 'blocked', 'rescue')),
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    road_id    TEXT,                           -- nearest road, closed if a responder verifies
    roads      TEXT NOT NULL DEFAULT '[]',     -- JSON: roads close enough to be risky
    place      TEXT NOT NULL,                  -- street name for messages
    note       TEXT NOT NULL DEFAULT '',       -- shown to responders only
    source     TEXT NOT NULL,                  -- 'app', 'sms' or 'story'
    client     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'verified', 'dismissed')),
    created_at TEXT NOT NULL,
    handled_by TEXT
);
CREATE TABLE IF NOT EXISTS feeds (             -- live feeds: weather, weather drill, flood story
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscribers (       -- people who get SMS alerts for their street
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phone         TEXT NOT NULL UNIQUE,
    street        TEXT NOT NULL,
    lang          TEXT NOT NULL DEFAULT 'en',
    verified      INTEGER NOT NULL DEFAULT 0,
    pending_street TEXT,                       -- requested by web sign-up, applied once verified
    pending_lang  TEXT,
    otp_hash      TEXT,
    otp_expires   TEXT,
    otp_attempts  INTEGER NOT NULL DEFAULT 0,
    otp_sends     TEXT NOT NULL DEFAULT '[]',  -- JSON: when codes were sent (rate limit)
    shelter_id    TEXT,                        -- shelter their route currently leads to
    route_roads   TEXT,                        -- JSON: roads of that route (to spot a blocked route)
    safe_at       TEXT,
    last_alert_at TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (            -- every SMS SafeRoute sends
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phone      TEXT NOT NULL,
    text       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safe_marks (        -- people who said they are safe, once each
    who        TEXT PRIMARY KEY,               -- phone, or a salted hash of a web visitor
    shelter_id TEXT,
    source     TEXT NOT NULL,
    at         TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Closure:
    road_id: str
    reason: str
    source: str
    reported_by: str
    created_at: str

    @property
    def sensor_id(self) -> str | None:
        return self.source.removeprefix("sensor:") if self.source.startswith("sensor:") else None


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit on success, roll back on error
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        # Keep one connection open for the app's lifetime. Otherwise every short-lived connection
        # is the last one to close, and SQLite checkpoints and deletes its write-ahead log each time.
        self._keeper = sqlite3.connect(self.path, check_same_thread=False)

    def close(self) -> None:
        keeper = getattr(self, "_keeper", None)
        if keeper is not None:
            keeper.close()
            self._keeper = None

    @staticmethod
    def _changed(conn: sqlite3.Connection, actor: str, action: str, detail: str) -> None:
        conn.execute("UPDATE meta SET value = value + 1 WHERE key = 'version'")
        if action:
            conn.execute(
                "INSERT INTO events (at, actor, action, detail) VALUES (?, ?, ?, ?)",
                (now_iso(), actor, action, detail),
            )

    # ---- reads -------------------------------------------------------------------------------

    def version(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()[0]

    def closures(self) -> dict[str, Closure]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM closures ORDER BY created_at").fetchall()
        return {r["road_id"]: Closure(**dict(r)) for r in rows}

    def shelter_status(self) -> dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT shelter_id, status FROM shelters").fetchall()
        return {r["shelter_id"]: r["status"] for r in rows}

    def sensor_state(self) -> dict[str, dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM sensors").fetchall()
        return {
            r["sensor_id"]: {"level_cm": r["level_cm"], "armed": bool(r["armed"]), "updated_at": r["updated_at"]}
            for r in rows
        }

    def events(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT at, actor, action, detail FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def all_events(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT id, at, actor, action, detail FROM events ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ---- responder actions -------------------------------------------------------------------

    def close_road(self, road_id: str, reason: str, source: str, actor: str, label: str) -> bool:
        """Close a road. Returns False if it was already closed."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO closures (road_id, reason, source, reported_by, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (road_id, reason, source, actor, now_iso()),
            )
            if cur.rowcount == 0:
                return False
            self._changed(conn, actor, "closed", f"{label}: {reason}")
            return True

    def reopen_road(self, road_id: str, actor: str, label: str) -> Closure | None:
        """Reopen a road. A sensor whose closure a person overrides stays quiet until it dries out."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM closures WHERE road_id = ?", (road_id,)).fetchone()
            if row is None:
                return None
            closure = Closure(**dict(row))
            conn.execute("DELETE FROM closures WHERE road_id = ?", (road_id,))
            if closure.sensor_id:
                conn.execute("UPDATE sensors SET armed = 0 WHERE sensor_id = ?", (closure.sensor_id,))
            self._changed(conn, actor, "reopened", label)
            return closure

    def set_shelter(self, shelter_id: str, status: str, actor: str, label: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT status FROM shelters WHERE shelter_id = ?", (shelter_id,)).fetchone()
            if (row["status"] if row else "open") == status:
                return False
            conn.execute(
                "INSERT INTO shelters (shelter_id, status, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (shelter_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
                (shelter_id, status, now_iso()),
            )
            self._changed(conn, actor, f"shelter {status}", label)
            return True

    def reset(self, actor: str) -> None:
        with self._conn() as conn:
            for table in ("closures", "shelters", "sensors", "occupancy", "arrivals", "reports", "safe_marks"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM feeds WHERE key != 'weather'")
            # Subscribers stay signed up and keep the route they were last told about, so a drill
            # loaded right after a reset still alerts them when it blocks that route.
            conn.execute("UPDATE subscribers SET safe_at = NULL, last_alert_at = NULL")
            self._changed(conn, actor, "reset", "Cleared closures, reports and readings; emptied and reopened shelters")

    # ---- shelter load ------------------------------------------------------------------------

    def occupancy(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT shelter_id, people FROM occupancy").fetchall()
        return {r["shelter_id"]: r["people"] for r in rows}

    def set_occupancy(self, shelter_id: str, people: int, actor: str, label: str, capacity: int) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT people FROM occupancy WHERE shelter_id = ?", (shelter_id,)).fetchone()
            if (row["people"] if row else 0) == people:
                return False
            conn.execute(
                "INSERT INTO occupancy (shelter_id, people, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (shelter_id) DO UPDATE SET people = excluded.people, updated_at = excluded.updated_at",
                (shelter_id, people, now_iso()),
            )
            self._changed(conn, actor, "occupancy", f"{label}: {people} of {capacity} places taken")
            return True

    def add_arrival(self, shelter_id: str, people: int, client: str, minutes: int, max_active: int = 3) -> bool:
        """Count people on their way to a shelter for a while. False if this client has too many."""
        now = datetime.now(UTC)
        with self._conn() as conn:
            conn.execute("DELETE FROM arrivals WHERE expires_at < ?", (now.isoformat(timespec="seconds"),))
            active = conn.execute("SELECT COUNT(*) FROM arrivals WHERE client = ?", (client,)).fetchone()[0]
            if active >= max_active:
                return False
            conn.execute(
                "INSERT INTO arrivals (shelter_id, people, client, expires_at) VALUES (?, ?, ?, ?)",
                (shelter_id, people, client, (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")),
            )
            self._changed(conn, "", "", "")
            return True

    def en_route(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT shelter_id, SUM(people) AS people FROM arrivals WHERE expires_at >= ? GROUP BY shelter_id",
                (now_iso(),),
            ).fetchall()
        return {r["shelter_id"]: r["people"] for r in rows}

    # ---- resident reports --------------------------------------------------------------------

    def add_report(self, report: dict, actor: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO reports (kind, lat, lon, road_id, roads, place, note, source, client, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report["kind"],
                    report["lat"],
                    report["lon"],
                    report.get("road_id"),
                    json.dumps(report.get("roads", [])),
                    report["place"],
                    report.get("note", ""),
                    report["source"],
                    report["client"],
                    now_iso(),
                ),
            )
            what = {"flooding": "flooding", "blocked": "a blocked road", "rescue": "a rescue request"}[report["kind"]]
            self._changed(conn, actor, "report", f"{what} near {report['place']} (#{cur.lastrowid}, unverified)")
            return cur.lastrowid

    def reports(self, status: str | None = "open") -> list[dict]:
        query = "SELECT * FROM reports" + (" WHERE status = ?" if status else "") + " ORDER BY id DESC LIMIT 200"
        with self._conn() as conn:
            rows = conn.execute(query, (status,) if status else ()).fetchall()
        return [{**dict(r), "roads": json.loads(r["roads"])} for r in rows]

    def report(self, report_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return {**dict(row), "roads": json.loads(row["roads"])} if row else None

    def handle_report(self, report_id: int, status: str, actor: str, detail: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE reports SET status = ?, handled_by = ? WHERE id = ? AND status = 'open'",
                (status, actor, report_id),
            )
            if cur.rowcount == 0:
                return False
            self._changed(conn, actor, f"report {status}", detail)
            return True

    # ---- live feeds --------------------------------------------------------------------------

    def feed(self, key: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM feeds WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_feed(self, key: str, value: dict | None, actor: str = "", action: str = "", detail: str = "") -> None:
        """Store (or with None, remove) a feed value. Logs an event when an action is given."""
        with self._conn() as conn:
            if value is None:
                conn.execute("DELETE FROM feeds WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO feeds (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, json.dumps(value), now_iso()),
                )
            self._changed(conn, actor, action, detail)

    # ---- sensors -----------------------------------------------------------------------------

    def record_reading(
        self, sensor_id: str, road_ids: tuple[str, ...], level_cm: float, area_label: str
    ) -> dict[str, list[str]]:
        """Store a reading and apply the sensor rule to every road it watches, in one transaction.

        Returns the road ids it closed and reopened.
        """
        actor = f"Sensor {sensor_id}"
        source = f"sensor:{sensor_id}"
        with self._conn() as conn:
            row = conn.execute("SELECT level_cm, armed FROM sensors WHERE sensor_id = ?", (sensor_id,)).fetchone()
            previous = row["level_cm"] if row else 0.0
            armed = bool(row["armed"]) if row else True
            marks = ",".join("?" * len(road_ids))
            sources = dict(
                conn.execute(f"SELECT road_id, source FROM closures WHERE road_id IN ({marks})", road_ids).fetchall()
            )

            closed, reopened = [], []
            armed_after = sensor_rules.decide(level_cm, armed, None)[1]
            for road_id in road_ids:
                current = sources.get(road_id)
                closure = None if current is None else ("self" if current == source else "other")
                action, armed_after = sensor_rules.decide(level_cm, armed, closure)
                if action == sensor_rules.CLOSE:
                    closed.append(road_id)
                elif action == sensor_rules.REOPEN:
                    reopened.append(road_id)

            reading = f"{level_cm:g} cm"
            conn.execute(
                "INSERT INTO sensors (sensor_id, level_cm, armed, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (sensor_id) DO UPDATE SET level_cm = excluded.level_cm, "
                "armed = excluded.armed, updated_at = excluded.updated_at",
                (sensor_id, level_cm, int(armed_after), now_iso()),
            )
            conn.executemany(
                "INSERT INTO closures (road_id, reason, source, reported_by, created_at) VALUES (?, ?, ?, ?, ?)",
                [(road_id, f"Flooded ({reading})", source, actor, now_iso()) for road_id in closed],
            )
            conn.executemany("DELETE FROM closures WHERE road_id = ?", [(road_id,) for road_id in reopened])

            if closed:
                segments = f"{len(closed)} road segment{'s' if len(closed) != 1 else ''}"
                self._changed(conn, actor, "closed", f"the {area_label} area ({segments}): flooded, {reading}")
            elif reopened:
                self._changed(conn, actor, "reopened", f"the {area_label} area: water down to {reading}")
            elif sensor_rules.band(level_cm) != sensor_rules.band(previous):
                self._changed(conn, actor, "water level", f"{area_label}: {reading} ({sensor_rules.band(level_cm)})")
            else:
                self._changed(conn, actor, "", "")
            return {"closed": closed, "reopened": reopened}

    # ---- SMS subscribers, outbox and safe marks ------------------------------------------------

    def subscriber(self, phone: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM subscribers WHERE phone = ?", (phone,)).fetchone()
        return self._subscriber(row) if row else None

    def subscribers(self, verified_only: bool = True) -> list[dict]:
        query = "SELECT * FROM subscribers" + (" WHERE verified = 1" if verified_only else "") + " ORDER BY id"
        with self._conn() as conn:
            rows = conn.execute(query).fetchall()
        return [self._subscriber(r) for r in rows]

    @staticmethod
    def _subscriber(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["verified"] = bool(data["verified"])
        data["otp_sends"] = json.loads(data["otp_sends"] or "[]")
        data["route_roads"] = json.loads(data["route_roads"]) if data["route_roads"] else None
        return data

    SUBSCRIBER_FIELDS = (
        "street",
        "lang",
        "verified",
        "otp_hash",
        "otp_expires",
        "otp_attempts",
        "otp_sends",
        "pending_street",
        "pending_lang",
        "shelter_id",
        "route_roads",
        "safe_at",
        "last_alert_at",
    )

    def save_subscriber(self, sub: dict, actor: str = "", action: str = "", detail: str = "") -> None:
        """Insert or update a subscriber by phone. Logs an event when an action is given."""
        values = {
            "street": sub["street"],
            "lang": sub.get("lang", "en"),
            "verified": int(bool(sub.get("verified"))),
            "otp_hash": sub.get("otp_hash"),
            "otp_expires": sub.get("otp_expires"),
            "otp_attempts": int(sub.get("otp_attempts", 0)),
            "otp_sends": json.dumps(sub.get("otp_sends", [])),
            "pending_street": sub.get("pending_street"),
            "pending_lang": sub.get("pending_lang"),
            "shelter_id": sub.get("shelter_id"),
            "route_roads": json.dumps(sub["route_roads"]) if sub.get("route_roads") is not None else None,
            "safe_at": sub.get("safe_at"),
            "last_alert_at": sub.get("last_alert_at"),
        }
        fields = self.SUBSCRIBER_FIELDS
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO subscribers (phone, {', '.join(fields)}, created_at) "
                f"VALUES (?, {', '.join('?' * len(fields))}, ?) "
                f"ON CONFLICT (phone) DO UPDATE SET {', '.join(f'{f} = excluded.{f}' for f in fields)}",
                (sub["phone"], *[values[f] for f in fields], now_iso()),
            )
            if action:
                self._changed(conn, actor, action, detail)

    def delete_subscriber(self, phone: str, actor: str, detail: str) -> bool:
        with self._conn() as conn:
            gone = conn.execute("DELETE FROM subscribers WHERE phone = ?", (phone,)).rowcount > 0
            if gone:
                self._changed(conn, actor, "sms stopped", detail)
            return gone

    def queue_sms(self, phone: str, text: str, kind: str, status: str = "queued") -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO outbox (phone, text, kind, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (phone, text, kind, status, now_iso()),
            )
            return cur.lastrowid

    def set_sms_status(self, message_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE outbox SET status = ? WHERE id = ?", (status, message_id))

    def messages_to(self, phone: str, after_id: int = 0) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, text, kind, created_at FROM outbox WHERE phone = ? AND id > ? ORDER BY id LIMIT 50",
                (phone, after_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def outbox_counts(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT kind, COUNT(*) AS n FROM outbox GROUP BY kind").fetchall()
        return {r["kind"]: r["n"] for r in rows}

    def mark_safe(self, who: str, shelter_id: str | None, source: str, label: str) -> bool:
        """Count someone as safe. Returns False if they had already said so (the time is updated)."""
        with self._conn() as conn:
            existed = conn.execute("SELECT 1 FROM safe_marks WHERE who = ?", (who,)).fetchone() is not None
            conn.execute(
                "INSERT INTO safe_marks (who, shelter_id, source, at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (who) DO UPDATE SET shelter_id = excluded.shelter_id, at = excluded.at",
                (who, shelter_id, source, now_iso()),
            )
            conn.execute("UPDATE subscribers SET safe_at = ? WHERE phone = ?", (now_iso(), who))
            self._changed(conn, "Resident" if source == "app" else "SMS", "" if existed else "safe", label)
            return not existed

    def safe_marks(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT shelter_id, source, at FROM safe_marks").fetchall()
        return [dict(r) for r in rows]

    def check_in(self, shelter_id: str, people: int, client: str, label: str, capacity: int) -> int:
        """People arrived at a shelter: raise its headcount, count them safe, clear their trip.

        A shelter whose headcount reaches its capacity counts as full from then on, so routing
        stops sending people there and SMS subscribers heading there are alerted.
        """
        with self._conn() as conn:
            row = conn.execute("SELECT people FROM occupancy WHERE shelter_id = ?", (shelter_id,)).fetchone()
            people_now = min(capacity * 2, (row["people"] if row else 0) + people)
            conn.execute(
                "INSERT INTO occupancy (shelter_id, people, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (shelter_id) DO UPDATE SET people = excluded.people, updated_at = excluded.updated_at",
                (shelter_id, people_now, now_iso()),
            )
            conn.execute("DELETE FROM arrivals WHERE client = ? AND shelter_id = ?", (client, shelter_id))
            conn.execute(
                "INSERT INTO safe_marks (who, shelter_id, source, at) VALUES (?, ?, 'checkin', ?) "
                "ON CONFLICT (who) DO UPDATE SET shelter_id = excluded.shelter_id, at = excluded.at",
                (f"web:{client}", shelter_id, now_iso()),
            )
            self._changed(conn, "Resident", "check-in", f"{people} at {label} ({people_now}/{capacity})")
            return people_now
