"""RecordTracker and the snapshot builder on their own: states in, records out."""

from __future__ import annotations

import itertools

import pytest

from custom_components.cellkeeper.records import RecordTracker
from custom_components.cellkeeper.snapshot import AFTER_S, BEFORE_S, build


def tracker() -> RecordTracker:
    return RecordTracker("ck0", itertools.count(1).__next__)


def inserts(ops):
    return [op.fields for op in ops if op.kind == "insert"]


def test_contactor_operations():
    tr = tracker()
    assert tr.feed(0, "contactor", "OPEN") == []  # the first state is only a baseline
    assert tr.feed(1, "contactor", "PRECHARGING") == []
    ops = tr.feed(2, "contactor", "CLOSED")
    (row,) = inserts(ops)
    assert (row["kind"], row["key"], row["old"], row["new"]) == ("contactor", "closed", "PRECHARGING", "CLOSED")
    assert ops[0].needs == [("values", "values", 2)]
    (row,) = inserts(tr.feed(3, "contactor", "AWAIT_OPEN"))
    assert row["key"] == "opened"
    assert tr.feed(4, "contactor", "OPEN") == []
    (row,) = inserts(tr.feed(5, "contactor", "PRE_FAIL"))
    assert row["key"] == "failed"


def test_contactor_change_during_a_gap_has_no_known_time():
    tr = tracker()
    tr.feed(0, "contactor", "CLOSED")
    tr.feed(10, "contactor", "unavailable")
    ops = tr.feed(20, "contactor", "OPEN")
    (row,) = inserts(ops)
    assert row["key"] == "opened" and not row["time_known"] and row["after"] == 10
    assert ops[0].needs == []  # the values now aren't the ones it opened at


def test_restarts_from_uptime():
    tr = tracker()
    tr.feed(100, "millis", 5000.0)
    assert tr.feed(101, "millis", 5001.0) == []
    (row,) = inserts(tr.feed(200, "millis", 30.0))
    assert (row["kind"], row["key"]) == ("restart", "bms")
    assert row["t"] == 170 and row["time_known"] and row["data"] == {"uptime_before": 5001}
    # The millis counter wrapping after 49.7 days isn't a restart.
    tr.feed(300, "millis", 2**32 / 1000 - 10)
    assert tr.feed(320, "millis", 10.0) == []
    (row,) = inserts(tr.feed(1000, "uptime_hmi", 0.0) + tr.feed(1100, "uptime_hmi", 90.0) + tr.feed(1200, "uptime_hmi", 5.0))
    assert row["key"] == "hmi" and row["t"] == 1195


def test_states_requests_and_firmware():
    tr = tracker()
    tr.feed(0, "system_state", "INACTIVE")
    (row,) = inserts(tr.feed(1, "system_state", "OPERATING"))
    assert (row["old"], row["new"]) == ("INACTIVE", "OPERATING")
    tr.feed(0, "system_request", "0 NONE")
    (row,) = inserts(tr.feed(2, "system_request", "1 RUN"))
    assert (row["kind"], row["old"], row["new"]) == ("system_request", "NONE", "RUN")
    # The first firmware seen is recorded, with no time of its own.
    (row,) = inserts(tr.feed(0, "bms_firmware", "1.0"))
    assert (row["kind"], row["key"], row["old"], row["new"], row["time_known"]) == ("firmware", "bms", None, "1.0", False)
    (row,) = inserts(tr.feed(5, "bms_firmware", "1.1"))
    assert (row["old"], row["new"], row["time_known"]) == ("1.0", "1.1", True)


def test_settings_and_who():
    tr = tracker()
    tr.feed(0, "setting:max_charge_current", "50.0")
    assert tr.feed(1, "setting:max_charge_current", "50") == []  # the same number
    (row,) = inserts(tr.feed(2, "setting:max_charge_current", "60.0", {"by": "user", "user": "Chester"}))
    assert (row["kind"], row["key"], row["old"], row["new"]) == ("setting", "max_charge_current", "50", "60.0")
    assert row["data"] == {"by": "user", "user": "Chester"}


def test_balance_charge_session():
    tr = tracker()
    tr.feed(0, "balance_charge", "Floating, next today 10:00")
    ops = tr.feed(100, "balance_charge", "Balance charging, 2h00m left")
    (row,) = inserts(ops)
    assert row["kind"] == "balance_charge" and row["time_known"] and ops[0].needs == [("before", "cells", 100)]
    assert tr.feed(160, "balance_charge", "Balance charging, 1h59m left") == []
    assert tr.feed(170, "balance_charge", "Waiting for BMS") == []  # says nothing either way
    ops = tr.feed(7300, "balance_charge", "Floating, next in 7 days 10:00")
    (op,) = ops
    assert op.kind == "update" and op.id == 1 and op.fields == {"end": 7300, "end_after": None}
    assert op.needs == [("after", "cells", 7300), ("balanced", "balanced", (100, 7300))]


def test_snapshot_restores():
    tr = tracker()
    tr.feed(0, "contactor", "OPEN")
    tr.feed(0, "balance_charge", "Balance charging, 1h00m left")
    saved = tr.snapshot()
    again = tracker()
    again.restore(saved)
    assert inserts(again.feed(5, "contactor", "CLOSED"))[0]["old"] == "OPEN"
    assert again.balance_id is not None


def test_build_snapshot():
    t = 1000.0
    start = t - BEFORE_S
    current = [(start, 1.0), (t - 0.5, 120.0), (t - 0.2, 80.0), (t + 3, 0.0)]
    cells = {1: [(start, 3.30), (t, 3.10)], 2: [(start, 3.32)]}
    snap = build(t, {"current": current, "pack_voltage": [(start, 400.0)]}, cells)
    s = snap["series"]
    assert len(s["current"]) == BEFORE_S + AFTER_S + 1
    i = BEFORE_S  # the second the event was raised
    assert s["current"][i] == 80.0 and s["current_max"][i] == 120.0 and s["current_min"][i] == 1.0
    assert s["current"][i + 3] == 0.0
    assert s["pack_voltage"][0] == 400.0
    assert (s["cell_min"][i - 1], s["cell_min_n"][i - 1]) == (3.30, 1)
    assert (s["cell_min"][i], s["cell_min_n"][i], s["cell_max_n"][i]) == (3.10, 1, 2)
    assert snap["start"] == pytest.approx(start)
