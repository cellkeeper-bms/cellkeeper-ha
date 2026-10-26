"""Repairs: removing the old separate dashboard install."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant

from .frontend import OLD_INSTALL_ISSUE, async_remove_old_install


class OldDashboardInstallFlow(ConfirmRepairFlow):
    """Confirm, then remove the old install's Lovelace resource."""

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> Any:
        if user_input is not None:
            await async_remove_old_install(self.hass)
            return self.async_create_entry(data={})
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    if issue_id == OLD_INSTALL_ISSUE:
        return OldDashboardInstallFlow()
    return ConfirmRepairFlow()
