"""Logging one pack: catching up from the recorder, then following its event sensors live."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .discovery import PackInfo
from .tracker import EventTracker, Op, parse_state

if TYPE_CHECKING:
    from .runtime import Runtime

_LOGGER = logging.getLogger(__name__)


class PackLogger:
    """One pack's event trackers, fed from the recorder's history and then from live states."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime, info: PackInfo) -> None:
        self.hass = hass
        self.runtime = runtime
        self.info = info
        self.trackers: dict[str, EventTracker] = {}
        self._unsub: Any = None
        self._live = False
        self._buffer: list[State] = []
        # Whether the HMI is connected, from its presence sensor.
        self._offline_since: float | None = None
        self._presence_known = False

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
        presence = saved.get("_presence")
        if presence:
            self._offline_since = presence.get("offline_since")
            self._presence_known = True

        # Listen first and hold live states until the catch-up is done, so none fall between.
        self._unsub = async_track_state_change_event(self.hass, list(self.info.events), self._on_state)
        synced = pack.get("synced_until") if pack else None
        await self._catch_up(synced, now)
        self._live = True
        buffered, self._buffer = self._buffer, []
        for state in buffered:
            self._feed_live(state)

    @callback
    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

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

        states = await recorder.async_add_executor_job(
            lambda: history.get_significant_states(
                self.hass,
                start,
                None,
                list(self.info.events),
                include_start_time_state=True,
                significant_changes_only=False,
                no_attributes=True,
            )
        )
        start_ts = start.timestamp()
        points: list[tuple[float, str, str]] = []
        for entity_id, entity_states in states.items():
            known = (key := self.info.events.get(entity_id)) in self.trackers and self.trackers[key].prev is not None
            for s in entity_states:
                if isinstance(s, State):
                    t = s.last_changed.timestamp()
                else:
                    t = dt_util.parse_datetime(s["last_changed"]).timestamp()
                # The state at the start is one a tracker has already seen, from before any gap:
                # only a tracker that's new needs it.
                if known and abs(t - start_ts) < 0.0005:
                    continue
                points.append((t, entity_id, s.state if isinstance(s, State) else s["state"]))
        points.sort()

        ops: list[Op] = []
        for t, entity_id, state in points:
            ops += self._feed(t, entity_id, state, markers)
        await self.runtime.async_write(
            self.prefix,
            ops,
            markers,
            {**{k: tr.snapshot() for k, tr in self.trackers.items()}, "_presence": self._presence_snapshot()},
            now.timestamp(),
        )
        _LOGGER.debug("%s: caught up %s states from %s, %s changes", self.prefix, len(points), start, len(ops))

    # ---- live states ----

    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        if state is None:
            return
        if not self._live:
            self._buffer.append(state)
            return
        self._feed_live(state)

    @callback
    def _feed_live(self, state: State) -> None:
        markers: list[tuple[float, str, str | None]] = []
        t = state.last_changed.timestamp()
        ops = self._feed(t, state.entity_id, state.state, markers)
        key = self.info.events.get(state.entity_id)
        trackers = {key: self.trackers[key].snapshot()} if key in self.trackers else {}
        if markers:
            trackers["_presence"] = self._presence_snapshot()
        if not ops and not markers and not trackers:
            return
        self.hass.async_create_task(self.runtime.async_write(self.prefix, ops, markers, trackers, t))

    # ---- shared ----

    def _feed(self, t: float, entity_id: str, state: str, markers: list[tuple[float, str, str | None]]) -> list[Op]:
        if entity_id == self.info.presence_entity:
            self._presence(t, state, markers)
        key = self.info.events.get(entity_id)
        if key is None:
            return []
        return self.trackers[key].feed(t, state)

    def _presence(self, t: float, state: str, markers: list[tuple[float, str, str | None]]) -> None:
        """HMI offline and back online, from the presence sensor going unavailable."""
        if parse_state(state) is None:
            if self._offline_since is None and self._presence_known:
                self._offline_since = t
                markers.append((t, "hmi_offline", None))
        else:
            if self._offline_since is not None and t > self._offline_since:
                markers.append((t, "hmi_online", None))
                self._offline_since = None
            self._presence_known = True

    def _presence_snapshot(self) -> dict[str, Any]:
        return {"offline_since": self._offline_since}
