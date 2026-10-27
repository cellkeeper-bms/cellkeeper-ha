"""Setting up CellKeeper: one entry logs every pack it finds."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import DOMAIN
from .dashboards import CONF_CREATE_DASHBOARDS
from .discovery import find_packs


class CellKeeperConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CellKeeperOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="CellKeeper", data={})
        packs = find_packs(self.hass)
        listed = ", ".join(f"{p.prefix} ({p.name})" for p in sorted(packs.values(), key=lambda p: p.prefix))
        return self.async_show_form(
            step_id="user",
            description_placeholders={"packs": listed or "none yet; packs are added when their HMI appears"},
        )


class CellKeeperOptionsFlow(OptionsFlow):
    """Whether to make a dashboard for each new pack."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_CREATE_DASHBOARDS, True)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_CREATE_DASHBOARDS, default=current): bool}),
        )
