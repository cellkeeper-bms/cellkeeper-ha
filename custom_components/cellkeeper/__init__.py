"""CellKeeper: the CellKeeper BMS dashboard, and a long-term history of its packs.

It serves the dashboard (the custom:cellkeeper strategy, frontend.py) on every page, and keeps
every event occurrence (from the HMI's event sensors) in its own SQLite database, so it outlives
the recorder's 10 days. The dashboard reads that over the websocket API.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from pathlib import Path

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import DB_DIR, DB_FILE, DOMAIN
from .dashboards import async_create_dashboards
from .discovery import find_packs
from .frontend import async_check_old_install, async_remove_frontend, async_setup_frontend
from .pack import PackLogger
from .runtime import Runtime
from .store import Store
from .websocket import async_register_websocket

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# How often the packs' "synced until" moves on when nothing changes, so a restart's catch-up
# doesn't start far back.
SYNC_INTERVAL = timedelta(minutes=10)

type CellKeeperConfigEntry = ConfigEntry[Runtime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_websocket(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CellKeeperConfigEntry) -> bool:
    store = Store(Path(hass.config.path(DB_DIR, DB_FILE)))
    await store.run(hass, store.open)
    runtime = Runtime(hass, store)
    await runtime.async_load()
    entry.runtime_data = runtime
    hass.data[DOMAIN] = runtime

    # The dashboard, on every page from now on.
    version = (await async_get_integration(hass, DOMAIN)).version
    url = await async_setup_frontend(hass, str(version))
    entry.async_on_unload(lambda: async_remove_frontend(hass, url))
    entry.async_create_background_task(hass, async_check_old_install(hass), "cellkeeper old dashboard check")

    async def start_packs() -> None:
        # The recorder's history is read on the first start, so wait until it can be.
        await get_instance(hass).async_db_ready
        packs = find_packs(hass)
        # A dashboard in the sidebar for each pack that has none yet (once per pack).
        try:
            await async_create_dashboards(hass, entry, list(packs.values()))
        except Exception:
            _LOGGER.exception("Couldn't make the packs' dashboards")
        for prefix, info in packs.items():
            if prefix in runtime.packs:
                continue
            pack = PackLogger(hass, runtime, info)
            runtime.packs[prefix] = pack
            try:
                await pack.async_start()
                _LOGGER.info("Logging %s (%s event sensors)", prefix, len(info.events))
            except Exception:
                _LOGGER.exception("Couldn't start logging %s", prefix)
                pack.async_stop()
                runtime.packs.pop(prefix, None)

    entry.async_create_background_task(hass, start_packs(), "cellkeeper start packs")

    # A pack added later (a new HMI) is picked up when its entities are registered.
    debouncer = Debouncer(hass, _LOGGER, cooldown=10, immediate=False, function=start_packs)
    entry.async_on_unload(debouncer.async_shutdown)

    @callback
    def registry_updated(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if event.data["action"] in ("create", "update"):
            debouncer.async_schedule_call()

    entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_updated))

    async def sync(_now: object = None) -> None:
        now = dt_util.utcnow().timestamp()
        for prefix in list(runtime.packs):
            await runtime.async_write(prefix, [], [], {}, now)

    entry.async_on_unload(async_track_time_interval(hass, sync, SYNC_INTERVAL))

    async def stopping(_event: Event) -> None:
        await sync()
        await runtime.async_close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stopping))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CellKeeperConfigEntry) -> bool:
    await entry.runtime_data.async_close()
    hass.data.pop(DOMAIN, None)
    return True
