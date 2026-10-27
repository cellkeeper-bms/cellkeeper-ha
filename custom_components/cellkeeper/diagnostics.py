"""Diagnostics download (Settings → Devices & services → CellKeeper → ⋮): what the integration
found and has logged, with each pack's recent rows, for a bug report."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

# Each pack's newest rows of each kind in the download.
RECENT = 50


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    runtime = entry.runtime_data
    store = runtime.store
    packs = {}
    for prefix, pack in runtime.packs.items():
        info = pack.info
        packs[prefix] = {
            "name": info.name,
            "events": len(info.events),
            "roles": info.roles,
            "settings": sorted(info.settings.values()),
            "cells": len(info.cells),
            "balancing": len(info.balancing),
            "trackers": await store.run(hass, store.tracker_states, prefix),
            "occurrences": await store.run(hass, store.occurrences, prefix, None, None, None, None, RECENT),
            "records": await store.run(hass, store.records, prefix, None, None, None, None, RECENT),
        }
    return {"database": await store.run(hass, store.stats), "options": dict(entry.options), "packs": packs}
