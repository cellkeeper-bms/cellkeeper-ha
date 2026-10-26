"""EventTracker: event sensor states to occurrences, without Home Assistant."""

from __future__ import annotations

import itertools

from custom_components.cellkeeper.tracker import FOLD_S, EventTracker, parse_state


def make() -> EventTracker:
    counter = itertools.count(1)
    return EventTracker("ck0", "warning_overcurrent_discharging", lambda: next(counter))


def feed(tr: EventTracker, *points: tuple[float, str]) -> list:
    ops = []
    for t, s in points:
        ops += tr.feed(t, s)
    return ops


def test_parse_state() -> None:
    assert parse_state("INACTIVE").count == 0
    assert parse_state("INACTIVE (3)") == parse_state("INACTIVE (3)")
    p = parse_state("WARNING (7)")
    assert p.active and p.level == "WARNING" and p.count == 7
    # The trip data the HMI will add later doesn't get in the way.
    p = parse_state("FATAL (2) −52.4 A · d=FFFF3344")
    assert p.active and p.level == "FATAL" and p.count == 2
    assert parse_state("unavailable") is None
    assert parse_state("unknown") is None
    assert parse_state(None) is None


def test_raise_and_clear() -> None:
    tr = make()
    ops = feed(tr, (100, "INACTIVE"), (200, "WARNING (1)"), (260, "INACTIVE (1)"))
    assert [o.kind for o in ops] == ["insert", "update"]
    row = ops[0].fields
    assert row["kind"] == "raised" and row["level"] == "WARNING" and row["raised_at"] == 200
    assert row["time_known"] and row["n"] == 1
    assert ops[1].id == ops[0].id
    assert ops[1].fields == {"cleared_at": 260, "clear": "cleared"}
    assert tr.open_id is None


def test_repeats_while_active_fold() -> None:
    tr = make()
    ops = feed(tr, (0, "INACTIVE"), (10, "WARNING (1)"), (20, "WARNING (2)"), (30, "WARNING (5)"))
    raised, again, more = ops
    assert raised.fields["kind"] == "raised"
    assert again.kind == "insert" and again.fields["kind"] == "again" and again.fields["n"] == 1
    assert more.kind == "update" and more.id == again.id and more.fields["n"] == 4 and more.fields["count_to"] == 5
    # Long after, a repeat starts a new row.
    ops = feed(tr, (30 + FOLD_S + 1, "WARNING (6)"))
    assert ops[0].kind == "insert" and ops[0].id != again.id


def test_between_readings() -> None:
    tr = make()
    ops = feed(tr, (0, "INACTIVE (1)"), (50, "INACTIVE (3)"))
    assert len(ops) == 1
    assert ops[0].fields["kind"] == "between" and ops[0].fields["n"] == 2
    assert ops[0].fields["count_from"] == 1 and ops[0].fields["count_to"] == 3


def test_escalation() -> None:
    tr = make()
    ops = feed(tr, (0, "INACTIVE"), (10, "WARNING (1)"), (20, "FATAL (1)"))
    assert ops[1].kind == "update" and ops[1].fields == {"level": "FATAL", "escalated_at": 20}


def test_bms_restart() -> None:
    tr = make()
    ops = feed(tr, (0, "INACTIVE"), (10, "WARNING (3)"), (20, "INACTIVE"))
    assert ops[-1].kind == "update" and ops[-1].fields["clear"] == "bms_restart"
    # Counts start again after a restart: a lower count is a new occurrence.
    tr = make()
    ops = feed(tr, (0, "INACTIVE (5)"), (10, "WARNING (1)"))
    assert len(ops) == 1 and ops[0].fields["kind"] == "raised" and ops[0].fields["count_from"] == 0
    tr = make()
    assert feed(tr, (0, "INACTIVE (5)"), (10, "INACTIVE")) == []


def test_unavailable_gap_marks_time_unknown() -> None:
    tr = make()
    ops = feed(tr, (0, "INACTIVE (1)"), (100, "unavailable"), (500, "WARNING (2)"))
    row = ops[0].fields
    assert row["raised_at"] == 500 and row["raised_after"] == 100 and not row["time_known"]
    # A clear seen only after a gap is bounded the same way.
    ops = feed(tr, (600, "unavailable"), (900, "INACTIVE (2)"))
    assert ops[0].fields == {"cleared_at": 900, "clear": "cleared", "cleared_after": 600}
    # The same state again after a gap is nothing.
    assert feed(tr, (1000, "unavailable"), (1100, "INACTIVE (2)")) == []


def test_already_active_when_first_seen() -> None:
    tr = make()
    ops = feed(tr, (0, "CRITICAL (2)"))
    assert ops[0].fields["kind"] == "active" and not ops[0].fields["time_known"]
    assert tr.open_id == ops[0].id


def test_old_and_repeated_states_are_ignored() -> None:
    tr = make()
    feed(tr, (100, "INACTIVE"), (200, "WARNING (1)"))
    assert feed(tr, (150, "INACTIVE (9)")) == []
    assert feed(tr, (200, "WARNING (1)")) == []
    assert feed(tr, (300, "WARNING (1)")) == []


def test_snapshot_restore() -> None:
    tr = make()
    feed(tr, (0, "INACTIVE"), (10, "WARNING (1)"), (20, "WARNING (2)"))
    tr2 = make()
    tr2.restore(tr.snapshot())
    assert tr2.prev == tr.prev and tr2.open_id == tr.open_id and tr2.fold_id == tr.fold_id
    ops = tr2.feed(30, "INACTIVE (2)")
    assert ops[0].kind == "update" and ops[0].id == tr.open_id
