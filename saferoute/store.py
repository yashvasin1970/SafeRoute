"""Live state in SQLite: road closures, shelter status, sensor readings and the activity log.

Every change bumps a version number in the same transaction. Browsers poll that number, so they
fetch the full state only when something actually changed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
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
            conn.execute("DELETE FROM closures")
            conn.execute("DELETE FROM shelters")
            conn.execute("DELETE FROM sensors")
            self._changed(conn, actor, "reset", "Cleared all closures, reopened shelters, reset sensors")

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
