"""The dashboard: the integration serves it and loads it on every page.

frontend/ (the dashboard's source) builds custom_components/cellkeeper/frontend/cellkeeper-dashboard.js.
It defines the custom:cellkeeper dashboard strategy and its cards, so a dashboard with
`strategy: {type: custom:cellkeeper, prefix: ck0}` works without a Lovelace resource.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url, remove_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_BASE = "/cellkeeper_frontend"
JS_FILE = "cellkeeper-dashboard.js"
JS_PATH = Path(__file__).parent / "frontend" / JS_FILE

# Registered once per Home Assistant run: static paths can't be removed again.
_SERVED = f"{DOMAIN}_frontend_served"


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
