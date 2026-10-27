"""What the integration holds while it runs: the database, the packs, and who's listening."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .pack import PackLogger
from .records import RecordOp
from .store import Store
from .tracker import Op

_LOGGER = logging.getLogger(__name__)

# (prefix, {"occurrences", "markers", "records", "snapshots"}) for each write that inserted or
# changed something: the rows as they are now, and the ids of new snapshots
Listener = Callable[[str, dict[str, list[Any]]], None]


class Runtime:
    """The integration's state, kept in hass.data."""

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store
        self.packs: dict[str, PackLogger] = {}
        self.listeners: list[Listener] = []
        # The sensor platform's: adds a pack's sensors once it's logging.
        self.on_pack_started: Callable[[str], None] | None = None
        self._id = 0
        self._record_id = 0
        self.closed = False
        self._paused = False
        self._pending: list[tuple[Any, ...]] = []
        self._write_lock = asyncio.Lock()

    async def async_load(self) -> None:
        self._id = await self.store.run(self.hass, self.store.max_id)
        self._record_id = await self.store.run(self.hass, self.store.max_record_id)

    def next_id(self) -> int:
        """A new occurrence id. Ids are handed out here, in order, so trackers can refer to rows
        before they're written."""
        self._id += 1
        return self._id

    def next_record_id(self) -> int:
        self._record_id += 1
        return self._record_id

    # ---- writing ----

    async def async_write(self, prefix: str, ops: list[Op], markers: list[tuple[float, str, str | None]],
                          trackers: dict[str, dict[str, Any]], synced_until: float | None,
                          records: list[RecordOp] = (), snapshots: list[tuple[int, float, dict[str, Any]]] = ()) -> None:
        """Write a batch, or hold it while a backup is copying the database."""
        batch = (prefix, ops, markers, trackers, synced_until, list(records), list(snapshots))
        if self._paused:
            self._pending.append(batch)
            return
        await self._write_now(*batch)

    async def _write_now(self, prefix: str, ops: list[Op], markers: list[tuple[float, str, str | None]],
                         trackers: dict[str, dict[str, Any]], synced_until: float | None,
                         records: list[RecordOp], snapshots: list[tuple[int, float, dict[str, Any]]]) -> None:
        async with self._write_lock:
            if self.store.closed:
                return
            await self.store.run(self.hass, self.store.apply, prefix, ops, markers, trackers, synced_until, records, snapshots)
        if self.listeners and (ops or markers or records or snapshots) and not self.store.closed:
            await self._notify(prefix, ops, markers, records, snapshots)

    async def _notify(self, prefix: str, ops: list[Op], markers: list[tuple[float, str, str | None]],
                      records: list[RecordOp], snapshots: list[tuple[int, float, dict[str, Any]]]) -> None:
        store = self.store
        rows = [r for r in [await store.run(self.hass, store.occurrence, i) for i in sorted({op.id for op in ops})] if r]
        recs = [r for r in [await store.run(self.hass, store.record, i) for i in sorted({op.id for op in records})] if r]
        payload = {
            "occurrences": rows,
            "markers": [{"prefix": prefix, "t": t, "kind": kind, "text": text} for t, kind, text in markers],
            "records": recs,
            "snapshots": [s[0] for s in snapshots],
        }
        for listener in list(self.listeners):
            try:
                listener(prefix, payload)
            except Exception:
                _LOGGER.exception("CellKeeper listener failed")

    @callback
    def async_listen(self, listener: Listener) -> Callable[[], None]:
        self.listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self.listeners:
                self.listeners.remove(listener)

        return remove

    # ---- backups ----

    async def async_pause(self) -> None:
        """Before a backup: everything written is in the main database file, and nothing more is
        written until async_resume."""
        async with self._write_lock:
            self._paused = True
            await self.store.run(self.hass, self.store.checkpoint)

    async def async_resume(self) -> None:
        # Writes that arrive while the held ones go out queue behind them, in order.
        while self._pending:
            await self._write_now(*self._pending.pop(0))
        self._paused = False

    # ---- stopping ----

    async def async_close(self) -> None:
        """Stop the packs and close the database (once: at HA's stop, or when unloaded)."""
        if self.closed:
            return
        self.closed = True
        for pack in self.packs.values():
            pack.async_stop()
        self.packs.clear()
        await self.async_resume()
        async with self._write_lock:
            await self.store.run(self.hass, self.store.close)
        await self.hass.async_add_executor_job(self.store.shutdown)
