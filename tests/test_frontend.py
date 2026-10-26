"""The dashboard the integration serves, and the repair for the old separate install."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cellkeeper.const import DOMAIN
from custom_components.cellkeeper.frontend import JS_PATH, OLD_INSTALL_ISSUE, URL_BASE
from custom_components.cellkeeper.repairs import OldDashboardInstallFlow, async_create_fix_flow

MANIFEST = json.loads((Path(__file__).parent.parent / "custom_components" / "cellkeeper" / "manifest.json").read_text())
OLD_URL = "/hacsfiles/CellKeeper-Dashboard/cellkeeper-dashboard.js?hacstag=1428075328123"


@pytest.fixture
def env(recorder_mock, enable_custom_integrations: None, hass: HomeAssistant, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)


async def setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="CellKeeper", data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def test_bundle_matches_the_manifest() -> None:
    """The committed build is there and is this version's (frontend/ builds it)."""
    head = JS_PATH.read_text(encoding="utf-8")[:200]
    assert f"CellKeeper Dashboard {MANIFEST['version']} " in head


async def test_dashboard_is_served_on_every_page(env, hass: HomeAssistant, hass_client) -> None:
    entry = await setup(hass)
    url = f"{URL_BASE}/cellkeeper-dashboard.js?v={MANIFEST['version']}"
    assert url in hass.data[DATA_EXTRA_MODULE_URL].urls
    client = await hass_client()
    resp = await client.get(url)
    assert resp.status == 200
    assert "ll-strategy-dashboard-cellkeeper" in await resp.text()
    # Unloaded: not on new pages any more.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert url not in hass.data[DATA_EXTRA_MODULE_URL].urls


async def test_old_install_repair(env, hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    resources = hass.data[LOVELACE_DATA].resources
    await resources.async_get_info()
    await resources.async_create_item({"res_type": "module", "url": OLD_URL})
    await resources.async_create_item({"res_type": "module", "url": "/local/cellkeeper-dashboard-dev/cellkeeper-dashboard-dev.js?v=t1"})
    await resources.async_create_item({"res_type": "module", "url": "/hacsfiles/mushroom/mushroom.js"})

    await setup(hass)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, OLD_INSTALL_ISSUE)
    assert issue is not None and issue.is_fixable
    assert issue.translation_placeholders["url"] == OLD_URL.split("?")[0]

    flow = await async_create_fix_flow(hass, OLD_INSTALL_ISSUE, None)
    assert isinstance(flow, OldDashboardInstallFlow)
    flow.hass, flow.handler, flow.issue_id = hass, DOMAIN, OLD_INSTALL_ISSUE
    assert (await flow.async_step_init())["type"] == "form"
    assert (await flow.async_step_confirm({}))["type"] == "create_entry"

    # Only the old install's resource went; the dev build and other cards stay.
    urls = [r["url"] for r in resources.async_items()]
    assert OLD_URL not in urls and len(urls) == 2
    assert ir.async_get(hass).async_get_issue(DOMAIN, OLD_INSTALL_ISSUE) is None


async def test_no_repair_without_old_install(env, hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    await setup(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, OLD_INSTALL_ISSUE) is None
