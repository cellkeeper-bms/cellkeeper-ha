"""The integration's own sensors, on a device of their own for each pack, so Home Assistant's long-term
statistics keep them too: contactor operations, cycles, the last full charge's cell spread and
this month's warnings. They're counted from the integration's database."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .pack import LOAD_A

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HealthSensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any]], Any]


SENSORS = (
    HealthSensorDescription(
        key="contactor_operations",
        name="Contactor Operations",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:electric-switch",
        value=lambda t: t["contactor_operations"],
    ),
    HealthSensorDescription(
        key="contactor_openings_under_load",
        name="Contactor Openings Under Load",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:flash-alert",
        value=lambda t: t["contactor_openings_under_load"],
    ),
    HealthSensorDescription(
        key="equivalent_full_cycles",
        name="Equivalent Full Cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:battery-sync",
        suggested_display_precision=1,
        value=lambda t: t["cycles"],
    ),
    HealthSensorDescription(
        key="full_charge_spread",
        name="Full Charge Cell Spread",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        icon="mdi:battery-check",
        suggested_display_precision=0,
        value=lambda t: None if t["full_spread"] is None else round(t["full_spread"] * 1000, 1),
    ),
    HealthSensorDescription(
        key="warnings_this_month",
        name="Warnings This Month",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda t: t["warnings_month"],
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: Any, async_add_entities: AddEntitiesCallback) -> None:
    runtime = entry.runtime_data
    added: set[str] = set()

    @callback
    def add_pack(prefix: str) -> None:
        if prefix in added or (pack := runtime.packs.get(prefix)) is None:
            return
        added.add(prefix)
        totals = PackTotals(hass, runtime, prefix)
        entities = [HealthSensor(totals, pack.info.name, description) for description in SENSORS]
        entry.async_on_unload(totals.async_start())
        async_add_entities(entities)

    runtime.on_pack_started = add_pack
    for prefix in list(runtime.packs):
        add_pack(prefix)


class PackTotals:
    """The totals for one pack, read again whenever the database changes for it."""

    def __init__(self, hass: HomeAssistant, runtime: Any, prefix: str) -> None:
        self.hass = hass
        self.runtime = runtime
        self.prefix = prefix
        self.values: dict[str, Any] | None = None
        self.listeners: list[Callable[[], None]] = []
        self._debouncer = Debouncer(hass, _LOGGER, cooldown=5, immediate=True, function=self.async_refresh)

    @callback
    def async_start(self) -> Callable[[], None]:
        @callback
        def changed(prefix: str, _payload: dict[str, list[Any]]) -> None:
            if prefix == self.prefix:
                self._debouncer.async_schedule_call()

        remove = self.runtime.async_listen(changed)
        self.hass.async_create_task(self.async_refresh())

        @callback
        def stop() -> None:
            remove()
            self._debouncer.async_shutdown()

        return stop

    async def async_refresh(self) -> None:
        store = self.runtime.store
        if store.closed:
            return
        month = dt_util.start_of_local_day(dt_util.now().replace(day=1)).timestamp()
        self.values = await store.run(self.hass, store.health_totals, self.prefix, month, LOAD_A)
        for listener in list(self.listeners):
            listener()


class HealthSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, totals: PackTotals, name: str, description: HealthSensorDescription) -> None:
        self.entity_description = description
        self._totals = totals
        self._attr_unique_id = f"{totals.prefix}_{description.key}"
        # Ids like the HMI's own entities: sensor.ck0_...
        self.entity_id = f"sensor.{totals.prefix}_{description.key}"
        # A device can only belong to one integration, so the pack's history has its own.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, totals.prefix)},
            name=f"{name} history",
            manufacturer="CellKeeper",
            model="Pack history",
        )

    @property
    def available(self) -> bool:
        return self._totals.values is not None

    @property
    def native_value(self) -> Any:
        return None if self._totals.values is None else self.entity_description.value(self._totals.values)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key == "full_charge_spread" and self._totals.values:
            t = self._totals.values.get("full_t")
            return {"last_full_charge": dt_util.utc_from_timestamp(t).isoformat()} if t else None
        return None

    async def async_added_to_hass(self) -> None:
        self._totals.listeners.append(self.async_write_ha_state)
        self.async_on_remove(lambda: self._totals.listeners.remove(self.async_write_ha_state))
