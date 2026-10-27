"""The pack's long-term health: each day's summary, full charges, and the day's lowest point.

They're worked out from the recorder's history once a day is over, so a restart in the middle
of a day loses nothing and the same code runs for the days before the integration was installed.

    daily        one per day: Ah and kWh in and out, equivalent full cycles, the SOC and
                 temperature range, contactor operations, events by level, balancing per cell
    full_charge  each time the pack reaches full: every cell, the SOC estimators, temperatures
    empty        each day's lowest SOC: every cell, and which was lowest

"Full" is the pack held at its charge target with the charge current tapered off: at the
inverter's charge voltage limit when the HMI reports it, else with the highest cell at the
Cell Voltage Working Max. It has to hold for FULL_HOLD_S, and counts again only once the SOC
has fallen REARM_SOC below it or REARM_S has passed (a floating pack is full once a day).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FULL_HOLD_S = 300
# Tapered off: less current than this many A per Ah of capacity, and at least TAPER_MIN_A.
TAPER_C = 0.02
TAPER_MIN_A = 1.0
# At the target: the pack within this many volts per cell of the inverter's charge voltage limit...
TARGET_MARGIN_V_PER_CELL = 0.01
# ...or the highest cell within this of the Cell Voltage Working Max.
WORKING_MAX_MARGIN_V = 0.03
REARM_SOC = 5.0
REARM_S = 20 * 3600

# The sensors a day's pass reads, by role (discovery.ROLES).
DAY_ROLES = ("current", "pack_voltage", "soc", "temp_min", "temp_max", "charge_target", "cell_max", "capacity",
             "capacity_est")


@dataclass
class FullDetector:
    """Says when the pack reaches full. Fed every value in time order, across days."""

    held_since: float | None = None
    last_t: float | None = None  # the last full charge
    last_soc: float | None = None
    working_max: float | None = None  # V, from the setting
    cells: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {"held_since": self.held_since, "last_t": self.last_t, "last_soc": self.last_soc}

    def restore(self, s: dict[str, Any]) -> None:
        self.held_since = s.get("held_since")
        self.last_t = s.get("last_t")
        self.last_soc = s.get("last_soc")

    def at_target(self, v: dict[str, float | None]) -> bool:
        current, capacity = v.get("current"), v.get("capacity")
        if current is None:
            return False
        taper = max(TAPER_MIN_A, TAPER_C * capacity) if capacity else TAPER_MIN_A
        if abs(current) > taper:
            return False
        target, pack = v.get("charge_target"), v.get("pack_voltage")
        if target and pack is not None and self.cells:
            return pack >= target - TARGET_MARGIN_V_PER_CELL * self.cells
        cell_max = v.get("cell_max")
        return bool(self.working_max and cell_max is not None and cell_max >= self.working_max - WORKING_MAX_MARGIN_V)

    def _due(self, t: float, soc: float | None) -> float | None:
        """When the hold in progress counts as a full charge, if it does by t: once it has held
        long enough, and the last full charge was long enough ago or the SOC fell since."""
        if self.held_since is None:
            return None
        when = self.held_since + FULL_HOLD_S
        if self.last_t is not None:
            fell = soc is not None and self.last_soc is not None and soc <= self.last_soc - REARM_SOC
            if not fell:
                when = max(when, self.last_t + REARM_S)
        if when > t:
            return None
        self.last_t = when
        self.last_soc = soc
        return when

    def feed(self, t: float, v: dict[str, float | None]) -> float | None:
        """The values as they are from t on. Returns the time of a full charge reached by t."""
        if not self.at_target(v):
            self.held_since = None
            return None
        if self.held_since is None:
            self.held_since = t
        return self._due(t, v.get("soc"))

    def tick(self, t: float, v: dict[str, float | None]) -> float | None:
        """Nothing changed up to t: a hold that started earlier may have completed."""
        return self._due(t, v.get("soc"))


@dataclass
class Day:
    """One day's pass: integrates the current and keeps the ranges. Fed in time order."""

    start: float
    end: float
    detector: FullDetector
    values: dict[str, float | None] = field(default_factory=dict)
    ah_in: float = 0.0
    ah_out: float = 0.0
    kwh_in: float = 0.0
    kwh_out: float = 0.0
    covered_s: float = 0.0  # seconds with a current reading
    soc_min: tuple[float, float] | None = None  # (soc, t)
    soc_max: float | None = None
    temp_min: float | None = None
    temp_max: float | None = None
    fulls: list[float] = field(default_factory=list)
    _t: float | None = None

    def __post_init__(self) -> None:
        # The values carried over from the day before hold from the start.
        if self.values:
            self._t = self.start

    def feed(self, t: float, role: str, value: float | None) -> None:
        t = min(max(t, self.start), self.end)
        self._advance(t)
        self.values[role] = value
        if value is None:
            return
        if role == "soc":
            if self.soc_min is None or value < self.soc_min[0]:
                self.soc_min = (value, t)
            self.soc_max = value if self.soc_max is None else max(self.soc_max, value)
        elif role == "temp_min":
            self.temp_min = value if self.temp_min is None else min(self.temp_min, value)
        elif role == "temp_max":
            self.temp_max = value if self.temp_max is None else max(self.temp_max, value)
        if (full := self.detector.feed(t, self.values)) is not None:
            self.fulls.append(full)

    def finish(self) -> None:
        self._advance(self.end)

    def _advance(self, t: float) -> None:
        """Integrate from the last point to t with the values held. The recorder only keeps
        changes, so a value holds until the next one; unavailable (the HMI away) stops it."""
        last = self._t
        self._t = t
        if last is None or t <= last:
            return
        # (Floating at the target all day counts once every REARM_S.)
        while (full := self.detector.tick(t, self.values)) is not None:
            self.fulls.append(full)
        current = self.values.get("current")
        if current is None:
            return
        dt_h = (t - last) / 3600
        self.covered_s += t - last
        ah = current * dt_h
        volts = self.values.get("pack_voltage")
        kwh = ah * volts / 1000 if volts is not None else 0.0
        if ah >= 0:
            self.ah_in += ah
            self.kwh_in += kwh
        else:
            self.ah_out -= ah
            self.kwh_out -= kwh

    @property
    def empty(self) -> bool:
        """No readings at all (before the pack was installed, or the HMI away all day)."""
        return self.covered_s == 0 and self.soc_min is None

    def summary(self) -> dict[str, Any]:
        capacity = self.values.get("capacity")
        return {
            "ah_in": round(self.ah_in, 3),
            "ah_out": round(self.ah_out, 3),
            "kwh_in": round(self.kwh_in, 4),
            "kwh_out": round(self.kwh_out, 4),
            "capacity": capacity,
            # The BMS's own estimate of the capacity (its EKF), at the end of the day.
            "capacity_est": None if (est := self.values.get("capacity_est")) is None else round(est, 3),
            "cycles": round(self.ah_out / capacity, 4) if capacity else None,
            "covered": round(self.covered_s / (self.end - self.start), 4),
            "soc_min": None if self.soc_min is None else round(self.soc_min[0], 2),
            "soc_max": None if self.soc_max is None else round(self.soc_max, 2),
            "temp_min": self.temp_min,
            "temp_max": self.temp_max,
        }


