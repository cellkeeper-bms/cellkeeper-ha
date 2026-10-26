"""Setting up CellKeeper: one entry logs every pack it finds."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN
from .discovery import find_packs


class CellKeeperConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="CellKeeper", data={})
        packs = find_packs(self.hass)
        listed = ", ".join(f"{p.prefix} ({p.name})" for p in sorted(packs.values(), key=lambda p: p.prefix))
        return self.async_show_form(
            step_id="user",
            description_placeholders={"packs": listed or "none yet; packs are added when their HMI appears"},
        )
