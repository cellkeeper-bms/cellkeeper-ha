"""Finding CellKeeper packs: ESPHome devices with the HMI's event sensors."""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import slugify

# A device needs this many event sensors to count as a pack (the HMI has 61).
MIN_EVENT_SENSORS = 5

BOOT_EVENT = "info_boot_normal"


@dataclass
class PackInfo:
    """One pack: its prefix (ck0) and its event sensors."""

    prefix: str
    name: str
    device_id: str
    mac: str | None
    events: dict[str, str] = field(default_factory=dict)  # entity_id -> event key

    @property
    def presence_entity(self) -> str:
        """The sensor that says whether the HMI is connected: the boot event, or any event."""
        for entity_id, key in self.events.items():
            if key == BOOT_EVENT:
                return entity_id
        return next(iter(self.events))


def event_key(entry: er.RegistryEntry) -> str | None:
    """The event's key from the HMI's name for it, "Event Warning Overcurrent Discharging" ->
    "warning_overcurrent_discharging". It's the same as the end of the default entity id, but
    survives renaming the entity."""
    name = slugify(entry.original_name or "")
    for level in ("fatal", "critical", "warning", "info"):
        if name.startswith(f"event_{level}_"):
            return name[len("event_") :]
    return None


def find_packs(hass: HomeAssistant) -> dict[str, PackInfo]:
    """Every pack in the entity registry, by prefix."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    by_device: dict[str, dict[str, str]] = {}
    for entry in ent_reg.entities.values():
        if entry.platform != "esphome" or entry.domain != "sensor" or not entry.device_id:
            continue
        if entry.disabled_by is not None:
            continue
        key = event_key(entry)
        if key:
            by_device.setdefault(entry.device_id, {})[entry.entity_id] = key

    packs: dict[str, PackInfo] = {}
    for device_id, events in by_device.items():
        if len(events) < MIN_EVENT_SENSORS:
            continue
        device = dev_reg.async_get(device_id)
        prefix = _prefix(events, device)
        if not prefix or prefix in packs:
            continue
        mac = None
        for entity_id in events:
            unique_id = ent_reg.entities[entity_id].unique_id
            mac = unique_id.split("/", 1)[0] if "/" in unique_id else None
            break
        packs[prefix] = PackInfo(
            prefix=prefix,
            name=(device.name_by_user or device.name) if device else prefix,
            device_id=device_id,
            mac=mac,
            events=events,
        )
    return packs


def _prefix(events: dict[str, str], device: dr.DeviceEntry | None) -> str | None:
    """The entity ids' prefix (sensor.ck0_event_... -> ck0), as the dashboard uses it. The most
    common one wins if some entities were renamed; the device's name is the fallback."""
    counts: dict[str, int] = {}
    for entity_id, key in events.items():
        object_id = entity_id.split(".", 1)[1]
        tail = f"_event_{key}"
        if object_id.endswith(tail):
            p = object_id[: -len(tail)]
            counts[p] = counts.get(p, 0) + 1
    if counts:
        return max(counts, key=counts.get)
    if device:
        return slugify(device.name_by_user or device.name or "") or None
    return None