def balancing(points: dict[int, list[tuple[float, float | None]]]) -> dict[str, int]:
    """Seconds each cell was bled for: the rise of its balancing time counter. A drop is the BMS
    restarting its counter, so counting carries on from the new value."""
    out: dict[str, int] = {}
    for n, pts in points.items():
        total = 0.0
        last = None
        for _t, v in pts:
            if v is None:
                continue
            if last is not None and v > last:
                total += v - last
            last = v
        if total >= 1:
            out[str(n)] = round(total)
    return out


def cells_data(cells: dict[int, float | None]) -> dict[str, Any] | None:
    """Every cell's voltage (index 0 is cell 1), with the lowest, highest and spread."""
    known = {n: v for n, v in cells.items() if v is not None and v > 0}
    if not known:
        return None
    lo = min(known, key=known.get)
    hi = max(known, key=known.get)
    # Up to the last cell with a reading: the HMI has sensors for 108 whatever the pack has.
    count = max(known)
    return {
        "v": [None if (x := cells.get(n)) is None or x <= 0 else round(x, 4) for n in range(1, count + 1)],
        "min": round(known[lo], 4),
        "min_cell": lo,
        "max": round(known[hi], 4),
        "max_cell": hi,
        "spread": round(known[hi] - known[lo], 4),
        "mean": round(sum(known.values()) / len(known), 4),
    }
