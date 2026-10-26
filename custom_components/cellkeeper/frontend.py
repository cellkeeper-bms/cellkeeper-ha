"""The dashboard: the integration serves it and loads it on every page.

frontend/ (the dashboard's source) builds custom_components/cellkeeper/frontend/cellkeeper-dashboard.js.
It defines the custom:cellkeeper dashboard strategy and its cards, so a dashboard with
`strategy: {type: custom:cellkeeper, prefix: ck0}` works without a Lovelace resource.

The dashboard used to be installed on its own (HACS, CellKeeper-Dashboard). Two copies would define
the same cards, so if that one is still a Lovelace resource, a repair offers to remove it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.components.frontend import add_extra_js_url, remove_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_BASE = "/cellkeeper_frontend"
JS_FILE = "cellkeeper-dashboard.js"
JS_PATH = Path(__file__).parent / "frontend" / JS_FILE

# Registered once per Home Assistant run: static paths can't be removed again.
_SERVED = f"{DOMAIN}_frontend_served"

OLD_INSTALL_ISSUE = "old_dashboard_install"


def _is_old_install(url: str) -> bool:
    """A Lovelace resource of the separate dashboard install (not the dev build)."""
    u = url.split("?", 1)[0].lower()
    return u.endswith("/cellkeeper-dashboard.js") and not u.startswith(URL_BASE)


async def async_setup_frontend(hass: HomeAssistant, version: str) -> str:
    """Serve the dashboard and load it on every page; returns its URL."""
    url = f"{URL_BASE}/{JS_FILE}?v={version}"
    if not hass.data.get(_SERVED):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(f"{URL_BASE}/{JS_FILE}", str(JS_PATH), True)]
        )
        hass.data[_SERVED] = True
    add_extra_js_url(hass, url)
    return url


def async_remove_frontend(hass: HomeAssistant, url: str) -> None:
    try:
        remove_extra_js_url(hass, url)
    except KeyError:
        pass


def _resources(hass: HomeAssistant) -> Any:
    """Lovelace's resource collection, or None (Lovelace not loaded, or a future change)."""
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA  # noqa: PLC0415

        return hass.data[LOVELACE_DATA].resources
    except (ImportError, KeyError, AttributeError):
        return None


async def async_old_installs(hass: HomeAssistant) -> list[dict[str, Any]]:
    """The old dashboard install's Lovelace resources."""
    resources = _resources(hass)
    if resources is None:
        return []
    try:
        await resources.async_get_info()  # loads the collection if it isn't yet
        return [r for r in resources.async_items() if _is_old_install(r.get("url", ""))]
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Couldn't read the Lovelace resources", exc_info=True)
        return []


async def async_check_old_install(hass: HomeAssistant) -> None:
    """Raise or clear the repair for the old separate dashboard install."""
    old = await async_old_installs(hass)
    if old:
        ir.async_create_issue(
            hass,
            DOMAIN,
            OLD_INSTALL_ISSUE,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=OLD_INSTALL_ISSUE,
            translation_placeholders={"url": old[0]["url"].split("?", 1)[0]},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, OLD_INSTALL_ISSUE)


async def async_remove_old_install(hass: HomeAssistant) -> int:
    """Remove the old install's Lovelace resources; returns how many."""
    resources = _resources(hass)
    old = await async_old_installs(hass)
    for r in old:
        await resources.async_delete_item(r["id"])
    ir.async_delete_issue(hass, DOMAIN, OLD_INSTALL_ISSUE)
    return len(old)
