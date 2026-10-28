"""Records and snapshots in a test Home Assistant: from the recorder's history and live."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from .test_init import EVENTS, MAC, ask, ent, set_all, settle, setup

# entity -> (the HMI's name for it, platform, unit)
OTHERS = {
    "sensor.ck0_contactor_state_text": ("Contactor State Text", "text_sensor", None),
    "sensor.ck0_system_state_text": ("System State Text", "text_sensor", None),
    "sensor.ck0_millis": ("Millis", "sensor", "s"),
    "sensor.ck0_bms_firmware": ("BMS Firmware", "text_sensor", None),
    "sensor.ck0_balance_charge_status": ("Balance Charge Status", "text_sensor", None),
    "sensor.ck0_current": ("Current", "sensor", "A"),
    "sensor.ck0_pack_internal_voltage": ("Pack Internal Voltage", "sensor", "V"),
    "sensor.ck0_module_temperature_max": ("Module Temperature Max", "sensor", "°F"),
    "sensor.ck0_cell_voltage_001": ("Cell Voltage 001", "sensor", "V"),
    "sensor.ck0_cell_voltage_002": ("Cell Voltage 002", "sensor", "V"),
    "sensor.ck0_cell_balancing_time_001": ("Cell Balancing Time 001", "sensor", None),
    "sensor.ck0_cell_balancing_time_002": ("Cell Balancing Time 002", "sensor", None),
    "number.ck0_max_charge_current": ("Max Charge Current", "number", "A"),
    "number.ck0_soc_set_cmd": ("SOC Set Cmd", "number", "%"),
}


@pytest.fixture
def env(recorder_mock, enable_custom_integrations: None) -> None:
    """The recorder (it has to start before Home Assistant does) and custom_components/."""


@pytest.fixture
def pack(hass: HomeAssistant, tmp_path) -> None:
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
    for entity_id, (name, platform, unit) in OTHERS.items():
        domain, object_id = entity_id.split(".")
        reg.async_get_or_create(
            domain, "esphome", f"{MAC}/0/{platform}/{name}", suggested_object_id=object_id,
            original_name=name, device_id=device.id, config_entry=esphome, unit_of_measurement=unit,
        )


def put(hass: HomeAssistant, entity_id: str, state: str, context: Context | None = None) -> None:
    unit = OTHERS[entity_id][2]
    hass.states.async_set(entity_id, state, {"unit_of_measurement": unit} if unit else {}, context=context)


def values(hass: HomeAssistant, current: str = "0", cells: tuple[str, str] = ("3.300", "3.310")) -> None:
    put(hass, "sensor.ck0_current", current)
    put(hass, "sensor.ck0_pack_internal_voltage", "400.0")
    put(hass, "sensor.ck0_module_temperature_max", "77.0")
    put(hass, "sensor.ck0_cell_voltage_001", cells[0])
    put(hass, "sensor.ck0_cell_voltage_002", cells[1])


async def records(client, **kw) -> list[dict]:
    return (await ask(client, {"type": "cellkeeper/records/list", "prefix": "ck0", **kw}))["records"]


async def test_records_from_history_and_live(
    env, hass: HomeAssistant, pack, hass_ws_client, hass_admin_user, freezer: FrozenDateTimeFactory
) -> None:
    # Before the integration: the contactor closes under 5 A, a setting changes.
    set_all(hass)
    values(hass)
    put(hass, "sensor.ck0_contactor_state_text", "OPEN")
    put(hass, "sensor.ck0_system_state_text", "INACTIVE")
    put(hass, "sensor.ck0_bms_firmware", "1.0")
    put(hass, "sensor.ck0_millis", "1000")
    put(hass, "number.ck0_max_charge_current", "50.0")
    put(hass, "sensor.ck0_balance_charge_status", "Floating, next today 10:00")
    for n in (1, 2):
        put(hass, f"sensor.ck0_cell_balancing_time_00{n}", "100")
    await async_wait_recording_done(hass)
    freezer.tick(timedelta(minutes=1))
    values(hass, current="5.0")
    put(hass, "sensor.ck0_contactor_state_text", "PRECHARGING")
    freezer.tick(timedelta(seconds=1))
    closed_at = dt_util.utcnow().timestamp()
    put(hass, "sensor.ck0_contactor_state_text", "CLOSED")
    put(hass, "number.ck0_max_charge_current", "55.0")
    # A command reads back a live value: not a setting.
    put(hass, "number.ck0_soc_set_cmd", "50.1")
    put(hass, "number.ck0_soc_set_cmd", "50.2")
    await async_wait_recording_done(hass)
    freezer.tick(timedelta(minutes=1))

    await setup(hass)
    client = await hass_ws_client()
    rows = await records(client)
    by_kind = {(r["kind"], r["key"]): r for r in rows}
    closed = by_kind[("contactor", "closed")]
    assert closed["t"] == pytest.approx(closed_at, abs=0.01) and closed["time_known"]
    assert closed["data"]["values"]["current"] == 5.0 and closed["data"]["values"]["pack_voltage"] == 400.0
    setting = by_kind[("setting", "max_charge_current")]
    assert (setting["old"], setting["new"]) == ("50.0", "55.0") and "by" not in setting["data"]
    assert by_kind[("firmware", "bms")]["new"] == "1.0"
    assert ("setting", "soc_set_cmd") not in by_kind

    # (Short steps while it's subscribed: a longer jump looks like a missed heartbeat.)
    await client.send_json_auto_id({"type": "cellkeeper/events/subscribe", "prefix": "ck0"})
    assert (await client.receive_json())["success"]

    # Live: a user changes a setting...
    freezer.tick(timedelta(seconds=10))
    put(hass, "number.ck0_max_charge_current", "60.0", Context(user_id=hass_admin_user.id))
    await settle(hass)
    event = (await client.receive_json())["event"]
    (row,) = event["records"]
    assert row["new"] == "60.0" and row["data"]["by"] == "user" and row["data"]["user"] == hass_admin_user.name
    # ...and the HMI changes one itself.
    freezer.tick(timedelta(seconds=10))
    put(hass, "number.ck0_max_charge_current", "65.0")
    await settle(hass)
    assert (await client.receive_json())["event"]["records"][0]["data"] == {"by": "hmi"}

    # The BMS restarts: it's been up 20 s when the HMI reads it again, and the next reading agrees.
    freezer.tick(timedelta(seconds=10))
    now = dt_util.utcnow().timestamp()
    put(hass, "sensor.ck0_millis", "20")
    await settle(hass)
    freezer.tick(timedelta(seconds=1))
    put(hass, "sensor.ck0_millis", "21")
    await settle(hass)
    (row,) = (await client.receive_json())["event"]["records"]
    assert (row["kind"], row["key"]) == ("restart", "bms") and row["t"] == pytest.approx(now - 20, abs=0.01)

    # A balance charge: cell 2 is bled for 300 s and the spread closes.
    freezer.tick(timedelta(seconds=10))
    put(hass, "sensor.ck0_balance_charge_status", "Balance charging, 2h00m left")
    await settle(hass)
    started = (await client.receive_json())["event"]["records"][0]
    assert started["data"]["before"]["spread"] == pytest.approx(0.010)
    await async_wait_recording_done(hass)
    freezer.tick(timedelta(seconds=20))
    values(hass, cells=("3.400", "3.402"))
    put(hass, "sensor.ck0_cell_balancing_time_002", "400")
    put(hass, "sensor.ck0_balance_charge_status", "Floating, next in 7 days 10:00")
    await settle(hass)
    ended = (await client.receive_json())["event"]["records"][0]
    assert ended["id"] == started["id"] and ended["end"] is not None
    assert ended["data"]["before"]["spread"] == pytest.approx(0.010)
    assert ended["data"]["after"]["spread"] == pytest.approx(0.002)
    assert ended["data"]["balanced"] == {"2": 300}

    assert {r["kind"] for r in await records(client, kinds=["setting"])} == {"setting"}


async def test_snapshot_of_a_warning(
    env, hass: HomeAssistant, pack, hass_ws_client, freezer: FrozenDateTimeFactory
) -> None:
    set_all(hass)
    values(hass, current="10")
    await async_wait_recording_done(hass)
    await setup(hass)

    # The current climbs over the minute before an overcurrent warning.
    for i in range(30):
        freezer.tick(timedelta(seconds=2))
        values(hass, current=str(10 + i * 5))
    raised_at = dt_util.utcnow().timestamp()
    hass.states.async_set(ent("warning_overcurrent_charging"), "WARNING (1)")
    await settle(hass)
    freezer.tick(timedelta(seconds=5))
    values(hass, current="0")
    await async_wait_recording_done(hass)

    client = await hass_ws_client()
    rows = (await ask(client, {"type": "cellkeeper/events/list", "prefix": "ck0"}))["occurrences"]
    assert not rows[0]["snapshot"]  # not until the recorder has the seconds after it
    for _ in range(3):
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
    await settle(hass)
    await async_wait_recording_done(hass)
    await settle(hass)

    rows = (await ask(client, {"type": "cellkeeper/events/list", "prefix": "ck0"}))["occurrences"]
    assert rows[0]["snapshot"]
    snap = await ask(client, {"type": "cellkeeper/snapshot", "occurrence_id": rows[0]["id"]})
    s = snap["series"]
    assert snap["t"] == pytest.approx(raised_at, abs=0.01)
    assert s["current"][60] == 155.0 and s["current"][-1] == 0.0
    assert s["temp_max"][60] == pytest.approx(25.0)  # 77 °F
    assert s["cell_max_n"][60] == 2


async def test_upgrade_reads_the_whole_history(
    env, hass: HomeAssistant, pack, hass_ws_client, freezer: FrozenDateTimeFactory
) -> None:
    """An entry that logged events before records existed: the first start with records reads the
    recorder's whole history for them, and takes snapshots of the warnings already logged."""
    set_all(hass)
    values(hass, current="10")
    put(hass, "sensor.ck0_contactor_state_text", "OPEN")
    await async_wait_recording_done(hass)
    freezer.tick(timedelta(minutes=1))
    put(hass, "sensor.ck0_contactor_state_text", "CLOSED")
    for i in range(10):
        freezer.tick(timedelta(seconds=2))
        values(hass, current=str(10 + i))
    hass.states.async_set(ent("warning_overcurrent_charging"), "WARNING (1)")
    await async_wait_recording_done(hass)
    freezer.tick(timedelta(minutes=1))
    values(hass, current="0")
    await async_wait_recording_done(hass)
    entry = await setup(hass)

    # As if it had run 0.14.0: events logged, no records, no snapshots.
    runtime = hass.data["cellkeeper"]
    def forget(db=runtime.store._db) -> None:
        db.execute("DELETE FROM records")
        db.execute("DELETE FROM snapshots")
        db.execute("DELETE FROM trackers WHERE event = '_records'")
    await runtime.store.run(hass, forget)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)

    client = await hass_ws_client()
    rows = await records(client, kinds=["contactor"])
    assert [r["key"] for r in rows] == ["closed"]
    occurrences = (await ask(client, {"type": "cellkeeper/events/list", "prefix": "ck0"}))["occurrences"]
    assert occurrences[0]["snapshot"]
