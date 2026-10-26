"""The integration in a test Home Assistant: discovery, the recorder catch-up, live logging, the
websocket API and backups."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.cellkeeper.backup import async_post_backup, async_pre_backup
from custom_components.cellkeeper.const import DOMAIN

MAC = "02:00:00:00:00:01"
EVENTS = {
    "warning_overcurrent_discharging": "Event Warning Overcurrent Discharging",
    "warning_overcurrent_charging": "Event Warning Overcurrent Charging",
    "critical_cell_voltage_high": "Event Critical Cell Voltage High",
    "fatal_pos_stuck_open": "Event Fatal POS Stuck Open",
    "info_boot_normal": "Event Info Boot Normal",
    "info_inverter_detected": "Event Info Inverter Detected",
}


def ent(key: str) -> str:
    return f"sensor.ck0_event_{key}"


@pytest.fixture
def env(recorder_mock, enable_custom_integrations: None) -> None:
    """The recorder (it has to start before Home Assistant does) and custom_components/."""


@pytest.fixture
def pack(hass: HomeAssistant, tmp_path) -> None:
    """An ESPHome device with the HMI's event sensors, like a real ck0. Each test gets its own
    config directory, so its own CellKeeper database."""
    hass.config.config_dir = str(tmp_path)
    esphome = MockConfigEntry(domain="esphome", title="ck0")
    esphome.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=esphome.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, MAC)}, name="ck0"
    )
    reg = er.async_get(hass)
    for key, name in EVENTS.items():
        reg.async_get_or_create(
            "sensor", "esphome", f"{MAC}/0/text_sensor/{name}",
            suggested_object_id=f"ck0_event_{key}", original_name=name,
            device_id=device.id, config_entry=esphome,
        )
    # Something that isn't an event sensor, on the same device.
    reg.async_get_or_create(
        "sensor", "esphome", f"{MAC}/0/sensor/Current", suggested_object_id="ck0_current",
        original_name="Current", device_id=device.id, config_entry=esphome,
    )


def set_all(hass: HomeAssistant, state: str = "INACTIVE") -> None:
    for key in EVENTS:
        hass.states.async_set(ent(key), state)


async def setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="CellKeeper", data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def ws(hass_ws_client, msg: dict) -> dict:
    return await ask(await hass_ws_client(), msg)


async def ask(client, msg: dict) -> dict:
    await client.send_json_auto_id(msg)
    reply = await client.receive_json()
    assert reply["success"], reply
    return reply["result"]


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_catch_up_live_and_websocket(
    env, hass: HomeAssistant, pack, hass_ws_client, freezer: FrozenDateTimeFactory
) -> None:
    # History from before the integration was installed: an overcurrent raised and cleared.
    set_all(hass)
    await async_wait_recording_done(hass)
    freezer.tick(timedelta(minutes=5))
    raised_at = dt_util.utcnow().timestamp()
    hass.states.async_set(ent("warning_overcurrent_discharging"), "WARNING (1)")
    await async_wait_recording_done(hass)
    freezer.tick(timedelta(minutes=4))
    hass.states.async_set(ent("warning_overcurrent_discharging"), "INACTIVE (1)")
    await async_wait_recording_done(hass)

    entry = await setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    packs = (await ws(hass_ws_client, {"type": "cellkeeper/packs"}))["packs"]
    assert [p["prefix"] for p in packs] == ["ck0"]
    assert packs[0]["mac"] == MAC and packs[0]["live"]

    result = await ws(hass_ws_client, {"type": "cellkeeper/events/list", "prefix": "ck0"})
    rows = result["occurrences"]
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "warning_overcurrent_discharging" and row["kind"] == "raised"
    assert row["raised_at"] == pytest.approx(raised_at, abs=1)
    assert row["cleared_at"] - row["raised_at"] == pytest.approx(240, abs=1)
    assert row["time_known"]
    assert [m["kind"] for m in result["markers"]] == ["ha_started", "logging_started"]

    # Live, with a subscriber.
    client = await hass_ws_client()
    await client.send_json_auto_id({"type": "cellkeeper/events/subscribe", "prefix": "ck0"})
    assert (await client.receive_json())["success"]
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(ent("critical_cell_voltage_high"), "CRITICAL (1)")
    await settle(hass)
    event = (await client.receive_json())["event"]
    assert event["occurrences"][0]["event"] == "critical_cell_voltage_high"
    assert event["occurrences"][0]["cleared_at"] is None

    rows = (await ws(hass_ws_client, {"type": "cellkeeper/events/list", "prefix": "ck0"}))["occurrences"]
    assert [r["event"] for r in rows] == ["critical_cell_voltage_high", "warning_overcurrent_discharging"]

    info = await ws(hass_ws_client, {"type": "cellkeeper/info"})
    assert info["occurrences"] == 2 and info["bytes"] > 0


async def test_restart_catches_up_without_duplicates(
    env, hass: HomeAssistant, pack, hass_ws_client, freezer: FrozenDateTimeFactory
) -> None:
    set_all(hass)
    await async_wait_recording_done(hass)
    entry = await setup(hass)

    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(ent("warning_overcurrent_charging"), "WARNING (1)")
    await settle(hass)

    # Unloaded (like an HA restart): what changes meanwhile is in the recorder.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(ent("warning_overcurrent_charging"), "INACTIVE (1)")
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(ent("info_inverter_detected"), "INFO (1)")
    await async_wait_recording_done(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)

    rows = (await ws(hass_ws_client, {"type": "cellkeeper/events/list", "prefix": "ck0"}))["occurrences"]
    assert [r["event"] for r in rows] == ["info_inverter_detected", "warning_overcurrent_charging"]
    assert rows[1]["clear"] == "cleared" and rows[1]["cleared_at"] is not None


async def test_downtime_marks_time_unknown(
    env, hass: HomeAssistant, pack, hass_ws_client, freezer: FrozenDateTimeFactory
) -> None:
    set_all(hass)
    await async_wait_recording_done(hass)
    entry = await setup(hass)
    # Logged in before the clock jumps an hour (the test login expires sooner).
    client = await hass_ws_client()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)
    synced = dt_util.utcnow().timestamp()

    # Home Assistant is down for an hour; when it starts, the pack's states have moved on.
    freezer.tick(timedelta(hours=1))
    get_instance(hass).recorder_runs_manager._recording_start = dt_util.utcnow()
    freezer.tick(timedelta(seconds=5))
    hass.states.async_set(ent("info_inverter_detected"), "INFO (1)")
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(ent("warning_overcurrent_charging"), "WARNING (1)")
    await async_wait_recording_done(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    rows = (await ask(client, {"type": "cellkeeper/events/list", "prefix": "ck0"}))["occurrences"]
    by_event = {r["event"]: r for r in rows}
    # The first change of each sensor after the start happened at some point while HA was down...
    inverter = by_event["info_inverter_detected"]
    assert not inverter["time_known"] and inverter["raised_after"] == pytest.approx(synced, abs=1)
    assert not by_event["warning_overcurrent_charging"]["time_known"]
    # ...and later ones are exact.
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(ent("info_inverter_detected"), "INACTIVE (1)")
    await settle(hass)
    rows = (await ask(client, {"type": "cellkeeper/events/list", "prefix": "ck0"}))["occurrences"]
    cleared = next(r for r in rows if r["event"] == "info_inverter_detected")
    assert cleared["clear"] == "cleared" and cleared["cleared_after"] is None


async def test_hmi_offline_markers(
    env, hass: HomeAssistant, pack, hass_ws_client, freezer: FrozenDateTimeFactory
) -> None:
    set_all(hass)
    await async_wait_recording_done(hass)
    await setup(hass)

    freezer.tick(timedelta(minutes=1))
    set_all(hass, "unavailable")
    await settle(hass)
    freezer.tick(timedelta(minutes=10))
    # Back, with an event that happened while it was away.
    for key in EVENTS:
        hass.states.async_set(ent(key), "WARNING (1)" if key == "warning_overcurrent_charging" else "INACTIVE")
    await settle(hass)

    result = await ws(hass_ws_client, {"type": "cellkeeper/events/list", "prefix": "ck0"})
    kinds = [m["kind"] for m in result["markers"]]
    # The test's Home Assistant started just before: an outage that long is still the HMI's.
    assert kinds == ["hmi_online", "hmi_offline", "ha_started", "logging_started"]
    row = result["occurrences"][0]
    assert not row["time_known"] and row["raised_after"] is not None


async def test_ha_start_is_not_an_hmi_outage(
    env, hass: HomeAssistant, pack, hass_ws_client, freezer: FrozenDateTimeFactory
) -> None:
    set_all(hass)
    await async_wait_recording_done(hass)
    entry = await setup(hass)
    # Logged in before the clock jumps an hour (the test login expires sooner).
    client = await hass_ws_client()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)

    # Home Assistant restarts: the HMI's entities are unavailable for a moment while it connects.
    freezer.tick(timedelta(hours=1))
    started = dt_util.utcnow()
    get_instance(hass).recorder_runs_manager._recording_start = started
    with patch("custom_components.cellkeeper.pack.ha_starts", return_value=[started.timestamp()]):
        freezer.tick(timedelta(seconds=2))
        set_all(hass, "unavailable")
        freezer.tick(timedelta(seconds=3))
        set_all(hass)
        await async_wait_recording_done(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await settle(hass)
    result = await ask(client, {"type": "cellkeeper/events/list", "prefix": "ck0"})
    kinds = [m["kind"] for m in result["markers"]]
    assert "hmi_offline" not in kinds and kinds[0] == "ha_started"
    assert result["markers"][0]["t"] == pytest.approx(started.timestamp(), abs=1)


async def test_backup_holds_writes(
    env, hass: HomeAssistant, pack, hass_ws_client, freezer: FrozenDateTimeFactory
) -> None:
    set_all(hass)
    await async_wait_recording_done(hass)
    await setup(hass)

    await async_pre_backup(hass)
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(ent("warning_overcurrent_charging"), "WARNING (1)")
    await settle(hass)
    info = await ws(hass_ws_client, {"type": "cellkeeper/info"})
    assert info["occurrences"] == 0

    await async_post_backup(hass)
    info = await ws(hass_ws_client, {"type": "cellkeeper/info"})
    assert info["occurrences"] == 1


async def test_config_flow(env, hass: HomeAssistant, pack) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    assert "ck0" in result["description_placeholders"]["packs"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    await settle(hass)
    # Only one entry.
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "abort" and result["reason"] == "single_instance_allowed"
