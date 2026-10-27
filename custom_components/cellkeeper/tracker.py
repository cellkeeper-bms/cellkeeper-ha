"""Turning an event sensor's states into occurrences.

The HMI shows each BMS event as a text sensor (sensor.<prefix>_event_<label>_<name>) whose state is
"INACTIVE", "INACTIVE (n)" (happened n times since the BMS started, not active now) or
"<LEVEL> (n)" (active now). The count only resets when the BMS restarts.

EventTracker follows one sensor and says what changed as plain operations, so the same code runs
on the recorder's history (the first 10 days, and what happened while Home Assistant was down)
and on live state changes. It doesn't touch the database or Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

LEVELS = ("INFO", "WARNING", "CRITICAL", "FATAL")

# Repeats of one event within this long of the last one fold into one row ("happened 12 more
# times"): an event that's counted on every failed read can't flood the log.
FOLD_S = 600

_ACTIVE = re.compile(r"^(INFO|WARNING|CRITICAL|FATAL) \((\d+)\)")
_INACTIVE = re.compile(r"^INACTIVE \((\d+)\)")
# The latest occurrence's data, as hex, when the HMI sends it: "WARNING (7) d=FA0FFFF2D4C"
_DATA = re.compile(r" d=([0-9A-Fa-f]{1,16})(?![0-9A-Za-z])")


@dataclass(frozen=True)
class Parsed:
    """One event sensor state."""

    active: bool
    count: int
    level: str | None = None
    # The data of the latest occurrence (hex), from HMI firmware that sends it. It only changes
    # with the count, so it isn't part of what makes a state new.
    data: str | None = field(default=None, compare=False)


def parse_state(state: str | None) -> Parsed | None:
    """Parse an event sensor's state, or None when it isn't one (unavailable, unknown).

    After "(n)" the HMI may add the latest occurrence's data, " d=<hex>"; anything else after it
    is ignored.
    """
    if not state:
        return None
    d = _DATA.search(state)
    data = d.group(1).upper() if d else None
    if m := _ACTIVE.match(state):
        return Parsed(True, int(m.group(2)), m.group(1), data)
    if m := _INACTIVE.match(state):
        return Parsed(False, int(m.group(1)), None, data)
    if state == "INACTIVE":
        return Parsed(False, 0)
    return None


def rank(level: str | None) -> int:
    """A level's rank, INFO lowest; -1 for none."""
    return LEVELS.index(level) if level in LEVELS else -1


@dataclass
class Op:
    """One change to the log: insert a row, update one, or add a marker."""

    kind: str  # "insert", "update"
    id: int
    fields: dict[str, Any]


