"""Backups: the database is complete in its main file, and not written to, while one is taken."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_pre_backup(hass: HomeAssistant) -> None:
    if (runtime := hass.data.get(DOMAIN)) and not runtime.closed:
        await runtime.async_pause()


async def async_post_backup(hass: HomeAssistant) -> None:
    if (runtime := hass.data.get(DOMAIN)) and not runtime.closed:
        await runtime.async_resume()
