"""RecordTracker and the snapshot builder on their own: states in, records out."""

from __future__ import annotations

import itertools
import json

import pytest

from custom_components.cellkeeper.records import RecordTracker
from custom_components.cellkeeper.snapshot import AFTER_S, BEFORE_S, build
from custom_components.cellkeeper.store import Store


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
    # A later start is a restart once the next reading agrees.
    assert tr.feed(200, "millis", 30.0) == []
    (row,) = inserts(tr.feed(201, "millis", 31.0))
    assert (row["kind"], row["key"]) == ("restart", "bms")
    assert row["t"] == 170 and row["time_known"] and row["data"] == {"uptime_before": 5001}
    # The millis counter wrapping after 49.7 days isn't a restart.
    tr.feed(300 + 2**32 / 1000, "millis", 2**32 / 1000 + 130)
    tr.feed(320 + 2**32 / 1000, "millis", 10.0)
    assert tr.feed(321 + 2**32 / 1000, "millis", 11.0) == []
    rows = inserts(sum((tr.feed(t, "uptime_hmi", up) for t, up in ((1000, 0.0), (1100, 100.0), (1200, 5.0), (1260, 65.0))), []))
    (row,) = rows
    assert row["key"] == "hmi" and row["t"] == 1195 and row["data"] == {"uptime_before": 100}


def test_misread_uptimes_are_not_restarts():
    tr = tracker()
    tr.feed(100, "millis", 5000.0)
    # A reading far too high (more uptime than time has passed) is skipped...
    assert tr.feed(101, "millis", 9.2e15) == []
    # ...and so is one too low that the next reading doesn't agree with.
    assert tr.feed(102, "millis", 300.0) == []
    assert tr.feed(103, "millis", 5003.0) == []
    assert tr.last["millis"] == 5003.0
    # A restart that the next reading confirms, after a misread in between, uses the boot it ended.
    assert tr.feed(200, "millis", 10.0) == []
    assert tr.feed(201, "millis", 1e12) == []
    (row,) = inserts(tr.feed(202, "millis", 12.0))
    assert row["t"] == 190 and row["data"] == {"uptime_before": 5003}
    # The clocks drifting apart over a long gap isn't a misread.
    assert tr.feed(10202, "millis", 10024.0) == []
    assert tr.last["millis"] == 10024.0


def test_restart_waiting_for_its_next_reading_survives_a_restore():
    tr = tracker()
    tr.feed(100, "millis", 5000.0)
    tr.feed(200, "millis", 30.0)
    again = tracker()
    again.restore(tr.snapshot())
    (row,) = inserts(again.feed(260, "millis", 90.0))
    assert row["t"] == 170


def test_uptime_from_before_the_boot_was_kept():
    """A snapshot from 0.17.1 or earlier has only the last uptime."""
    tr = tracker()
    tr.restore({"last": {"millis": 5000.0}, "last_t": {"millis": 100.0}})
    (row,) = inserts(tr.feed(200, "millis", 30.0))
    assert row["t"] == 170
    assert tr.feed(201, "millis", 31.0) == []


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


def test_module_ids():
    tr = tracker()
    # The IDs when logging began are recorded, without a known time.
    (row,) = inserts(tr.feed(0, "module_ids", "023 · pack type 34"))
    assert (row["kind"], row["old"], row["new"], row["time_known"]) == ("modules", None, "023 · pack type 34", False)
    assert tr.feed(10, "module_ids", "023 · pack type 34") == []
    # A module swapped.
    (row,) = inserts(tr.feed(20, "module_ids", "024 · pack type 35"))
    assert (row["old"], row["new"], row["time_known"]) == ("023 · pack type 34", "024 · pack type 35", True)


def test_restarts_from_misread_uptimes_are_removed_once(tmp_path):
    """0.17.1 and earlier made a restart of every garbage uptime reading."""
    path = tmp_path / "cellkeeper.db"
    store = Store(path)
    store.open()
    store._db.execute("DELETE FROM meta")  # as 0.17.1 left it
    rows = [
        (1, "bms", 1000, 500),  # the first one: nothing to fit in
        (2, "bms", 5000, 3990),  # fits in the 4000 s since the boot before started
        (3, "bms", 5000, 9.2e15),  # the same boot again, uptime before far too high
        (4, "bms", 5000, 4100),  # the same start: two boots can't start together
        (5, "bms", 6000, 3000),  # more uptime than the 1000 s since
        (6, "bms", 9000, 3995),  # fits once the ones above are gone
        (7, "hmi", 1000, 4e17),  # a first one that can't be real
        (8, "hmi", 2000, None),
    ]
    for row_id, key, t, before in rows:
        data = json.dumps({"uptime_before": before}) if before is not None else None
        store._db.execute(
            "INSERT INTO records (id, prefix, t, kind, key, time_known, data) VALUES (?, 'ck0', ?, 'restart', ?, 1, ?)",
            (row_id, t, key, data),
        )
    store.close()
    store.open()
    assert [r[0] for r in store._db.execute("SELECT id FROM records ORDER BY id")] == [1, 2, 6, 8]
    # Once only: restarts logged from now on are left alone.
    store._db.execute(
        "INSERT INTO records (id, prefix, t, kind, key, time_known, data) VALUES (9, 'ck0', 9001, 'restart', 'bms', 1, ?)",
        (json.dumps({"uptime_before": 9e15}),),
    )
    store.close()
    store.open()
    assert store._db.execute("SELECT COUNT(*) FROM records WHERE id = 9").fetchone()[0] == 1
    store.close()
