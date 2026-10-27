"""Turning the pack's state sensors into records: contactor operations, system state and request
changes, BMS and HMI restarts, firmware and settings changes, and balance charges.

Like tracker.py, RecordTracker only says what changed, so the same code runs on the recorder's
history and on live states. Values from the pack's other sensors at the moment of a record
(current, voltages, cells) are asked for in RecordOp.needs, and the pack logger fills them in.

Sources (the pack logger maps entities to these):
    contactor, system_state, system_request, bms_firmware, hmi_firmware, balance_charge (text)
    millis, uptime_hmi (seconds, already converted)
    setting:<key> (a number entity's state)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

UNAVAILABLE = ("unavailable", "unknown", "")

CLOSED = "CLOSED"
FAILED = ("PRE_FAIL", "TST_FAIL")

# An uptime that drops by more than this is a restart (less is a reading's jitter).
RESTART_DROP_S = 5
# The BMS's millis is 32 bits: it wraps after 49.7 days, which isn't a restart.
MILLIS_WRAP_S = 2**32 / 1000

BALANCING = "Balance charging"
FLOATING = ("Floating", "Off")


@dataclass
class RecordOp:
    """Insert a record or update one. needs: (data key, what, when) to fill into data first."""

    kind: str  # "insert", "update"
    id: int
    fields: dict[str, Any]
    needs: list[tuple[str, str, Any]] = field(default_factory=list)


class RecordTracker:
    """One pack's state sensors: the last value of each, and the balance charge in progress."""

    def __init__(self, prefix: str, next_id: Any) -> None:
        self.prefix = prefix
        self.next_id = next_id
        self.last: dict[str, Any] = {}  # source -> its last value
        self.last_t: dict[str, float] = {}
        self.gap: dict[str, float] = {}  # source -> unavailable since
        self.balance_id: int | None = None  # the balance charge in progress
        self.balance_start: float | None = None
        self.ops: list[RecordOp] = []

    # ---- saving and restoring ----

    def snapshot(self) -> dict[str, Any]:
        return {
            "last": self.last,
            "last_t": self.last_t,
            "gap": self.gap,
            "balance_id": self.balance_id,
            "balance_start": self.balance_start,
        }

    def restore(self, s: dict[str, Any]) -> None:
        self.last = dict(s.get("last") or {})
        self.last_t = dict(s.get("last_t") or {})
        self.gap = dict(s.get("gap") or {})
        self.balance_id = s.get("balance_id")
        self.balance_start = s.get("balance_start")

    def mark_gap(self, t: float) -> None:
        """Home Assistant was down after t: every change seen next happened at some time after it."""
        for source in self.last:
            self.gap.setdefault(source, t)

    def knows(self, source: str) -> bool:
        return source in self.last

    # ---- feeding states ----

    def feed(self, t: float, source: str, state: Any, by: dict[str, Any] | None = None) -> list[RecordOp]:
        """Take a source's state at time t and return the records it makes. by: who changed a
        setting, when it's known (live only)."""
        self.ops = []
        if t < self.last_t.get(source, -math.inf):
            return []
        if state is None or (isinstance(state, str) and state in UNAVAILABLE):
            if source in self.last:
                self.gap.setdefault(source, t)
            self.last_t[source] = t
            return []

        prev = self.last.get(source)
        after = self.gap.pop(source, None)
        if source.startswith("setting:"):
            self._setting(t, source[len("setting:"):], prev, state, after, by)
        elif source in ("millis", "uptime_hmi"):
            self._uptime(t, "bms" if source == "millis" else "hmi", prev, state)
        elif source == "contactor":
            self._contactor(t, prev, state, after)
        elif source in ("system_state", "system_request"):
            if source == "system_request":
                state = _request(state)
            if prev is not None and prev != state:
                self._insert(t, source, None, prev, state, after)
        elif source in ("bms_firmware", "hmi_firmware"):
            if prev != state:
                # The first one seen is recorded too: the firmware the pack had when logging began.
                self._insert(t, "firmware", source.split("_")[0], prev, state, after if prev is not None else None,
                             known=prev is not None and after is None)
        elif source == "balance_charge":
            state = self._balance(t, prev, state, after)
            if state is None:
                return self.ops
        self.last[source] = state
        self.last_t[source] = t
        return self.ops

    # ---- sources ----

    def _contactor(self, t: float, prev: str | None, state: str, after: float | None) -> None:
        if prev is None or prev == state:
            return
        needs = [("values", "values", t)] if after is None else []
        if state == CLOSED:
            self._insert(t, "contactor", "closed", prev, state, after, needs=needs)
        elif prev == CLOSED:
            # The current as the contacts open is what wears them.
            self._insert(t, "contactor", "opened", prev, state, after, needs=needs)
        elif state in FAILED:
            self._insert(t, "contactor", "failed", prev, state, after, needs=needs)

    def _uptime(self, t: float, key: str, prev: float | None, up: Any) -> None:
        up = float(up)
        if prev is None or up >= prev - RESTART_DROP_S:
            return
        if key == "bms" and prev > MILLIS_WRAP_S - 3600 and up < 3600:
            return
        # It started `up` seconds before this reading: the time is exact even after a gap.
        self._insert(t - up, "restart", key, None, None, None, data={"uptime_before": round(prev)})

    def _setting(self, t: float, key: str, prev: Any, state: Any, after: float | None, by: dict[str, Any] | None) -> None:
        new, old = _number(state), _number(prev)
        if new is None or old is None or new == old:
            return
        data = dict(by) if by and after is None else {}
        self._insert(t, "setting", key, prev, state, after, data=data)

    def _balance(self, t: float, prev: str | None, state: str, after: float | None) -> str | None:
        """A balance charge runs while the status says "Balance charging, ...". Other statuses
        ("Waiting for BMS") say nothing either way and are skipped."""
        charging = state.startswith(BALANCING)
        if not charging and not state.startswith(FLOATING):
            return None
        state = "charging" if charging else "floating"
        if charging and prev != "charging":
            known = prev is not None and after is None
            row_id = self._insert(t, "balance_charge", None, None, None, after if prev is not None else None,
                                  known=known, needs=[("before", "cells", t)] if known else [])
            self.balance_id, self.balance_start = row_id, t if known else None
        elif not charging and prev == "charging" and self.balance_id is not None:
            fields: dict[str, Any] = {"end": t, "end_after": after}
            needs: list[tuple[str, str, Any]] = []
            if after is None:
                needs.append(("after", "cells", t))
                if self.balance_start is not None:
                    needs.append(("balanced", "balanced", (self.balance_start, t)))
            self.ops.append(RecordOp("update", self.balance_id, fields, needs))
            self.balance_id = self.balance_start = None
        return state

    # ---- helpers ----

    def _insert(self, t: float, kind: str, key: str | None, old: Any, new: Any, after: float | None, *,
                known: bool | None = None, data: dict[str, Any] | None = None,
                needs: list[tuple[str, str, Any]] | None = None) -> int:
        row_id = self.next_id()
        self.ops.append(
            RecordOp(
                "insert",
                row_id,
                {
                    "prefix": self.prefix,
                    "t": t,
                    "kind": kind,
                    "key": key,
                    "old": None if old is None else str(old),
                    "new": None if new is None else str(new),
                    "time_known": after is None if known is None else known,
                    "after": after,
                    "data": data or {},
                },
                needs or [],
            )
        )
        return row_id


def _request(state: str) -> str:
    """ "1 RUN" -> "RUN" (the HMI shows the request's number first)."""
    head, _, word = state.partition(" ")
    return word if head.lstrip("-").isdigit() and word else state


def _number(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None
