"""Logging one pack: catching up from the recorder, then following its sensors live.

Event sensors become occurrences (tracker.py), the state sensors and settings become records
(records.py), and each warning or worse gets a snapshot of the minute around it (snapshot.py).
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import RecorderRuns
from homeassistant.components.recorder.models import process_timestamp
from homeassistant.components.recorder.util import session_scope
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Context, Event, EventStateChangedData, HassJob, HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .discovery import PackInfo
from .records import RecordOp, RecordTracker
from .snapshot import AFTER_S, BEFORE_S, RECORDER_LAG_S, SERIES, build
from .tracker import EventTracker, Op, parse_state, rank
from .values import cells_summary, series, states_at, to_si, unit_of

if TYPE_CHECKING:
    from .runtime import Runtime

_LOGGER = logging.getLogger(__name__)

# The HMI's entities are unavailable for a moment while Home Assistant starts. An outage that
# begins this soon after a start and lasts no longer is Home Assistant's, not the HMI's.
HA_START_S = 120

# The state sensors that make records (records.py), by role.
RECORD_ROLES = ("contactor", "system_state", "system_request", "millis", "uptime_hmi", "bms_firmware",
                "hmi_firmware", "balance_charge")
# These change every second or so: the recorder's history of them is read a piece at a time.
FAST_ROLES = ("millis", "uptime_hmi")
FAST_CHUNK_S = 6 * 3600
# The values kept with a contactor operation.
CONTACTOR_VALUES = ("current", "pack_voltage", "output_voltage", "soc")
# A number service call this recent made the setting change that follows it (an automation's,
# or a script's: a user's carries their id anyway).
CALL_S = 30
# At most this many snapshots are taken from the recorder's history on a catch-up, newest first.
CATCH_UP_SNAPSHOTS = 200


def ha_starts(hass: HomeAssistant, since: datetime) -> list[float]:
    """When Home Assistant started since `since` (the recorder's runs), oldest first."""
    with session_scope(hass=hass, read_only=True) as session:
        rows = session.query(RecorderRuns.start).filter(RecorderRuns.start >= since).all()
    return sorted(process_timestamp(r[0]).timestamp() for r in rows)


class PackLogger:
    """One pack's trackers, fed from the recorder's history and then from live states."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime, info: PackInfo) -> None:
        self.hass = hass
        self.runtime = runtime
        self.info = info
        self.trackers: dict[str, EventTracker] = {}
        self.records = RecordTracker(info.prefix, runtime.next_record_id)
        # entity_id -> records.py's source name
        self.sources: dict[str, str] = {info.roles[r]: r for r in RECORD_ROLES if r in info.roles}
        self.sources.update({e: f"setting:{key}" for e, key in info.settings.items()})
        self._unsubs: list[Any] = []
        self._live = False
        self._stopped = False
        self._records_new = False
        self._buffer: list[tuple[State, Context]] = []
        self._lock = asyncio.Lock()  # live writes go out in the order they happened
        self._calls: dict[str, float] = {}  # recent number service calls: context id -> time
        self._timers: set[Any] = set()
        self._units: dict[str, str | None] = {}
        # Whether the HMI is connected, from its presence sensor.
        self._offline_since: float | None = None
        self._offline_at_start = False  # the outage began as Home Assistant started
        self._presence_known = False
        self._starts: list[float] = []

    @property
    def prefix(self) -> str:
        return self.info.prefix

    # ---- starting and stopping ----

    async def async_start(self) -> None:
        store = self.runtime.store
        now = dt_util.utcnow()
        await store.run(self.hass, store.upsert_pack, self.prefix, self.info.name, self.info.device_id, self.info.mac, now.timestamp())
        saved = await store.run(self.hass, store.tracker_states, self.prefix)
        pack = await store.run(self.hass, store.pack, self.prefix)

        for key in self.info.events.values():
            tracker = EventTracker(self.prefix, key, self.runtime.next_id)
            if key in saved:
                tracker.restore(saved[key])
            self.trackers[key] = tracker
        # Records began with 0.15.0: the first time, they're read from the recorder's whole history
        # even when the events are already up to date.
        self._records_new = "_records" not in saved
        if records := saved.get("_records"):
            self.records.restore(records)
        presence = saved.get("_presence")
        if presence:
            self._offline_since = presence.get("offline_since")
            self._offline_at_start = bool(presence.get("at_start"))
            self._presence_known = True

        # Listen first and hold live states until the catch-up is done, so none fall between.
        self._unsubs.append(
            async_track_state_change_event(self.hass, [*self.info.events, *self.sources], self._on_state)
        )
        self._unsubs.append(self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._on_call))
        synced = pack.get("synced_until") if pack else None
        await self._catch_up(synced, now)
        self._live = True
        buffered, self._buffer = self._buffer, []
        for state, context in buffered:
            self._feed_live(state, context)

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for cancel in list(self._timers):
            cancel()
        self._timers.clear()

    async def async_sync(self, now: float) -> None:
        """Everything up to now has been seen (written every few minutes when nothing changes)."""
        async with self._lock:
            await self.runtime.async_write(self.prefix, [], [], {"_records": deepcopy(self.records.snapshot())}, now)

    # ---- the recorder's history ----

    async def _catch_up(self, synced: float | None, now: datetime) -> None:
        """Feed the trackers everything the recorder has since they last saw a state.

        The first time, that's the recorder's whole history (10 days by default). After a restart,
        it's what changed while the integration wasn't running.
        """
        recorder = get_instance(self.hass)
        oldest = now - timedelta(days=recorder.keep_days)
        # Just after `synced`: the history's state "at the start" is the last one before the start,
        # given the start's time, so starting at `synced` itself would bring back an older state
        # if one changed exactly then.
        start = oldest if synced is None else max(dt_util.utc_from_timestamp(synced + 0.001), oldest)
        markers: list[tuple[float, str, str | None]] = []
        if synced is None:
            markers.append((start.timestamp(), "logging_started", None))
        elif synced < recorder.recorder_runs_manager.recording_start.timestamp():
            # Home Assistant was down after `synced`: whatever changed then is only known to have
            # happened after it.
            for tracker in self.trackers.values():
                if tracker.prev is not None and tracker.gap_since is None:
                    tracker.gap_since = synced
            self.records.mark_gap(synced)

        try:
            self._starts = await recorder.async_add_executor_job(ha_starts, self.hass, start)
        except Exception:  # an older or newer recorder schema: just no "started" markers
            _LOGGER.debug("Couldn't read Home Assistant's start times", exc_info=True)
            self._starts = [recorder.recorder_runs_manager.recording_start.timestamp()]
        for t in self._starts:
            if t > start.timestamp():
                markers.append((t, "ha_started", None))

        start_ts = start.timestamp()
        records_ts = oldest.timestamp() if self._records_new else start_ts
        fast = {e for e, source in self.sources.items() if source in FAST_ROLES}
        slow = [e for e in self.sources if e not in fast]
        points: list[tuple[float, str, str]] = []
        for since, ids in ((start_ts, list(self.info.events)), (records_ts, slow)):
            states = await recorder.async_add_executor_job(series, self.hass, since, None, ids)
            for entity_id, entity_states in states.items():
                known = self._knows(entity_id)
                for t, state in entity_states:
                    # The state at the start is one a tracker has already seen, from before any
                    # gap: only a tracker that's new needs it.
                    if known and abs(t - since) < 0.0005:
                        continue
                    points.append((t, entity_id, state))
        points.sort()

        ops: list[Op] = []
        recs: list[RecordOp] = []
        for t, entity_id, state in points:
            o, r = self._feed(t, entity_id, state, markers)
            ops += o
            recs += r

        # The uptimes, a piece at a time: each source's states only need to be in order.
        t0, end = records_ts, now.timestamp()
        while fast and t0 < end:
            if self._stopped:  # Home Assistant is stopping: the recorder is going away
                return
            t1 = min(t0 + FAST_CHUNK_S, end)
            chunk = await recorder.async_add_executor_job(series, self.hass, t0, t1, list(fast), t0 == records_ts)
            for entity_id, entity_states in chunk.items():
                known = self._knows(entity_id)
                for t, state in entity_states:
                    if known and t0 == records_ts and abs(t - records_ts) < 0.0005:
                        continue
                    recs += self._feed_record(t, entity_id, state, None)
            t0 = t1

        await self._fill(recs, live=False)
        await self.runtime.async_write(
            self.prefix,
            ops,
            markers,
            {
                **{k: tr.snapshot() for k, tr in self.trackers.items()},
                "_presence": self._presence_snapshot(),
                "_records": deepcopy(self.records.snapshot()),
            },
            now.timestamp(),
            recs,
        )
        _LOGGER.debug("%s: caught up %s states from %s, %s changes, %s records", self.prefix, len(points), start,
                      len(ops), len(recs))

        # Snapshots of the warnings it found, from the recorder's history while it still has it;
        # the first time, of the ones already logged too.
        due = self._snapshots_due(ops)
        if self._records_new:
            store = self.runtime.store
            due += await store.run(self.hass, store.without_snapshot, self.prefix, oldest.timestamp() + BEFORE_S)
        due = sorted(set(due), key=lambda d: -d[1])[:CATCH_UP_SNAPSHOTS]
        due = [d for d in due if d[1] - BEFORE_S > oldest.timestamp()]
        if due:
            self.hass.async_create_background_task(self._catch_up_snapshots(due), f"cellkeeper {self.prefix} snapshots")

    def _knows(self, entity_id: str) -> bool:
        if (key := self.info.events.get(entity_id)) in self.trackers:
            return self.trackers[key].prev is not None
        if source := self.sources.get(entity_id):
            return self.records.knows(source)
        return False

    # ---- live states ----

    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        if state is None:
            return
        if not self._live:
            self._buffer.append((state, event.context))
            return
        self._feed_live(state, event.context)

    @callback
    def _on_call(self, event: Event) -> None:
        """A number service call: the setting change it makes is Home Assistant's (an automation's)."""
        if event.data.get("domain") != "number":
            return
        now = time.time()
        self._calls = {c: t for c, t in self._calls.items() if now - t < CALL_S}
        self._calls[event.context.id] = now

    @callback
    def _feed_live(self, state: State, context: Context) -> None:
        markers: list[tuple[float, str, str | None]] = []
        t = state.last_changed.timestamp()
        by = self._who(context) if state.entity_id in self.info.settings else None
        ops, recs = self._feed(t, state.entity_id, state.state, markers, by)
        key = self.info.events.get(state.entity_id)
        trackers = {key: self.trackers[key].snapshot()} if key in self.trackers else {}
        if markers:
            trackers["_presence"] = self._presence_snapshot()
        if not ops and not markers and not trackers and not recs:
            return
        trackers["_records"] = deepcopy(self.records.snapshot())
        self.hass.async_create_task(self._write_live(ops, markers, trackers, t, recs))

    async def _write_live(self, ops: list[Op], markers: list[tuple[float, str, str | None]],
                          trackers: dict[str, Any], t: float, recs: list[RecordOp]) -> None:
        async with self._lock:
            await self._name_users(recs)
            await self._fill(recs, live=True)
            await self.runtime.async_write(self.prefix, ops, markers, trackers, t, recs)
        for occurrence_id, raised_at in self._snapshots_due(ops):
            self._schedule_snapshot(occurrence_id, raised_at)

    def _who(self, context: Context) -> dict[str, Any]:
        """Who changed a setting: a user in Home Assistant, Home Assistant itself (an automation or
        a script), or the HMI (its touch screen, or its own scripts such as the balance charge)."""
        if context.user_id:
            return {"by": "user", "user_id": context.user_id}
        if context.id in self._calls or context.parent_id:
            return {"by": "automation"}
        return {"by": "hmi"}

    async def _name_users(self, recs: list[RecordOp]) -> None:
        for op in recs:
            data = op.fields.get("data") or {}
            if (user_id := data.get("user_id")) and (user := await self.hass.auth.async_get_user(user_id)):
                data["user"] = user.name

    # ---- shared ----

    def _feed(self, t: float, entity_id: str, state: str, markers: list[tuple[float, str, str | None]],
              by: dict[str, Any] | None = None) -> tuple[list[Op], list[RecordOp]]:
        if entity_id == self.info.presence_entity:
            self._presence(t, state, markers)
        recs = self._feed_record(t, entity_id, state, by) if entity_id in self.sources else []
        key = self.info.events.get(entity_id)
        if key is None:
            return [], recs
        return self.trackers[key].feed(t, state), recs

    def _feed_record(self, t: float, entity_id: str, state: str, by: dict[str, Any] | None) -> list[RecordOp]:
        source = self.sources[entity_id]
        value: Any = state
        if source in FAST_ROLES:
            # Uptimes in seconds, whatever unit they're shown in.
            value = to_si(state, self._unit(entity_id))
        return self.records.feed(t, source, value, by)

    def _unit(self, entity_id: str) -> str | None:
        if entity_id not in self._units:
            self._units[entity_id] = unit_of(self.hass, entity_id)
        return self._units[entity_id]

    def _presence(self, t: float, state: str, markers: list[tuple[float, str, str | None]]) -> None:
        """HMI offline and back online, from the presence sensor going unavailable.

        An outage that starts as Home Assistant starts is only the HMI's if it goes on longer
        than Home Assistant takes to connect: its "offline" marker is added when it's over.
        """
        if parse_state(state) is None:
            if self._offline_since is None and self._presence_known:
                self._offline_since = t
                self._offline_at_start = any(0 <= t - s <= HA_START_S for s in self._starts)
                if not self._offline_at_start:
                    markers.append((t, "hmi_offline", None))
        else:
            if self._offline_since is not None and t >= self._offline_since:
                if not self._offline_at_start:
                    markers.append((t, "hmi_online", None))
                elif t - self._offline_since > HA_START_S:
                    markers.append((self._offline_since, "hmi_offline", None))
                    markers.append((t, "hmi_online", None))
                self._offline_since = None
                self._offline_at_start = False
            self._presence_known = True

    def _presence_snapshot(self) -> dict[str, Any]:
        return {"offline_since": self._offline_since, "at_start": self._offline_at_start}

    # ---- values at a record's time ----

    async def _fill(self, recs: list[RecordOp], live: bool) -> None:
        """Fill in what the records asked for: the pack's values at their time."""
        for op in recs:
            if not op.needs:
                continue
            data = op.fields.setdefault("data", {})
            for key, what, when in op.needs:
                try:
                    data[key] = await self._lookup(what, when, live)
                except Exception:
                    _LOGGER.debug("%s: couldn't read the %s for a record", self.prefix, what, exc_info=True)

    async def _lookup(self, what: str, when: Any, live: bool) -> Any:
        if what == "values":
            ids = {r: self.info.roles[r] for r in CONTACTOR_VALUES if r in self.info.roles}
            states = await self._states_at(when, list(ids.values()), live)
            return {r: _round(to_si(states.get(e), self._unit(e))) for r, e in ids.items()}
        if what == "cells":
            states = await self._states_at(when, list(self.info.cells.values()), live)
            return cells_summary({n: to_si(states.get(e), self._unit(e)) for n, e in self.info.cells.items()})
        if what == "balanced":
            # How long each cell was bled for: its balancing time counter's rise over the charge.
            t0, t1 = when
            ids = list(self.info.balancing.values())
            before = await self._states_at(t0, ids, False)
            after = await self._states_at(t1, ids, live)
            out = {}
            for n, e in self.info.balancing.items():
                a, b = to_si(before.get(e), self._unit(e)), to_si(after.get(e), self._unit(e))
                if a is not None and b is not None and b > a:
                    out[str(n)] = round(b - a)
            return out
        raise ValueError(what)

    async def _states_at(self, t: float, entity_ids: list[str], live: bool) -> dict[str, str]:
        """The entities' states at time t: as they are now for a live record, else the recorder's."""
        if live and abs(time.time() - t) < 5:
            return {e: s.state for e in entity_ids if (s := self.hass.states.get(e))}
        return await get_instance(self.hass).async_add_executor_job(states_at, self.hass, t, entity_ids)

    # ---- event snapshots ----

    @staticmethod
    def _snapshots_due(ops: list[Op]) -> list[tuple[int, float]]:
        """The occurrences raised just now, at a known time, of a warning or worse."""
        return [
            (op.id, op.fields["raised_at"])
            for op in ops
            if op.kind == "insert"
            and op.fields.get("kind") == "raised"
            and op.fields.get("time_known")
            and rank(op.fields.get("start_level")) >= rank("WARNING")
        ]

    def _schedule_snapshot(self, occurrence_id: int, t: float) -> None:
        """Take it once the recorder has the seconds after it too."""
        if self._stopped:
            return
        delay = t + AFTER_S + RECORDER_LAG_S - time.time()

        @callback
        def fire(_now: Any) -> None:
            self._timers.discard(cancel)
            if not self._stopped:
                self.hass.async_create_task(self._snapshot(occurrence_id, t))

        cancel = async_call_later(self.hass, max(delay, 0), HassJob(fire, cancel_on_shutdown=True))
        self._timers.add(cancel)

    async def _catch_up_snapshots(self, due: list[tuple[int, float]]) -> None:
        for occurrence_id, t in due:
            if self._stopped:
                return
            if t + AFTER_S + RECORDER_LAG_S > time.time():
                self._schedule_snapshot(occurrence_id, t)
            else:
                await self._snapshot(occurrence_id, t)

    async def _snapshot(self, occurrence_id: int, t: float) -> None:
        roles = {self.info.roles[r]: r for r in SERIES if r in self.info.roles}
        ids = [*roles, *self.info.cells.values()]
        units = {e: self._unit(e) for e in ids}
        try:
            # From a second early: the state in force is the last one strictly before the start.
            pts = await get_instance(self.hass).async_add_executor_job(
                series, self.hass, t - BEFORE_S - 1, t + AFTER_S + 0.001, ids
            )
        except Exception:
            _LOGGER.debug("%s: couldn't read a snapshot", self.prefix, exc_info=True)
            return
        values = {r: [(when, to_si(s, units[e])) for when, s in pts.get(e, [])] for e, r in roles.items()}
        cells = {n: [(when, to_si(s, units[e])) for when, s in pts.get(e, [])] for n, e in self.info.cells.items()}
        if not any(values.values()) and not any(cells.values()):
            return
        data = build(t, values, cells)
        if not self._stopped:
            await self.runtime.async_write(self.prefix, [], [], {}, None, snapshots=[(occurrence_id, t, data)])


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 4)