@dataclass
class EventTracker:
    """One event sensor: its last known state and the rows it's still adding to."""

    prefix: str
    key: str  # the sensor's id after "_event_", e.g. "warning_overcurrent_discharging"
    next_id: Any  # callable returning a new row id
    prev: Parsed | None = None
    prev_t: float | None = None
    gap_since: float | None = None  # unavailable since (the HMI or HA was away)
    open_id: int | None = None  # the occurrence that's active now
    fold_id: int | None = None  # the last repeat row, while repeats still fold into it
    fold_last: float | None = None
    fold_n: int = 0
    ops: list[Op] = field(default_factory=list)

    # ---- saving and restoring (the baselines table) ----

    def snapshot(self) -> dict[str, Any]:
        """What has to survive a restart."""
        p = self.prev
        return {
            "active": None if p is None else p.active,
            "count": None if p is None else p.count,
            "level": None if p is None else p.level,
            "t": self.prev_t,
            "gap_since": self.gap_since,
            "open_id": self.open_id,
            "fold_id": self.fold_id,
            "fold_last": self.fold_last,
            "fold_n": self.fold_n,
        }

    def restore(self, s: dict[str, Any]) -> None:
        """Pick up from a snapshot."""
        if s.get("active") is not None:
            self.prev = Parsed(bool(s["active"]), int(s["count"]), s.get("level"))
        self.prev_t = s.get("t")
        self.gap_since = s.get("gap_since")
        self.open_id = s.get("open_id")
        self.fold_id = s.get("fold_id")
        self.fold_last = s.get("fold_last")
        self.fold_n = s.get("fold_n") or 0

    # ---- feeding states ----

    def feed(self, t: float, state: str | None) -> list[Op]:
        """Take the sensor's state at time t (seconds since the epoch) and return the changes.

        States must arrive in time order; one before the last one is ignored, and the same state
        again is nothing, so history that overlaps what was already seen is harmless.
        """
        self.ops = []
        if self.prev_t is not None and t < self.prev_t:
            return []
        if self.gap_since is not None and t < self.gap_since:
            return []
        cur = parse_state(state)
        if cur is None:
            # Unavailable: the HMI went offline or HA restarted. What happens until it comes back
            # is only known to have happened after this.
            if self.prev is not None and self.gap_since is None:
                self.gap_since = t
            return []

        prev = self.prev
        if prev is None:
            # The first state seen. If it's active, the event started before logging did.
            if cur.active:
                self._raise(t, cur, 0, kind="active", after=None, known=False)
            self._take(t, cur)
            return self.ops

        if cur == prev and self.gap_since is None:
            self._take(t, cur)
            return self.ops

        # Something happened while the sensor was unavailable: it's only known to be after this.
        after = self.gap_since
        known = after is None

        restarted = cur.count < prev.count or (prev.active and not cur.active and cur.count == 0)
        if restarted:
            # The BMS restarted and its counts began again.
            if self.open_id is not None:
                self._update(self.open_id, cleared_at=t, clear="bms_restart")
                self.open_id = None
            self._end_fold()
            if cur.active:
                self._raise(t, cur, 0, kind="raised", after=after, known=known)
            elif cur.count:
                self._between(t, cur.level, 0, cur.count, after, known, cur.data)
        elif cur.active and not prev.active:
            self._raise(t, cur, prev.count, kind="raised", after=after, known=known)
        elif cur.active and prev.active:
            if rank(cur.level) > rank(prev.level):
                if self.open_id is None:
                    self._raise(t, cur, prev.count, kind="active", after=after, known=False)
                else:
                    self._update(self.open_id, level=cur.level, escalated_at=t)
            if cur.count > prev.count:
                self._repeat(t, cur.level, prev.count, cur.count, "again", after, known, cur.data)
        elif prev.active and not cur.active:
            if self.open_id is not None:
                fields: dict[str, Any] = {"cleared_at": t, "clear": "cleared"}
                if not known:
                    fields["cleared_after"] = after
                self._update(self.open_id, **fields)
                self.open_id = None
            if cur.count > prev.count:
                self._between(t, prev.level, prev.count, cur.count, after, known, cur.data)
        elif cur.count > prev.count:
            # Raised and cleared again between two readings.
            self._between(t, None, prev.count, cur.count, after, known, cur.data)

        self._take(t, cur)
        return self.ops

    # ---- helpers ----

    def _take(self, t: float, cur: Parsed) -> None:
        self.prev = cur
        self.prev_t = t
        self.gap_since = None

    def _raise(self, t: float, cur: Parsed, count_from: int, *, kind: str, after: float | None, known: bool) -> None:
        self._end_fold()
        row_id = self.next_id()
        self.ops.append(
            Op(
                "insert",
                row_id,
                {
                    "prefix": self.prefix,
                    "event": self.key,
                    "kind": kind,
                    "level": cur.level,
                    "start_level": cur.level,
                    "raised_at": t,
                    "raised_after": after,
                    "time_known": known,
                    "count_from": count_from,
                    "count_to": cur.count,
                    "n": max(cur.count - count_from, 1),
                    "last_at": t,
                    "data": cur.data,
                },
            )
        )
        self.open_id = row_id

    def _between(self, t: float, level: str | None, count_from: int, count_to: int, after: float | None, known: bool,
                 data: str | None = None) -> None:
        self._repeat(t, level, count_from, count_to, "between", after, known, data)

    def _repeat(self, t: float, level: str | None, count_from: int, count_to: int, kind: str, after: float | None, known: bool,
                data: str | None = None) -> None:
        n = count_to - count_from
        if self.fold_id is not None and self.fold_last is not None and known and t - self.fold_last <= FOLD_S:
            self.fold_n += n
            self.fold_last = t
            # A folded row keeps the latest occurrence's data.
            extra = {"data": data} if data else {}
            self._update(self.fold_id, n=self.fold_n, count_to=count_to, last_at=t, **extra)
            return
        row_id = self.next_id()
        self.ops.append(
            Op(
                "insert",
                row_id,
                {
                    "prefix": self.prefix,
                    "event": self.key,
                    "kind": kind,
                    "level": level,
                    "start_level": level,
                    "raised_at": t,
                    "raised_after": after,
                    "time_known": known,
                    "count_from": count_from,
                    "count_to": count_to,
                    "n": n,
                    "last_at": t,
                    "data": data,
                },
            )
        )
        self.fold_id, self.fold_last, self.fold_n = row_id, t, n

    def _end_fold(self) -> None:
        self.fold_id, self.fold_last, self.fold_n = None, None, 0

    def _update(self, row_id: int, **fields: Any) -> None:
        self.ops.append(Op("update", row_id, fields))
