"""A dashboard in the sidebar for each pack, made once.

When the integration finds a pack that no dashboard shows yet (strategy custom:cellkeeper with its
prefix), it makes one: "CellKeeper ck0" at /cellkeeper-ck0, like one made by hand under Settings →
Dashboards. Each pack is handled once, whether a dashboard was made or one was already there, and
remembered in the config entry: a dashboard you rename, change or delete stays that way.

Home Assistant has no API for an integration to make a dashboard, so this uses the same dashboards
collection as Settings → Dashboards, reached through its websocket command. If a future version
moves it, nothing is made and the log says so; the dashboard can still be made by hand.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .discovery import PackInfo

_LOGGER = logging.getLogger(__name__)

STRATEGY = "custom:cellkeeper"
DEFAULT_PREFIX = "ck0"  # what the strategy uses without a prefix
ICON = "cellkeeper:battery"  # from the dashboard's icon set, which the integration loads

CONF_CREATE_DASHBOARDS = "create_dashboards"
DATA_HANDLED = "dashboards_handled"  # in the config entry: packs already given (or found with) one


def _lovelace(hass: HomeAssistant) -> Any:
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA  # noqa: PLC0415

        return hass.data[LOVELACE_DATA]
    except (ImportError, KeyError):
        return None


def _collection(hass: HomeAssistant) -> Any:
    """The storage dashboards collection Settings → Dashboards uses, or None."""
    try:
        handler = hass.data[websocket_api.DOMAIN]["lovelace/dashboards/create"][0]
        collection = inspect.unwrap(handler).__self__.storage_collection
    except (KeyError, AttributeError, TypeError, IndexError):
        return None
    return collection if hasattr(collection, "async_create_item") else None


async def _shown_prefixes(hass: HomeAssistant) -> set[str]:
    """The packs some dashboard already shows with the CellKeeper strategy."""
    shown: set[str] = set()
    lovelace = _lovelace(hass)
    if lovelace is None:
        return shown
    for cfg in list(lovelace.dashboards.values()):
        try:
            config = await cfg.async_load(False)
        except Exception:  # noqa: BLE001 - empty or broken dashboards show nothing
            continue
        strategy = config.get("strategy") if isinstance(config, dict) else None
        if isinstance(strategy, dict) and strategy.get("type") == STRATEGY:
            shown.add(str(strategy.get("prefix") or DEFAULT_PREFIX))
    return shown


async def async_create_dashboards(hass: HomeAssistant, entry: ConfigEntry, packs: list[PackInfo]) -> list[str]:
    """Make a dashboard for each of `packs` that isn't handled yet; returns the url paths made."""
    if not entry.options.get(CONF_CREATE_DASHBOARDS, True):
        return []
    handled = set(entry.data.get(DATA_HANDLED, []))
    todo = [p for p in packs if p.prefix not in handled]
    if not todo:
        return []
    lovelace, collection = _lovelace(hass), _collection(hass)
    if lovelace is None or collection is None:
        _LOGGER.warning("Couldn't make the packs' dashboards (Home Assistant's dashboards aren't where they used to be)")
        return []

    shown = await _shown_prefixes(hass)
    taken = {item.get("url_path") for item in collection.async_items()} | set(lovelace.dashboards)
    made: list[str] = []
    for pack in todo:
        if pack.prefix not in shown:
            url_path = f"cellkeeper-{pack.prefix}".replace("_", "-")
            n = 2
            while url_path in taken:
                url_path, n = f"cellkeeper-{pack.prefix}-{n}".replace("_", "-"), n + 1
            title = f"CellKeeper {pack.name}" if pack.name else f"CellKeeper {pack.prefix}"
            try:
                await collection.async_create_item(
                    {"url_path": url_path, "title": title, "icon": ICON, "show_in_sidebar": True, "require_admin": False}
                )
                await lovelace.dashboards[url_path].async_save({"strategy": {"type": STRATEGY, "prefix": pack.prefix}})
            except Exception:
                _LOGGER.exception("Couldn't make the dashboard for %s", pack.prefix)
                continue
            taken.add(url_path)
            made.append(url_path)
            _LOGGER.info("Made the dashboard %s for %s", url_path, pack.prefix)
        handled.add(pack.prefix)
    hass.config_entries.async_update_entry(entry, data={**entry.data, DATA_HANDLED: sorted(handled)})
    return made
