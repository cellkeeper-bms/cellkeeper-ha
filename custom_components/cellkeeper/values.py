"""The pack's values as the database keeps them: fixed SI units (V, A, °C, s), whatever units
Home Assistant shows them in (the desk HA shows °F), and read from the recorder for a past moment.
"""

from __future__ import annotations

from datetime import timedelta
import math
from typing import Any

from homeassistant.components.recorder import history
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfTemperature
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import ElectricCurrentConverter, ElectricPotentialConverter, TemperatureConverter

_SECONDS = {"ms": 0.001, "s": 1, "min": 60, "h": 3600, "d": 86400}


def to_si(state: Any, unit: str | None) -> float | None:
    """A state as a number in SI units, or None when it isn't a number."""
    try:
        v = float(state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if not unit:
        return v
    if unit in TemperatureConverter.VALID_UNITS:
        return TemperatureConverter.convert(v, unit, UnitOfTemperature.CELSIUS)
    if unit in ElectricPotentialConverter.VALID_UNITS:
        return ElectricPotentialConverter.convert(v, unit, UnitOfElectricPotential.VOLT)
    if unit in ElectricCurrentConverter.VALID_UNITS:
        return ElectricCurrentConverter.convert(v, unit, UnitOfElectricCurrent.AMPERE)
    if unit in _SECONDS:
        return v * _SECONDS[unit]
    return v


def unit_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """The unit the entity's states are in now (the recorder keeps no attributes for us)."""
    if (state := hass.states.get(entity_id)) and (unit := state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)):
        return unit
    entry = er.async_get(hass).async_get(entity_id)
    return entry.unit_of_measurement if entry else None


def series(hass: HomeAssistant, start: float, end: float | None, entity_ids: list[str],
           include_start: bool = True) -> dict[str, list[tuple[float, str]]]:
    """Each entity's states from start to end, oldest first; with include_start, the one in force
    at start comes first (at start's time). Runs in the recorder's executor."""
    if not entity_ids:
        return {}
    states = history.get_significant_states(
        hass,
        dt_util.utc_from_timestamp(start),
        None if end is None else dt_util.utc_from_timestamp(end),
        entity_ids,
        include_start_time_state=include_start,
        significant_changes_only=False,
        no_attributes=True,
    )
    out: dict[str, list[tuple[float, str]]] = {}
    for entity_id, entity_states in states.items():
        pts = out.setdefault(entity_id, [])
        for s in entity_states:
            if isinstance(s, State):
                pts.append((s.last_changed.timestamp(), s.state))
            else:
                pts.append((dt_util.parse_datetime(s["last_changed"]).timestamp(), s["state"]))
    return out


def states_at(hass: HomeAssistant, t: float, entity_ids: list[str]) -> dict[str, str]:
    """Each entity's state at time t, from the recorder. Runs in the recorder's executor."""
    out: dict[str, str] = {}
    for entity_id, pts in series(hass, t, t + timedelta(milliseconds=1).total_seconds(), entity_ids).items():
        before = [s for when, s in pts if when <= t + 0.0005]
        if before:
            out[entity_id] = before[-1]
    return out


def cells_summary(cells: dict[int, float | None]) -> dict[str, Any] | None:
    """The lowest and highest cell and the spread between them."""
    known = {n: v for n, v in cells.items() if v is not None and v > 0}
    if not known:
        return None
    lo = min(known, key=known.get)
    hi = max(known, key=known.get)
    return {
        "min": round(known[lo], 4),
        "min_cell": lo,
        "max": round(known[hi], 4),
        "max_cell": hi,
        "spread": round(known[hi] - known[lo], 4),
        "cells": len(known),
    }
