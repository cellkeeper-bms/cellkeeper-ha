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

from .records import RESTART_DROP_S, UPTIME_DRIFT

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# The first restart's uptime before has nothing to fit in: more than this is a misread.
MISREAD_UPTIME_S = 10 * 365 * 86400

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
        kind TEXT NOT NULL,           -- hmi_offline, hmi_online, ha_started, logging_started
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
    # 1 -> 2
    """
    CREATE TABLE records (
        id INTEGER PRIMARY KEY,
        prefix TEXT NOT NULL,
        t REAL NOT NULL,              -- when it happened (a restart: when it started)
        kind TEXT NOT NULL,           -- contactor, system_state, system_request, restart, firmware,
                                      -- setting, balance_charge, modules; daily, full_charge, empty
        key TEXT,                     -- contactor: closed, opened, failed; restart and firmware: bms, hmi;
                                      -- setting: the setting (max_charge_current)
        old TEXT,
        new TEXT,
        time_known INTEGER NOT NULL,
        after REAL,                   -- when time_known is 0: it happened between this and t
        end REAL,                     -- a balance charge's end
        end_after REAL,
        data TEXT                     -- JSON: the values at the time, who changed a setting...
    );
    CREATE INDEX records_pack_time ON records (prefix, t);
    CREATE INDEX records_pack_kind ON records (prefix, kind, t);
    CREATE TABLE snapshots (
        occurrence_id INTEGER PRIMARY KEY,
        prefix TEXT NOT NULL,
        t REAL NOT NULL,
        data TEXT NOT NULL            -- JSON: snapshot.build()
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
        self._repair()

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

    def _repair(self) -> None:
        """One-off fixes to rows earlier versions got wrong, each run once (listed in meta)."""
        db = self._db
        done = {r[0] for r in db.execute("SELECT key FROM meta WHERE key LIKE 'repair:%'")}
        for name, fn in (("repair:misread_restarts", self._drop_misread_restarts),):
            if name in done:
                continue
            db.execute("BEGIN")
            n = fn()
            db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (name, str(n)))
            db.execute("COMMIT")
            if n:
                _LOGGER.info("Removed %s restart records made from misread uptimes", n)

    def _drop_misread_restarts(self) -> int:
        """Up to 0.17.1 a single garbage uptime reading made a restart. A real one's uptime before
        fits in the time since the boot before it started, and no two boots start together."""
        db = self._db
        rows = db.execute("SELECT id, prefix, key, t, data FROM records WHERE kind = 'restart' ORDER BY prefix, key, t")
        bad: list[int] = []
        last: dict[tuple[str, str], float] = {}
        for row_id, prefix, key, t, data in sorted(
            rows, key=lambda r: (r[1], r[2], r[3], (json.loads(r[4]) if r[4] else {}).get("uptime_before", 0))
        ):
            before = (json.loads(data) if data else {}).get("uptime_before")
            prev = last.get((prefix, key))
            if before is not None and prev is not None:
                since = t - prev
                if since < RESTART_DROP_S or before > since + RESTART_DROP_S + since * UPTIME_DRIFT:
                    bad.append(row_id)
                    continue
            elif before is not None and before > MISREAD_UPTIME_S:
                bad.append(row_id)
                continue
            last[(prefix, key)] = t
        db.executemany("DELETE FROM records WHERE id = ?", [(i,) for i in bad])
        return len(bad)

    def checkpoint(self) -> None:
        """Write the WAL into the main file, so a copy of the file alone is complete."""
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ---- packs and trackers ----

    def max_id(self) -> int:
        return self._db.execute("SELECT COALESCE(MAX(id), 0) FROM occurrences").fetchone()[0]

    def max_record_id(self) -> int:
        return self._db.execute("SELECT COALESCE(MAX(id), 0) FROM records").fetchone()[0]

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
              trackers: dict[str, dict[str, Any]], synced_until: float | None,
              records: list[Any] = (), snapshots: list[tuple[int, float, dict[str, Any]]] = ()) -> None:
        """Write a batch of tracker operations, markers, records, snapshots and tracker states in
        one transaction."""
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
                for op in records:
                    fields = {k: (json.dumps(v) if k == "data" else _sql(v)) for k, v in op.fields.items()}
                    if op.kind == "insert":
                        cols = ["id", *fields]
                        db.execute(
                            f"INSERT INTO records ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            (op.id, *fields.values()),
                        )
                    else:
                        # data is merged into what's there (a balance charge's start values stay).
                        sets = ", ".join(
                            "data = json_patch(COALESCE(data, '{}'), ?)" if k == "data" else f"{k} = ?" for k in fields
                        )
                        db.execute(f"UPDATE records SET {sets} WHERE id = ?", (*fields.values(), op.id))
                for occurrence_id, t, data in snapshots:
                    db.execute(
                        "INSERT OR REPLACE INTO snapshots (occurrence_id, prefix, t, data) VALUES (?, ?, ?, ?)",
                        (occurrence_id, prefix, t, json.dumps(data, separators=(",", ":"))),
                    )
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
            f"""SELECT o.*, EXISTS (SELECT 1 FROM snapshots s WHERE s.occurrence_id = o.id) AS snapshot
                FROM occurrences o WHERE {' AND '.join(where)} ORDER BY raised_at DESC, id DESC LIMIT ?""",
            (*args, limit),
        ).fetchall()
        return [_row(r) for r in rows]

    def occurrence(self, row_id: int) -> dict[str, Any] | None:
        row = self._db.execute(
            """SELECT o.*, EXISTS (SELECT 1 FROM snapshots s WHERE s.occurrence_id = o.id) AS snapshot
               FROM occurrences o WHERE id = ?""",
            (row_id,),
        ).fetchone()
        return _row(row) if row else None

    def records(self, prefix: str, start: float | None, end: float | None, kinds: list[str] | None,
                before_id: int | None, limit: int) -> list[dict[str, Any]]:
        where, args = ["prefix = ?"], [prefix]
        if start is not None:
            # A balance charge that ended after start, or is still going, belongs to the range too.
            where.append("(t >= ? OR end >= ? OR (kind = 'balance_charge' AND end IS NULL))")
            args += [start, start]
        if end is not None:
            where.append("t <= ?")
            args.append(end)
        if kinds:
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            args += kinds
        if before_id is not None:
            where.append("id < ?")
            args.append(before_id)
        rows = self._db.execute(
            f"SELECT * FROM records WHERE {' AND '.join(where)} ORDER BY t DESC, id DESC LIMIT ?", (*args, limit)
        ).fetchall()
        return [_record(r) for r in rows]

    def record(self, row_id: int) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM records WHERE id = ?", (row_id,)).fetchone()
        return _record(row) if row else None

    def without_snapshot(self, prefix: str, since: float) -> list[tuple[int, float]]:
        """Warnings or worse raised at a known time since `since` that have no snapshot yet."""
        rows = self._db.execute(
            """SELECT id, raised_at FROM occurrences o
               WHERE prefix = ? AND raised_at >= ? AND kind = 'raised' AND time_known
                 AND start_level IN ('WARNING', 'CRITICAL', 'FATAL')
                 AND NOT EXISTS (SELECT 1 FROM snapshots s WHERE s.occurrence_id = o.id)""",
            (prefix, since),
        ).fetchall()
        return [(r["id"], r["raised_at"]) for r in rows]

    def day_counts(self, prefix: str, start: float, end: float, load_a: float) -> dict[str, Any]:
        """A day's contactor operations (and openings at more than load_a) and events raised, by
        the level they were raised at."""
        contactor = {
            r["key"]: r["n"]
            for r in self._db.execute(
                """SELECT key, COUNT(*) AS n FROM records
                   WHERE prefix = ? AND kind = 'contactor' AND t >= ? AND t < ? GROUP BY key""",
                (prefix, start, end),
            )
        }
        contactor["under_load"] = self._db.execute(
            """SELECT COUNT(*) FROM records WHERE prefix = ? AND kind = 'contactor' AND key = 'opened'
                 AND t >= ? AND t < ? AND ABS(COALESCE(json_extract(data, '$.values.current'), 0)) > ?""",
            (prefix, start, end, load_a),
        ).fetchone()[0]
        # Each raise counts once: an event counted on every call (BMB read errors) folds
        # thousands of repeats into one row.
        events = {
            r["start_level"]: r["n"]
            for r in self._db.execute(
                """SELECT start_level, COUNT(*) AS n FROM occurrences
                   WHERE prefix = ? AND raised_at >= ? AND raised_at < ? AND kind IN ('raised', 'between')
                   GROUP BY start_level""",
                (prefix, start, end),
            )
            if r["start_level"]
        }
        return {"contactor": contactor, "events": events}

    def delete_health(self, prefix: str) -> None:
        """Forget a pack's daily, full charge and empty records, to work them out again."""
        with self._lock:
            self._db.execute("BEGIN")
            self._db.execute("DELETE FROM records WHERE prefix = ? AND kind IN ('daily', 'full_charge', 'empty')", (prefix,))
            self._db.execute("DELETE FROM trackers WHERE prefix = ? AND event = '_health'", (prefix,))
            self._db.execute("COMMIT")

    def health_totals(self, prefix: str, month_start: float, load_a: float) -> dict[str, Any]:
        """What the pack's own sensors show: totals over everything logged."""
        db = self._db
        ops = db.execute(
            "SELECT COUNT(*) FROM records WHERE prefix = ? AND kind = 'contactor' AND key IN ('closed', 'opened')",
            (prefix,),
        ).fetchone()[0]
        under_load = db.execute(
            """SELECT COUNT(*) FROM records WHERE prefix = ? AND kind = 'contactor' AND key = 'opened'
                 AND ABS(COALESCE(json_extract(data, '$.values.current'), 0)) > ?""",
            (prefix, load_a),
        ).fetchone()[0]
        cycles = db.execute(
            "SELECT SUM(json_extract(data, '$.cycles')) FROM records WHERE prefix = ? AND kind = 'daily'", (prefix,)
        ).fetchone()[0]
        full = db.execute(
            """SELECT t, json_extract(data, '$.cells.spread') AS spread FROM records
               WHERE prefix = ? AND kind = 'full_charge' ORDER BY t DESC LIMIT 1""",
            (prefix,),
        ).fetchone()
        warnings = db.execute(
            """SELECT COUNT(*) FROM occurrences WHERE prefix = ? AND raised_at >= ?
                 AND kind IN ('raised', 'between') AND start_level IN ('WARNING', 'CRITICAL', 'FATAL')""",
            (prefix, month_start),
        ).fetchone()[0]
        return {
            "contactor_operations": ops,
            "contactor_openings_under_load": under_load,
            "cycles": round(cycles, 3) if cycles is not None else 0.0,
            "full_spread": full["spread"] if full else None,
            "full_t": full["t"] if full else None,
            "warnings_month": warnings,
        }

    def snapshot(self, occurrence_id: int) -> dict[str, Any] | None:
        row = self._db.execute("SELECT data FROM snapshots WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
        return json.loads(row["data"]) if row else None

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
            "records": self._db.execute("SELECT COUNT(*) FROM records").fetchone()[0],
            "snapshots": self._db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
            "schema": SCHEMA_VERSION,
        }


def _sql(v: Any) -> Any:
    return int(v) if isinstance(v, bool) else v


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["time_known"] = bool(d["time_known"])
    if "snapshot" in d:
        d["snapshot"] = bool(d["snapshot"])
    return d


def _record(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["time_known"] = bool(d["time_known"])
    d["data"] = json.loads(d["data"]) if d["data"] else {}
    return d
