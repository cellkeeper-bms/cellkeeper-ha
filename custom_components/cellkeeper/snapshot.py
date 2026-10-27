"""An event's snapshot: the pack's key values for the minute before a warning or worse was raised,
and a few seconds after, taken from the recorder once it has them.

After the recorder's 10 days the exact history around an old trip is gone; its snapshot keeps it.
Values are in SI units, one per second; the current also has each second's lowest and highest,
since it updates ten times a second and a trip can be one spike.
"""

from __future__ import annotations

from typing import Any

BEFORE_S = 60
AFTER_S = 10
# How long after the last second of a snapshot to wait for the recorder to have written it.
RECORDER_LAG_S = 10

SERIES = ("current", "pack_voltage", "output_voltage", "soc", "temp_min", "temp_max", "limit_charge", "limit_discharge")


def build(t: float, series: dict[str, list[tuple[float, float | None]]],
          cells: dict[int, list[tuple[float, float | None]]]) -> dict[str, Any]:
    """series: role -> [(time, value)], cells: cell number -> [(time, volts)], oldest first, each
    starting with the value in force at t - BEFORE_S."""
    start = t - BEFORE_S
    steps = [start + i for i in range(BEFORE_S + AFTER_S + 1)]
    out: dict[str, Any] = {"t": t, "start": start, "step": 1, "series": {}}
    for role, pts in series.items():
        out["series"][role] = [_round(v) for v in _sample(pts, steps)]
        if role == "current":
            lo, hi = _extremes(pts, steps)
            out["series"]["current_min"], out["series"]["current_max"] = lo, hi
    if cells:
        sampled = {n: _sample(pts, steps) for n, pts in cells.items()}
        lo_v, hi_v, lo_n, hi_n = [], [], [], []
        for i in range(len(steps)):
            known = {n: s[i] for n, s in sampled.items() if s[i] is not None and s[i] > 0}
            if not known:
                lo_v.append(None), hi_v.append(None), lo_n.append(None), hi_n.append(None)
                continue
            lo, hi = min(known, key=known.get), max(known, key=known.get)
            lo_v.append(_round(known[lo])), hi_v.append(_round(known[hi]))
            lo_n.append(lo), hi_n.append(hi)
        out["series"].update(cell_min=lo_v, cell_max=hi_v, cell_min_n=lo_n, cell_max_n=hi_n)
    return out


def _sample(pts: list[tuple[float, float | None]], steps: list[float]) -> list[float | None]:
    """The value in force at each step."""
    out, i, cur = [], 0, None
    for s in steps:
        while i < len(pts) and pts[i][0] <= s:
            cur = pts[i][1]
            i += 1
        out.append(cur)
    return out


def _extremes(pts: list[tuple[float, float | None]], steps: list[float]) -> tuple[list[Any], list[Any]]:
    """Each second's lowest and highest value: the one in force as it began, and every one in it."""
    los, his = [], []
    i, cur = 0, None
    for s in steps:
        seen = [cur] if cur is not None else []
        while i < len(pts) and pts[i][0] <= s:
            cur = pts[i][1]
            if cur is not None:
                seen.append(cur)
            i += 1
        los.append(_round(min(seen)) if seen else None)
        his.append(_round(max(seen)) if seen else None)
    return los, his


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 4)
