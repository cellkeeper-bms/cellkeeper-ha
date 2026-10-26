"""The websocket API the dashboard reads the history with.

cellkeeper/packs                       the packs being logged
cellkeeper/events/list                 a pack's occurrences and markers, newest first
cellkeeper/events/subscribe            new and changed occurrences as they happen
cellkeeper/info                        the database: where, how big

Times are seconds since the epoch (UTC).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

MAX_LIMIT = 5000


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_packs)
    websocket_api.async_register_command(hass, ws_events_list)
    websocket_api.async_register_command(hass, ws_events_subscribe)
    websocket_api.async_register_command(hass, ws_info)


def _runtime(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> Any:
    runtime = hass.data.get(DOMAIN)
    if runtime is None or runtime.closed:
        connection.send_error(msg["id"], "not_loaded", "The CellKeeper integration isn't running")
        return None
    return runtime


@websocket_api.websocket_command({vol.Required("type"): "cellkeeper/packs"})
@websocket_api.async_response
async def ws_packs(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if not (runtime := _runtime(hass, connection, msg)):
        return
    packs = await runtime.store.run(hass, runtime.store.packs)
    for p in packs:
        p["live"] = p["prefix"] in runtime.packs
    connection.send_result(msg["id"], {"packs": packs})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "cellkeeper/events/list",
        vol.Required("prefix"): str,
        vol.Optional("start"): vol.Coerce(float),
        vol.Optional("end"): vol.Coerce(float),
        vol.Optional("event"): str,
        vol.Optional("before_id"): int,
        vol.Optional("limit", default=1000): vol.All(int, vol.Range(min=1, max=MAX_LIMIT)),
    }
)
@websocket_api.async_response
async def ws_events_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if not (runtime := _runtime(hass, connection, msg)):
        return
    store = runtime.store
    rows = await store.run(
        hass, store.occurrences, msg["prefix"], msg.get("start"), msg.get("end"), msg.get("event"),
        msg.get("before_id"), msg["limit"],
    )
    markers = await store.run(hass, store.markers, msg["prefix"], msg.get("start"), msg.get("end"), MAX_LIMIT)
    pack = await store.run(hass, store.pack, msg["prefix"])
    connection.send_result(
        msg["id"],
        {
            "occurrences": rows,
            "markers": markers,
            "more": len(rows) == msg["limit"],
            "synced_until": pack["synced_until"] if pack else None,
        },
    )


@websocket_api.websocket_command(
    {vol.Required("type"): "cellkeeper/events/subscribe", vol.Optional("prefix"): str}
)
@websocket_api.async_response
async def ws_events_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if not (runtime := _runtime(hass, connection, msg)):
        return
    want = msg.get("prefix")

    @callback
    def forward(prefix: str, rows: list[dict[str, Any]], markers: list[dict[str, Any]]) -> None:
        if want and prefix != want:
            return
        connection.send_message(
            websocket_api.event_message(msg["id"], {"prefix": prefix, "occurrences": rows, "markers": markers})
        )

    connection.subscriptions[msg["id"]] = runtime.async_listen(forward)
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "cellkeeper/info"})
@websocket_api.async_response
async def ws_info(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if not (runtime := _runtime(hass, connection, msg)):
        return
    connection.send_result(msg["id"], await runtime.store.run(hass, runtime.store.stats))
