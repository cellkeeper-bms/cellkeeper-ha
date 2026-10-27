"""Long-term health: the day's pass (health.py) on its own, and in a test Home Assistant from the
recorder's history of a whole day."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.cellkeeper.health import FULL_HOLD_S, REARM_S, Day, FullDetector, balancing, cells_data

from .test_init import EVENTS, MAC, ask, set_all, settle, setup


# ---- the pass on its own ----


def test_ah_in_and_out_and_ranges() -> None:
    d = Day(0, 7200, FullDetector(cells=2))
    d.feed(0, "pack_voltage", 400.0)
    d.feed(0, "capacity", 100.0)
    d.feed(0, "current", 10.0)  # charging 10 A for an hour
    d.feed(0, "soc", 50.0)
    d.feed(3600, "current", -20.0)  # discharging 20 A for half an hour
    d.feed(3600, "soc", 60.0)
    d.feed(5400, "current", 0.0)
    d.feed(5400, "soc", 50.0)
    d.feed(6000, "temp_max", 30.0)
    d.finish()
    s = d.summary()
    assert s["ah_in"] == pytest.approx(10) and s["ah_out"] == pytest.approx(10)
    assert s["kwh_in"] == pytest.approx(4) and s["kwh_out"] == pytest.approx(4)
    assert s["cycles"] == pytest.approx(0.1) and s["covered"] == 1
    assert (s["soc_min"], s["soc_max"], s["temp_max"]) == (50.0, 60.0, 30.0)
    assert d.soc_min == (50.0, 0)  # the first time it was lowest


def test_a_gap_in_the_readings_isnt_integrated() -> None:
    d = Day(0, 7200, FullDetector())
    d.feed(0, "current", 10.0)
    d.feed(3600, "current", None)  # unavailable: the HMI went away
    d.feed(5400, "current", 10.0)
    d.finish()
    assert d.summary()["ah_in"] == pytest.approx(10 + 5)


def test_full_at_the_inverters_target_once_tapered() -> None:
    det = FullDetector(cells=100)
    d = Day(0, 86400, det)
    d.feed(0, "charge_target", 400.0)
    d.feed(0, "soc", 95.0)
    d.feed(0, "pack_voltage", 399.5)  # within 0.01 V per cell
    d.feed(0, "current", 20.0)  # still charging hard
    d.feed(600, "current", 0.5)  # tapered off
    d.feed(600 + FULL_HOLD_S - 1, "soc", 99.0)
    assert not d.fulls
    d.feed(2000, "soc", 99.5)
    assert d.fulls == [600 + FULL_HOLD_S]
    # Still floating there an hour later: the same full charge.
    d.feed(6000, "soc", 99.6)
    assert len(d.fulls) == 1
    # After discharging well below it, the next one counts.
    d.feed(7000, "current", -30.0)
    d.feed(7000, "pack_voltage", 380.0)
    d.feed(9000, "soc", 80.0)
    d.feed(10000, "current", 0.2)
    d.feed(10000, "pack_voltage", 399.8)
    d.finish()
    # (And once more that evening, still floating there.)
    assert d.fulls == [600 + FULL_HOLD_S, 10000 + FULL_HOLD_S, 10000 + FULL_HOLD_S + REARM_S]


def test_full_at_the_working_max_without_a_target() -> None:
    det = FullDetector(cells=15, working_max=4.15)
    d = Day(0, 86400, det)
    d.feed(0, "current", 0.3)
    d.feed(0, "cell_max", 4.10)
    d.feed(100, "cell_max", 4.13)
    d.finish()
    # Floating there: a full charge, then another each time the last is REARM_S old.
    first = 100 + FULL_HOLD_S
    assert d.fulls == [first, first + REARM_S]
    # The hold carries over to the next day.
    d2 = Day(86400, 2 * 86400, det, values=dict(d.values))
    d2.feed(86400 + 3600, "soc", 99.0)
    d2.finish()
    assert d2.fulls == [first + 2 * REARM_S]
    s = det.snapshot()
    det2 = FullDetector()
    det2.restore(s)
    assert det2.last_t == first + 2 * REARM_S


def test_balancing_counts_across_a_restart() -> None:
    pts = {1: [(0, 100.0), (10, 160.0), (20, 5.0), (30, 25.0)], 2: [(0, 50.0), (10, 50.0)], 3: [(0, None)]}
    assert balancing(pts) == {"1": 80}


def test_cells_data() -> None:
    c = cells_data({1: 3.30, 2: 3.35, 3: None, 4: 3.32})
    assert c["v"] == [3.3, 3.35, None, 3.32]
    assert (c["min_cell"], c["max_cell"], c["spread"]) == (1, 2, pytest.approx(0.05))


# ---- in Home Assistant ----

OTHERS = {
    "sensor.ck0_current": ("Current", "sensor", "A"),
    "sensor.ck0_pack_internal_voltage": ("Pack Internal Voltage", "sensor", "V"),
    "sensor.ck0_soc": ("SOC", "sensor", "%"),
    "sensor.ck0_capacity": ("Capacity", "sensor", "Ah"),
    "sensor.ck0_inverter_can_voltage_limit_max": ("Inverter CAN Voltage Limit Max", "sensor", "V"),
    "sensor.ck0_module_temperature_max": ("Module Temperature Max", "sensor", "°C"),
    "sensor.ck0_contactor_state_text": ("Contactor State Text", "text_sensor", None),
    "sensor.ck0_cell_voltage_001": ("Cell Voltage 001", "sensor", "V"),
    "sensor.ck0_cell_voltage_002": ("Cell Voltage 002", "sensor", "V"),
    "sensor.ck0_cell_balancing_time_001": ("Cell Balancing Time 001", "sensor", "s"),
    "sensor.ck0_cell_balancing_time_002": ("Cell Balancing Time 002", "sensor", "s"),
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


def put(hass: HomeAssistant, entity_id: str, state: str) -> None:
    unit = OTHERS[entity_id][2]
    hass.states.async_set(entity_id, state, {"unit_of_measurement": unit} if unit else {})


async def test_a_day_from_the_recorder(
    env, hass: HomeAssistant, pack, hass_ws_client, freezer: FrozenDateTimeFactory
) -> None:
    """Yesterday: discharged 20 A for an hour, then charged to full at the inverter's target."""
    # (It ends a minute after now: the websocket's token isn't valid before it was made.)
    now = dt_util.now()
    day = dt_util.start_of_local_day(now - timedelta(days=1))
    freezer.move_to(day + timedelta(hours=1))
    set_all(hass)
    put(hass, "sensor.ck0_capacity", "100")
    put(hass, "sensor.ck0_inverter_can_voltage_limit_max", "8.4")
    put(hass, "sensor.ck0_pack_internal_voltage", "7.6")
    put(hass, "sensor.ck0_module_temperature_max", "25")
    put(hass, "sensor.ck0_soc", "60")
    put(hass, "sensor.ck0_current", "-20")
    put(hass, "sensor.ck0_cell_voltage_001", "3.80")
    put(hass, "sensor.ck0_cell_voltage_002", "3.81")
    put(hass, "sensor.ck0_cell_balancing_time_001", "0")
    put(hass, "sensor.ck0_cell_balancing_time_002", "0")
    await async_wait_recording_done(hass)

    # The day's low, an hour later.
    freezer.move_to(day + timedelta(hours=2))
    low_at = dt_util.utcnow().timestamp()
    put(hass, "sensor.ck0_soc", "40")
    put(hass, "sensor.ck0_current", "10")
    put(hass, "sensor.ck0_cell_voltage_001", "3.60")
    put(hass, "sensor.ck0_cell_voltage_002", "3.65")
    await async_wait_recording_done(hass)

    # Charging to the target; cell 2 is bled for a minute.
    freezer.move_to(day + timedelta(hours=8))
    put(hass, "sensor.ck0_pack_internal_voltage", "8.39")
    put(hass, "sensor.ck0_soc", "99")
    put(hass, "sensor.ck0_cell_voltage_001", "4.19")
    put(hass, "sensor.ck0_cell_voltage_002", "4.20")
    put(hass, "sensor.ck0_cell_balancing_time_002", "60")
    await async_wait_recording_done(hass)
    freezer.move_to(day + timedelta(hours=8, minutes=30))
    tapered_at = dt_util.utcnow().timestamp()
    put(hass, "sensor.ck0_current", "0.5")
    await async_wait_recording_done(hass)

    freezer.move_to(now + timedelta(minutes=1))
    await setup(hass)
    await settle(hass)

    client = await hass_ws_client()
    rows = (await ask(client, {"type": "cellkeeper/records/list", "prefix": "ck0",
                               "kinds": ["daily", "empty", "full_charge"]}))["records"]
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    daily = next(r for r in by_kind["daily"] if r["key"] == day.date().isoformat())
    d = daily["data"]
    assert d["ah_out"] == pytest.approx(20, rel=1e-3)
    assert d["ah_in"] == pytest.approx(10 * 6.5 + 0.5 * 15.5, rel=1e-3)
    assert d["cycles"] == pytest.approx(0.2, rel=1e-3)
    assert d["balancing"] == {"2": 60}
    assert (d["soc_min"], d["soc_max"]) == (40.0, 99.0)

    (empty,) = [r for r in by_kind["empty"] if r["key"] == day.date().isoformat()]
    assert empty["t"] == pytest.approx(low_at, abs=0.01)
    assert empty["data"]["cells"]["v"] == [3.6, 3.65] and empty["data"]["cells"]["min_cell"] == 1

    (full,) = by_kind["full_charge"]
    assert full["t"] == pytest.approx(tapered_at + FULL_HOLD_S, abs=0.01)
    assert full["data"]["cells"]["spread"] == pytest.approx(0.01)
    assert full["data"]["values"]["soc"] == 99.0

    # The pack's own sensors.
    assert hass.states.get("sensor.ck0_equivalent_full_cycles").state == "0.2"
    assert float(hass.states.get("sensor.ck0_full_charge_spread").state) == pytest.approx(10)
    assert hass.states.get("sensor.ck0_contactor_operations").state == "0"

    # Worked out again from the recorder: the same day, once.
    assert (await ask(client, {"type": "cellkeeper/health/rebuild", "prefix": "ck0"}))["started"]
    await hass.async_block_till_done(wait_background_tasks=True)
    again = (await ask(client, {"type": "cellkeeper/records/list", "prefix": "ck0", "kinds": ["daily", "full_charge"]}))["records"]
    assert sorted(r["kind"] for r in again) == ["daily", "full_charge"]
    assert next(r for r in again if r["kind"] == "daily")["data"]["ah_out"] == pytest.approx(20, rel=1e-3)
