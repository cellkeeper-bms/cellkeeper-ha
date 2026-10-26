"""The integration's own database: SQLite at /config/cellkeeper/cellkeeper.db.

It's separate from the recorder's database, so the recorder's purge and settings never touch it.
Every call runs on one worker thread (Store.run), so SQLite is only ever used from that thread.
Times are seconds since the epoch (UTC).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Each migration takes the database from version i to i + 1.
MIGRATIONS: list[str] = [
    # 0 -> 1
    """
    CREATE TABLE packs (
        prefix TEXT PRIMARY KEY,      -- ck0: the entity ids' prefix, the pack's name in the dashboard
        name TEXT,
        device_id TEXT,
        mac TEXT,
        first_seen REAL,
        synced_until REAL             -- every state up to here has been through the trackers
    );
    CREATE TABLE occurrences (
        id INTEGER PRIMARY KEY,
        prefix TEXT NOT NULL,
        event TEXT NOT NULL,          -- warning_overcurrent_discharging (the sensor id after _event_)
        kind TEXT NOT NULL,           -- raised, again (repeated while active), between (raised and
                                      -- cleared between readings), active (already active when seen)
        level TEXT,                   -- the level it reached
        start_level TEXT,
        raised_at REAL NOT NULL,      -- when it was seen
        raised_after REAL,            -- when time_known is 0: it happened between this and raised_at
        time_known INTEGER NOT NULL,
        escalated_at REAL,
        cleared_at REAL,
        cleared_after REAL,
        clear TEXT,                   -- cleared, bms_restart
        count_from INTEGER,           -- the sensor's count before and after
        count_to INTEGER,
        n INTEGER NOT NULL,           -- occurrences in this row (repeats fold together)
        last_at REAL,
        data TEXT,                    -- the trip data, once the HMI sends it (raw hex)
        text TEXT                     -- and decoded
    );
    CREATE INDEX occurrences_pack_time ON occurrences (prefix, raised_at);
    CREATE INDEX occurrences_pack_event ON occurrences (prefix, event, raised_at);
    CREATE TABLE markers (
        id INTEGER PRIMARY KEY,
        prefix TEXT NOT NULL,
        t REAL NOT NULL,
        kind TEXT NOT NULL,           -- hmi_offline, hmi_online, logging_started
        text TEXT
    );
    CREATE INDEX markers_pack_time ON markers (prefix, t);
    CREATE TABLE trackers (
        prefix TEXT NOT NULL,
        event TEXT NOT NULL,
        state TEXT NOT NULL,          -- EventTracker.snapshot() as JSON
        PRIMARY KEY (prefix, event)
    );
    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """,
]

OCCURRENCE_FIELDS = (
    "id", "prefix", "event", "kind", "level", "start_level", "raised_at", "raised_after", "time_known",
    "escalated_at", "cleared_at", "cleared_after", "clear", "count_from", "count_to", "n", "last_at",
    "data", "text",
)


class Store:
    """The database, used only from its own thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cellkeeper_db")
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ---- threading ----

    async def run(self, hass: Any, fn: Any, *args: Any) -> Any:
        """Run fn(*args) on the database thread."""
        return await hass.loop.run_in_executor(self._executor, fn, *args)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)

    @property
    def closed(self) -> bool:
        return self._db is None

    # ---- opening ----

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        self._db = db
        self._migrate()

    def close(self) -> None:
        if self._db is not None:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.close()
            self._db = None

    def _migrate(self) -> None:
        db = self._db
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{self.path} is schema {version}, newer than this version knows ({SCHEMA_VERSION})")
        while version < SCHEMA_VERSION:
            _LOGGER.info("Migrating %s from schema %s", self.path, version)
            # One transaction per step: the schema and its version change together.
            db.executescript(f"BEGIN; {MIGRATIONS[version]} PRAGMA user_version={version + 1}; COMMIT;")
            version += 1

    def checkpoint(self) -> None:
        """Write the WAL into the main file, so a copy of the file alone is complete."""
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ---- packs and trackers ----

    def max_id(self) -> int:
        return self._db.execute("SELECT COALESCE(MAX(id), 0) FROM occurrences").fetchone()[0]

    def pack(self, prefix: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM packs WHERE prefix = ?", (prefix,)).fetchone()
        return dict(row) if row else None

    def packs(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            """SELECT p.*, (SELECT COUNT(*) FROM occurrences o WHERE o.prefix = p.prefix) AS occurrences,
                      (SELECT MIN(raised_at) FROM occurrences o WHERE o.prefix = p.prefix) AS oldest
               FROM packs p ORDER BY p.prefix"""
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_pack(self, prefix: str, name: str | None, device_id: str | None, mac: str | None, now: float) -> None:
        self._db.execute(
            """INSERT INTO packs (prefix, name, device_id, mac, first_seen) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (prefix) DO UPDATE SET name = excluded.name, device_id = excluded.device_id,
                   mac = COALESCE(excluded.mac, packs.mac)""",
            (prefix, name, device_id, mac, now),
        )

    def tracker_states(self, prefix: str) -> dict[str, dict[str, Any]]:
        rows = self._db.execute("SELECT event, state FROM trackers WHERE prefix = ?", (prefix,)).fetchall()
        return {r["event"]: json.loads(r["state"]) for r in rows}

    # ---- writing ----

    def apply(self, prefix: str, ops: list[Any], markers: list[tuple[float, str, str | None]],
              trackers: dict[str, dict[str, Any]], synced_until: float | None) -> None:
        """Write a batch of tracker operations, markers and tracker states in one transaction."""
        db = self._db
        with self._lock:
            db.execute("BEGIN")
            try:
                for op in ops:
                    if op.kind == "insert":
                        cols = ["id", *op.fields]
                        db.execute(
                            f"INSERT INTO occurrences ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            (op.id, *(_sql(v) for v in op.fields.values())),
                        )
                    else:
                        sets = ", ".join(f"{k} = ?" for k in op.fields)
                        db.execute(f"UPDATE occurrences SET {sets} WHERE id = ?", (*(_sql(v) for v in op.fields.values()), op.id))
                for t, kind, text in markers:
                    db.execute("INSERT INTO markers (prefix, t, kind, text) VALUES (?, ?, ?, ?)", (prefix, t, kind, text))
                for event, state in trackers.items():
                    db.execute(
                        "INSERT OR REPLACE INTO trackers (prefix, event, state) VALUES (?, ?, ?)",
                        (prefix, event, json.dumps(state)),
                    )
                if synced_until is not None:
                    db.execute(
                        "UPDATE packs SET synced_until = MAX(COALESCE(synced_until, 0), ?) WHERE prefix = ?",
                        (synced_until, prefix),
                    )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    # ---- reading ----

    def occurrences(self, prefix: str, start: float | None, end: float | None, event: str | None,
                    before_id: int | None, limit: int) -> list[dict[str, Any]]:
        where, args = ["prefix = ?"], [prefix]
        if start is not None:
            # Rows still active, or that ended after start, belong to the range too.
            where.append("(raised_at >= ? OR cleared_at >= ? OR (cleared_at IS NULL AND kind IN ('raised', 'active')))")
            args += [start, start]
        if end is not None:
            where.append("raised_at <= ?")
            args.append(end)
        if event:
            where.append("event = ?")
            args.append(event)
        if before_id is not None:
            where.append("id < ?")
            args.append(before_id)
        rows = self._db.execute(
            f"SELECT * FROM occurrences WHERE {' AND '.join(where)} ORDER BY raised_at DESC, id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
        return [_row(r) for r in rows]

    def occurrence(self, row_id: int) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM occurrences WHERE id = ?", (row_id,)).fetchone()
        return _row(row) if row else None

    def markers(self, prefix: str, start: float | None, end: float | None, limit: int) -> list[dict[str, Any]]:
        where, args = ["prefix = ?"], [prefix]
        if start is not None:
            where.append("t >= ?")
            args.append(start)
        if end is not None:
            where.append("t <= ?")
            args.append(end)
        rows = self._db.execute(
            f"SELECT * FROM markers WHERE {' AND '.join(where)} ORDER BY t DESC LIMIT ?", (*args, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        size = self.path.stat().st_size if self.path.exists() else 0
        wal = self.path.with_name(self.path.name + "-wal")
        return {
            "path": str(self.path),
            "bytes": size + (wal.stat().st_size if wal.exists() else 0),
            "occurrences": self._db.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0],
            "markers": self._db.execute("SELECT COUNT(*) FROM markers").fetchone()[0],
            "schema": SCHEMA_VERSION,
        }


def _sql(v: Any) -> Any:
    return int(v) if isinstance(v, bool) else v


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["time_known"] = bool(d["time_known"])
    return d
