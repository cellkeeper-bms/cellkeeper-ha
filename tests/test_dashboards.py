"""A dashboard in the sidebar for each pack, made once."""

from __future__ import annotations

import pytest

from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.cellkeeper.const import DOMAIN
from custom_components.cellkeeper.dashboards import CONF_CREATE_DASHBOARDS, DATA_HANDLED, _collection

EVENTS = ["warning_overcurrent_discharging", "warning_overcurrent_charging", "critical_cell_voltage_high",
          "fatal_pos_stuck_open", "info_boot_normal"]


@pytest.fixture
def env(recorder_mock, enable_custom_integrations: None, hass: HomeAssistant, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)


def add_pack(hass: HomeAssistant, prefix: str, mac: str) -> None:
    """An ESPHome device with the HMI's event sensors."""
    esphome = MockConfigEntry(domain="esphome", title=prefix)
    esphome.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=esphome.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, mac)}, name=prefix
    )
    reg = er.async_get(hass)
    for key in EVENTS:
        name = "Event " + key.replace("_", " ").title()
        reg.async_get_or_create(
            "sensor", "esphome", f"{mac}/0/text_sensor/{name}", suggested_object_id=f"{prefix}_event_{key}",
            original_name=name, device_id=device.id, config_entry=esphome,
        )
        hass.states.async_set(f"sensor.{prefix}_event_{key}", "INACTIVE")


async def setup(hass: HomeAssistant, entry: MockConfigEntry | None = None, **options) -> MockConfigEntry:
    if entry is None:
        entry = MockConfigEntry(domain=DOMAIN, title="CellKeeper", data={}, options=options)
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def dashboards(hass: HomeAssistant) -> dict[str, dict]:
    """url_path -> (item, config) of the storage dashboards."""
    out = {}
    for item in _collection(hass).async_items():
        config = await hass.data[LOVELACE_DATA].dashboards[item["url_path"]].async_load(False)
        out[item["url_path"]] = {**item, "config": config}
    return out


async def test_makes_one_per_pack_once(env, hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    add_pack(hass, "ck0", "02:00:00:00:00:01")
    add_pack(hass, "ck1", "02:00:00:00:00:02")
    await async_wait_recording_done(hass)
    entry = await setup(hass)

    made = await dashboards(hass)
    assert set(made) == {"cellkeeper-ck0", "cellkeeper-ck1"}
    ck0 = made["cellkeeper-ck0"]
    assert ck0["title"] == "CellKeeper ck0" and ck0["show_in_sidebar"] and not ck0["require_admin"]
    assert ck0["icon"] == "cellkeeper:battery"
    assert ck0["config"] == {"strategy": {"type": "custom:cellkeeper", "prefix": "ck0"}}
    assert entry.data[DATA_HANDLED] == ["ck0", "ck1"]

    # Deleted by hand, then a restart: it stays deleted.
    await _collection(hass).async_delete_item(made["cellkeeper-ck1"]["id"])
    assert await hass.config_entries.async_unload(entry.entry_id)
    await setup(hass, entry)
    assert set(await dashboards(hass)) == {"cellkeeper-ck0"}


async def test_not_for_a_pack_that_has_one(env, hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    # Made by hand earlier, with the prefix left to its default (ck0).
    await _collection(hass).async_create_item({"url_path": "dashboard-cellkeeper", "title": "CellKeeper"})
    await hass.data[LOVELACE_DATA].dashboards["dashboard-cellkeeper"].async_save({"strategy": {"type": "custom:cellkeeper"}})
    # A test copy's strategy doesn't count.
    await _collection(hass).async_create_item({"url_path": "ck-dev", "title": "CK Dev"})
    await hass.data[LOVELACE_DATA].dashboards["ck-dev"].async_save({"strategy": {"type": "custom:cellkeeper-dev", "prefix": "ck1"}})
    add_pack(hass, "ck0", "02:00:00:00:00:01")
    add_pack(hass, "ck1", "02:00:00:00:00:02")
    await async_wait_recording_done(hass)
    entry = await setup(hass)

    assert set(await dashboards(hass)) == {"dashboard-cellkeeper", "ck-dev", "cellkeeper-ck1"}
    assert entry.data[DATA_HANDLED] == ["ck0", "ck1"]


async def test_option_off(env, hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    add_pack(hass, "ck0", "02:00:00:00:00:01")
    await async_wait_recording_done(hass)
    entry = await setup(hass, **{CONF_CREATE_DASHBOARDS: False})
    assert await dashboards(hass) == {}
    assert DATA_HANDLED not in entry.data


async def test_options_flow(env, hass: HomeAssistant) -> None:
    entry = await setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_CREATE_DASHBOARDS: False})
    assert result["type"] == "create_entry"
    assert entry.options[CONF_CREATE_DASHBOARDS] is False
