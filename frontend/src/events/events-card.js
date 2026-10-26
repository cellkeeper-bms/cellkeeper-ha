import { html, css, nothing } from "lit";
import { PackCard, baseStyles, fmt, fmtDuration } from "../overview/base.js";
import { define } from "../context.js";
import { unsafeCSS } from "lit";
import { LEVELS, LEVEL_COLORS, LEVEL_ICONS, PREVIOUS_COLOR, packEvents, escalationText, levelName, parseState } from "./model.js";

// custom:cellkeeper-events-card
//   prefix: ck0
//   mode: status | log | catalog
//   days: 10          (log) how far back to read Home Assistant's history
//   range: 90d        (log, with the CellKeeper integration) 10d, 90d, 1y or all to start with
//
// status:  what is wrong right now, how bad, since when and what happens next.
// log:     every event raised, escalated and cleared, with BMS starts and HMI outages, newest
//          first. With the CellKeeper integration installed it comes from the integration's own
//          database, which keeps it for years; without it, from Home Assistant's history of the
//          event sensors (10 days by default).
// catalog: every event the firmware knows: what it means, how it starts and escalates, and its
//          count since the BMS started.

const SEVERE = ["WARNING", "CRITICAL", "FATAL"];
const PREVIOUS_COLOR_CSS = unsafeCSS(PREVIOUS_COLOR);
const rank = (l) => LEVELS.indexOf(l);

// "3 h 10 min"; "" under a minute
function span(ms) {
  return ms < 60000 ? "" : fmtDuration(ms / 3600000);
}

function ago(hass, iso) {
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 60000) return "just now";
  return `${fmtDuration(ms / 3600000)} ago`;
}

function clock(hass, t) {
  return new Date(t).toLocaleTimeString(hass.locale?.language, { hour: "2-digit", minute: "2-digit" });
}

function dayLabel(hass, t) {
  const d = new Date(t);
  const today = new Date();
  const y = new Date(today.getTime() - 86400000);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString(hass.locale?.language, { weekday: "long", day: "numeric", month: "short" });
}

// ---- log from history ----

// Turn each sensor's state history into log entries.
function buildLog(history, events, bootKey) {
  const byId = Object.fromEntries(events.map((e) => [e.id, e]));
  const rows = [];
  for (const [id, points] of Object.entries(history)) {
    const e = byId[id];
    if (!e) continue;
    let prev; // last known (available) parsed state
    let raisedAt;
    let offlineSince;
    for (const p of points) {
      const t = (p.lu ?? p.lc) * 1000;
      const cur = parseState(p.s);
      if (cur.unavailable) {
        if (e.key === bootKey && offlineSince === undefined && prev) offlineSince = t;
        continue;
      }
      if (e.key === bootKey && offlineSince !== undefined) {
        const off = span(t - offlineSince);
        rows.push({ t, kind: "hmi", text: off ? `HMI back online after ${off}` : "HMI reconnected" });
        offlineSince = undefined;
      }
      if (prev) {
        if (cur.active && (!prev.active || rank(cur.level) > rank(prev.level))) {
          const escalated = prev.active && rank(cur.level) > rank(prev.level);
          if (e.key === bootKey && !escalated) rows.push({ t, kind: "boot", text: "BMS started" });
          else
            rows.push({
              t, e, level: cur.level, count: cur.count,
              text: escalated ? `Escalated to ${cur.level.toLowerCase()}` : "Raised",
            });
          raisedAt = t;
        } else if (cur.active && prev.active && cur.count > prev.count) {
          rows.push({ t, e, level: cur.level, count: cur.count, again: true, text: "Happened again" });
        } else if (!cur.active && prev.active) {
          // Back to plain INACTIVE means the BMS restarted and forgot it, not that it cleared.
          const reset = cur.count === 0;
          const took = raisedAt ? span(t - raisedAt) : "";
          rows.push({
            t, e, level: prev.level, count: prev.count, cleared: true, reset,
            text: reset ? "Gone after a BMS restart" : took ? `Cleared after ${took}` : "Cleared within a minute",
          });
          raisedAt = undefined;
        } else if (!cur.active && !prev.active && cur.count > prev.count) {
          rows.push({ t, e, level: e.level, count: cur.count, cleared: true, text: "Raised and cleared between readings" });
        }
      }
      prev = cur;
    }
    if (e.key === bootKey && offlineSince !== undefined) rows.push({ t: offlineSince, kind: "hmi", text: "HMI offline" });
  }
  rows.sort((a, b) => b.t - a.t);
  // Fold a burst of repeats of one event (within 10 min) into one row.
  const out = [];
  for (const r of rows) {
    const last = out[out.length - 1];
    if (last && r.again && last.again && last.e === r.e && last.first - r.t < 600000) {
      last.first = r.t;
      last.n += 1;
      last.from = r.count;
      last.text = `Happened ${last.n} more times`;
      continue;
    }
    out.push(r.again ? { ...r, first: r.t, n: 1, from: r.count } : r);
  }
  return out;
}

// ---- log from the CellKeeper integration ----

// How far back the log reads from the integration, in days (0: everything it has).
const STORE_RANGES = { "10d": 10, "90d": 90, "1y": 365, all: 0 };
const STORE_RANGE_NAMES = { "10d": "10 d", "90d": "90 d", "1y": "1 y", all: "All" };
const STORE_RANGE_TEXT = { "10d": "in the last 10 days", "90d": "in the last 90 days", "1y": "in the last year", all: "since the log began" };
const RANGE_KEY = "cellkeeper-events";

function rememberedRange() {
  try {
    return JSON.parse(localStorage.getItem(RANGE_KEY))?.range;
  } catch {
    return undefined;
  }
}

function rememberRange(range) {
  try {
    localStorage.setItem(RANGE_KEY, JSON.stringify({ range }));
  } catch {
    // Not remembered (private window, storage blocked).
  }
}

// The integration's occurrences and markers as the same log entries buildLog makes. A row whose
// time isn't known (it happened while the HMI or Home Assistant was away) carries `after`: it
// happened between then and when it was seen (null: before the log began).
function storeLog(result, events, bootKey) {
  const byKey = Object.fromEntries(events.map((e) => [e.key, e]));
  const rows = [];
  for (const o of result.occurrences) {
    const e = byKey[o.event] ?? { key: o.event, name: o.event, level: o.start_level ?? "INFO", desc: "" };
    const t = o.raised_at * 1000;
    const after = o.time_known ? undefined : o.raised_after ? o.raised_after * 1000 : null;
    const level = o.start_level ?? o.level ?? e.level;
    if (e.key === bootKey && o.kind === "raised") {
      rows.push({ t, kind: "boot", text: "BMS started", after });
      continue;
    }
    if (o.kind === "again") {
      rows.push({
        t: o.last_at * 1000, e, level, count: o.count_to, again: true, after, n: o.n, from: o.count_from + 1,
        text: o.n > 1 ? `Happened ${o.n} more times` : "Happened again",
      });
      continue;
    }
    if (o.kind === "between") {
      rows.push({
        t: o.last_at * 1000, e, level, count: o.count_to, cleared: true, after, n: o.n, from: o.count_from + 1,
        text: o.n > 1 ? `Raised and cleared ${o.n} times between readings` : "Raised and cleared between readings",
      });
      continue;
    }
    rows.push({
      t, e, level, count: o.count_to, after,
      text: o.kind === "active" ? "Already active when the log began" : "Raised",
    });
    if (o.escalated_at) {
      rows.push({ t: o.escalated_at * 1000, e, level: o.level, count: o.count_to, text: `Escalated to ${o.level.toLowerCase()}` });
    }
    if (o.cleared_at) {
      const reset = o.clear === "bms_restart";
      const took = o.time_known && !o.cleared_after ? span((o.cleared_at - o.raised_at) * 1000) : "";
      rows.push({
        t: o.cleared_at * 1000, e, level: o.level, count: o.count_to, cleared: true, reset,
        after: o.cleared_after ? o.cleared_after * 1000 : undefined,
        text: reset ? "Gone after a BMS restart" : took ? `Cleared after ${took}` : o.time_known ? "Cleared within a minute" : "Cleared",
      });
    }
  }
  // Markers, oldest first so each "back online" finds its "offline".
  let offline;
  for (const m of [...result.markers].sort((a, b) => a.t - b.t)) {
    const t = m.t * 1000;
    if (m.kind === "hmi_offline") offline = t;
    else if (m.kind === "hmi_online") {
      const off = offline !== undefined ? span(t - offline) : "";
      rows.push({ t, kind: "hmi", text: off ? `HMI back online after ${off}` : "HMI reconnected" });
      offline = undefined;
    } else if (m.kind === "ha_started") rows.push({ t, kind: "ha", text: "Home Assistant started" });
    else if (m.kind === "logging_started") rows.push({ t, kind: "start", text: "The CellKeeper log begins" });
  }
  if (offline !== undefined) rows.push({ t: offline, kind: "hmi", text: "HMI offline" });
  return rows.sort((a, b) => b.t - a.t);
}

const historyCache = new Map(); // prefix (and range) -> { at, promise }

class CellKeeperEventsCard extends PackCard {
  static properties = {
    ...PackCard.properties,
    _log: { state: true },
    _showInfo: { state: true },
    _limit: { state: true },
    _filter: { state: true },
    _open: { state: true },
    _areas: { state: true },
    _range: { state: true },
  };

  constructor() {
    super();
    this._showInfo = false;
    this._limit = 40;
    this._filter = "";
    this._open = undefined;
    this._areas = {};
  }

  setConfig(config) {
    super.setConfig({ mode: "status", days: 10, range: "90d", ...config });
    const saved = rememberedRange();
    this._range = saved in STORE_RANGES ? saved : this._config.range in STORE_RANGES ? this._config.range : "90d";
  }

  // Whether the CellKeeper integration keeps this Home Assistant's event history.
  get _stored() {
    return !!this.hass?.config?.components?.includes("cellkeeper");
  }

  _setRange(range) {
    this._range = range;
    rememberRange(range);
    this._log = undefined;
    this._loadLog(false);
  }

  watched() {
    const keys = this.hass ? packEvents(this.hass, this.prefix).map((e) => e.id) : [];
    return [...keys, "uptime_pico", "system_state_text", "contactor_state_text"];
  }

  updated(changed) {
    if (this._config.mode !== "log" || !changed.has("hass") || !this.hass) return;
    // Re-read the history shortly after any event sensor changes.
    const sig = packEvents(this.hass, this.prefix).map((e) => e.state).join("|");
    if (sig !== this._sig) {
      const first = this._sig === undefined;
      this._sig = sig;
      clearTimeout(this._reload);
      this._reload = setTimeout(() => this._loadLog(!first), first ? 0 : 3000);
    }
  }

  async _loadLog(force) {
    const events = packEvents(this.hass, this.prefix);
    const bootKey = events.find((e) => /_boot_normal$/.test(e.key))?.key;
    if (this._stored) {
      const key = `${this.prefix} ${this._range}`;
      try {
        const days = STORE_RANGES[this._range];
        const hit = historyCache.get(key);
        let promise = hit?.promise;
        if (force || !hit || Date.now() - hit.at > 60000) {
          const msg = { type: "cellkeeper/events/list", prefix: this.prefix, limit: 5000 };
          if (days) msg.start = Date.now() / 1000 - days * 86400;
          promise = this.hass.callWS(msg);
          historyCache.set(key, { at: Date.now(), promise });
        }
        const result = await promise;
        const log = storeLog(result, events, bootKey);
        log.more = result.more;
        this._log = log;
        return;
      } catch (err) {
        // The integration isn't running (or doesn't know this pack): Home Assistant's history.
        historyCache.delete(key);
      }
    }
    const hit = historyCache.get(this.prefix);
    let promise = hit?.promise;
    if (force || !hit || Date.now() - hit.at > 60000) {
      const end = new Date();
      const start = new Date(end.getTime() - this._config.days * 86400000);
      promise = this.hass.callWS({
        type: "history/history_during_period",
        start_time: start.toISOString(),
        end_time: end.toISOString(),
        entity_ids: events.map((e) => e.id),
        minimal_response: true,
        no_attributes: true,
        significant_changes_only: false,
      });
      historyCache.set(this.prefix, { at: Date.now(), promise });
    }
    try {
      this._log = buildLog(await promise, events, bootKey);
    } catch (err) {
      this._log = { error: err.message ?? String(err) };
    }
  }

  render() {
    if (!this.hass || !this._config) return nothing;
    const events = packEvents(this.hass, this.prefix);
    if (!events.length) return nothing;
    const body = { status: () => this._status(events), log: () => this._logView(events), catalog: () => this._catalog(events) }[
      this._config.mode
    ]();
    return html`<ha-card>${body}</ha-card>`;
  }

  // ---- status ----

  _status(events) {
    const active = events.filter((e) => e.active);
    const severe = active.filter((e) => SEVERE.includes(e.level)).sort((a, b) => rank(b.level) - rank(a.level));
    const info = active.filter((e) => e.level === "INFO");
    const earlier = events.filter((e) => !e.active && e.count > 0).sort((a, b) => b.count - a.count);
    const worst = severe[0]?.level;
    const uptime = this.numIn("uptime_pico", "s");
    const contactors = this.str("contactor_state_text");
    const headline = !worst
      ? "All clear"
      : worst === "FATAL"
      ? "Fatal fault"
      : `${severe.length} ${worst === "CRITICAL" ? "critical" : "warning"}${severe.length > 1 ? "s" : ""} active`;
    const sub = !worst
      ? `No warnings, critical or fatal events${uptime ? ` since the BMS started ${fmtDuration(uptime / 3600)} ago` : ""}.`
      : worst === "FATAL"
      ? `The BMS has opened the contactors${contactors ? ` (now ${contactors.toLowerCase()})` : ""}. Fatal events stay until the BMS restarts; fix the cause first.`
      : worst === "CRITICAL"
      ? "Critical events turn fatal and open the contactors if they stay active."
      : "Warnings don't stop the pack, but some turn fatal if they keep happening.";
    const color = worst ? LEVEL_COLORS[worst] : "var(--ck-good)";
    return html`
      <div class="status" style="--c:${color}">
        <div class="hero">
          <div class="badge"><ha-icon icon=${worst ? LEVEL_ICONS[worst] : "mdi:check-circle"}></ha-icon></div>
          <div>
            <div class="headline">${headline}</div>
            <div class="sub">${sub}</div>
          </div>
        </div>
        ${severe.length ? html`<div class="active">${severe.map((e) => this._activeRow(e))}</div>` : ""}
        ${info.length || earlier.length
          ? html`<div class="chips">
              ${info.map(
                (e) => html`<span class="chip" style="--c:${LEVEL_COLORS.INFO}" title=${e.desc} @click=${() => this.moreInfo(e.id)}
                  ><ha-icon icon="mdi:information-outline"></ha-icon>${e.name}</span
                >`
              )}
              ${earlier.length
                ? html`<span class="chips-label">Earlier since the BMS started:</span>
                    ${earlier.map(
                      (e) => html`<span class="chip" style="--c:${PREVIOUS_COLOR}" title=${e.desc} @click=${() => this.moreInfo(e.id)}
                        ><ha-icon icon="mdi:history"></ha-icon>${e.name}${e.count > 1 ? ` ×${e.count}` : ""}</span
                      >`
                    )}`
                : ""}
            </div>`
          : ""}
      </div>
    `;
  }

  _activeRow(e) {
    const c = LEVEL_COLORS[e.level];
    const left = e.escalate?.count && e.level === "WARNING" ? e.escalate.count - e.count : undefined;
    return html`
      <div class="arow clickable" style="--c:${c}" @click=${() => this.moreInfo(e.id)}>
        <ha-icon icon=${LEVEL_ICONS[e.level]}></ha-icon>
        <div class="atext">
          <div class="aname">${e.name}<span class="lvl">${levelName(e.level)}</span>${e.count > 1 ? html`<span class="cnt">×${e.count}</span>` : ""}</div>
          <div class="adesc">${e.desc}</div>
          <div class="aesc">
            ${e.level === "FATAL" ? "Stays until the BMS restarts." : escalationText(e)}
            ${left !== undefined
              ? html`<span class="prog"><i style="width:${Math.min(100, (e.count / e.escalate.count) * 100)}%"></i></span>${left} to go`
              : ""}
          </div>
        </div>
        <div class="since" title=${new Date(e.since).toLocaleString()}>${ago(this.hass, e.since)}</div>
      </div>
    `;
  }

  // ---- log ----

  _logView(events) {
    const log = this._log;
    const stored = this._stored;
    const range = stored ? STORE_RANGE_TEXT[this._range] : `in the last ${this._config.days} days`;
    const head = html`
      <div class="head">
        <ha-icon icon="mdi:timeline-text-outline"></ha-icon><span class="title">Event log</span>
        ${stored
          ? html`<div class="seg" title="Kept by the CellKeeper integration">
              ${Object.keys(STORE_RANGES).map(
                (k) => html`<button class=${k === this._range ? "on" : ""} @click=${() => this._setRange(k)}>${STORE_RANGE_NAMES[k]}</button>`
              )}
            </div>`
          : html`<span class="dim small">last ${this._config.days} days</span>`}
        <span class="spacer"></span>
        <button class="pill ${this._showInfo ? "on" : ""}" @click=${() => (this._showInfo = !this._showInfo)}>
          <ha-icon icon=${this._showInfo ? "mdi:checkbox-marked" : "mdi:checkbox-blank-outline"}></ha-icon>Info events
        </button>
      </div>
    `;
    if (!log) return html`<div class="wrap">${head}<div class="empty dim">Reading history…</div></div>`;
    if (log.error) return html`<div class="wrap">${head}<div class="empty dim">Couldn't read the history: ${log.error}</div></div>`;
    const rows = log.filter((r) => r.kind || this._showInfo || r.level !== "INFO");
    const shown = rows.slice(0, this._limit);
    let lastDay;
    return html`
      <div class="wrap">
        ${head}
        ${!rows.length
          ? html`<div class="empty dim">Nothing ${this._showInfo ? "" : "but info events "}${range}.</div>`
          : html`<div class="log">
              ${shown.map((r) => {
                const day = dayLabel(this.hass, r.t);
                const header = day !== lastDay ? html`<div class="day">${day}</div>` : "";
                lastDay = day;
                return html`${header}${this._logRow(r)}`;
              })}
            </div>`}
        ${rows.length > shown.length
          ? html`<button class="more" @click=${() => (this._limit += 40)}>Show ${Math.min(40, rows.length - shown.length)} more</button>`
          : log.more
          ? html`<div class="empty dim small">That's the newest 5000; pick a shorter range to see older ones.</div>`
          : ""}
      </div>
    `;
  }

  _logRow(r) {
    if (r.kind) {
      const icon = { boot: "mdi:restart", hmi: "mdi:lan-disconnect", ha: "mdi:home-assistant", start: "mdi:database-clock-outline" }[r.kind];
      return html`<div class="lrow sys">
        <span class="t num">${clock(this.hass, r.t)}</span>
        <ha-icon icon=${icon}></ha-icon>
        <span class="ltext"><b>${r.text}</b><span class="dim">${this._afterText(r)}</span></span>
      </div>`;
    }
    const c = r.cleared ? "var(--ck-muted)" : LEVEL_COLORS[r.level] ?? "var(--ck-muted)";
    return html`<div class="lrow ${r.e.id ? "clickable" : ""}" style="--c:${c}" @click=${() => r.e.id && this.moreInfo(r.e.id)} title=${r.e.desc}>
      <span class="t num">${clock(this.hass, r.t)}</span>
      <ha-icon icon=${r.reset ? "mdi:restart" : r.cleared ? "mdi:check" : LEVEL_ICONS[r.level] ?? "mdi:alert-circle-outline"}></ha-icon>
      <span class="ltext"
        ><b>${r.e.name}</b> <span class="dim">${r.text}${r.cleared || !r.level ? "" : html` · ${levelName(r.level)}`}${r.n > 1 ? ` · ×${r.from}–${r.count}` : r.count > 1 ? ` · ×${r.count}` : ""}${this._afterText(r)}</span></span
      >
    </div>`;
  }

  // " · some time after 14:02" for a row seen only once the HMI or Home Assistant was back.
  _afterText(r) {
    if (r.after === undefined) return "";
    const why = "It happened while the HMI or Home Assistant was away";
    if (r.after === null) return html` · <span title=${why}>time unknown</span>`;
    const same = new Date(r.after).toDateString() === new Date(r.t).toDateString();
    const when = same
      ? clock(this.hass, r.after)
      : new Date(r.after).toLocaleString(this.hass.locale?.language, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    return html` · <span title=${why}>some time after ${when}</span>`;
  }

  // ---- catalogue ----

  _catalog(events) {
    const q = this._filter.trim().toLowerCase();
    const shown = events.filter((e) => !q || `${e.name} ${e.desc} ${e.area} ${e.level}`.toLowerCase().includes(q));
    const areas = [...new Set(shown.map((e) => e.area))];
    return html`
      <div class="wrap">
        <div class="head">
          <ha-icon icon="mdi:format-list-bulleted-type"></ha-icon><span class="title">All events</span>
          <span class="dim small">${events.length}</span>
          <span class="spacer"></span>
          <input class="search" type="search" placeholder="Search" .value=${this._filter} @input=${(ev) => (this._filter = ev.target.value)} />
        </div>
        <div class="levels">
          ${LEVELS.map(
            (l) => html`<span><i style="background:${LEVEL_COLORS[l]}"></i><b>${levelName(l)}</b> ${
              { INFO: "for the record", WARNING: "a concern; some turn fatal if repeated", CRITICAL: "turns fatal if it stays active", FATAL: "contactors open until restart" }[l]
            }</span>`
          )}
        </div>
        ${areas.map((a) => {
          const list = shown.filter((e) => e.area === a);
          const seen = list.filter((e) => e.active || e.count > 0).length;
          const open = q ? true : this._areas[a] ?? seen > 0;
          return html`
            <button class="area" @click=${() => (this._areas = { ...this._areas, [a]: !open })}>
              <ha-icon icon=${open ? "mdi:chevron-down" : "mdi:chevron-right"}></ha-icon>${a}
              <span class="dim">${list.length}${seen ? html` · <b>${seen} seen</b>` : ""}</span>
            </button>
            ${open ? list.map((e) => this._catRow(e)) : ""}
          `;
        })}
        ${!shown.length ? html`<div class="empty dim">No event matches “${this._filter}”.</div>` : ""}
      </div>
    `;
  }

  _catRow(e) {
    const open = this._open === e.id;
    const now = e.active
      ? html`<span class="state" style="--c:${LEVEL_COLORS[e.level]}">${levelName(e.level)}${e.count > 1 ? ` ×${e.count}` : ""}</span>`
      : e.count
      ? html`<span class="state" style="--c:${PREVIOUS_COLOR}">×${e.count}</span>`
      : e.unavailable
      ? html`<span class="state off">–</span>`
      : "";
    return html`
      <div class="crow ${open ? "open" : ""}" @click=${() => (this._open = open ? undefined : e.id)}>
        <i class="dot" style="background:${LEVEL_COLORS[e.level]}" title="Starts as ${levelName(e.level)}"></i>
        <span class="cname">${e.name}</span>
        ${now}
        <ha-icon class="chev" icon=${open ? "mdi:chevron-up" : "mdi:chevron-down"}></ha-icon>
      </div>
      ${open
        ? html`<div class="cmore">
            <div>${e.desc}</div>
            <div class="dim">Starts as ${levelName(e.level).toLowerCase()}. ${escalationText(e)}.</div>
            <div class="dim">
              ${e.active ? `Active since ${new Date(e.since).toLocaleString()}` : e.count ? `Happened ${e.count}× since the BMS started` : "Not seen since the BMS started"}
              · <a @click=${(ev) => { ev.stopPropagation(); this.moreInfo(e.id); }}>history</a>
            </div>
          </div>`
        : ""}
    `;
  }

  getGridOptions() {
    return { columns: "full", rows: "auto" };
  }

  static styles = [
    baseStyles,
    css`
      .wrap {
        padding: 14px 16px 12px;
      }
      .dim {
        color: var(--ck-muted);
      }
      .small {
        font-size: 13px;
      }
      .spacer {
        flex: 1;
      }
      button {
        font: inherit;
        color: inherit;
        cursor: pointer;
      }
      .head {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 8px;
      }
      .head > ha-icon {
        --mdc-icon-size: 20px;
        color: var(--state-icon-color, var(--primary-color));
      }
      .title {
        font-size: 16px;
        font-weight: 500;
      }

      /* status */
      .status {
        padding: 18px 20px 16px;
        background: linear-gradient(90deg, color-mix(in srgb, var(--c) 14%, transparent), transparent 60%);
      }
      .hero {
        display: flex;
        align-items: center;
        gap: 16px;
      }
      .badge {
        flex: none;
        display: grid;
        place-items: center;
        width: 52px;
        height: 52px;
        border-radius: 50%;
        background: color-mix(in srgb, var(--c) 20%, transparent);
        color: var(--c);
      }
      .badge ha-icon {
        --mdc-icon-size: 30px;
      }
      .headline {
        font-size: 24px;
        line-height: 1.2;
        color: var(--c);
      }
      .sub {
        margin-top: 2px;
        font-size: 14px;
        color: var(--ck-muted);
      }
      .active {
        margin-top: 14px;
        display: grid;
        gap: 6px;
      }
      .arow {
        display: flex;
        align-items: flex-start;
        gap: 12px;
        padding: 10px 12px;
        border-radius: 10px;
        border-left: 4px solid var(--c);
        background: color-mix(in srgb, var(--c) 9%, var(--card-background-color, transparent));
      }
      .arow > ha-icon {
        color: var(--c);
        margin-top: 1px;
      }
      .atext {
        flex: 1;
        min-width: 0;
      }
      .aname {
        font-size: 15px;
        font-weight: 500;
      }
      .lvl,
      .cnt {
        margin-left: 8px;
        padding: 1px 8px;
        border-radius: 10px;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.03em;
        text-transform: uppercase;
        color: var(--c);
        background: color-mix(in srgb, var(--c) 16%, transparent);
      }
      .cnt {
        text-transform: none;
        color: var(--primary-text-color);
        background: var(--ck-track);
      }
      .adesc {
        margin-top: 2px;
        font-size: 13px;
      }
      .aesc {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 8px;
        margin-top: 4px;
        font-size: 12px;
        color: var(--ck-muted);
      }
      .prog {
        position: relative;
        display: inline-block;
        width: 80px;
        height: 6px;
        border-radius: 3px;
        background: var(--ck-track);
        overflow: hidden;
      }
      .prog i {
        position: absolute;
        inset: 0 auto 0 0;
        background: var(--c);
      }
      .since {
        flex: none;
        font-size: 12px;
        color: var(--ck-muted);
        white-space: nowrap;
      }
      .chips {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
        margin-top: 14px;
      }
      .chips-label {
        margin-left: 6px;
        font-size: 12px;
        color: var(--ck-muted);
      }
      .chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 3px 10px 3px 8px;
        border-radius: 14px;
        font-size: 12px;
        font-weight: 500;
        color: var(--c);
        background: color-mix(in srgb, var(--c) 14%, transparent);
        cursor: pointer;
        white-space: nowrap;
      }
      .chip ha-icon {
        --mdc-icon-size: 14px;
      }

      /* log */
      .pill {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 3px 10px 3px 6px;
        border-radius: 14px;
        border: 1px solid var(--ck-line);
        background: transparent;
        font-size: 12px;
        color: var(--ck-muted);
      }
      .pill ha-icon {
        --mdc-icon-size: 16px;
      }
      .pill.on {
        color: var(--primary-text-color);
      }
      .seg {
        display: inline-flex;
        border: 1px solid var(--ck-line);
        border-radius: 14px;
        overflow: hidden;
      }
      .seg button {
        padding: 3px 9px;
        border: 0;
        background: transparent;
        font-size: 12px;
        color: var(--ck-muted);
        cursor: pointer;
      }
      .seg button.on {
        background: var(--ck-line);
        color: var(--primary-text-color);
      }
      .empty {
        padding: 24px 0;
        text-align: center;
        font-size: 13px;
      }
      .log {
        margin: 0 -8px;
      }
      .day {
        position: sticky;
        top: 0;
        padding: 10px 8px 4px;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--ck-muted);
        background: var(--card-background-color, var(--ha-card-background));
      }
      .lrow {
        display: grid;
        grid-template-columns: 4.8em 20px 1fr;
        align-items: start;
        gap: 8px;
        padding: 5px 8px;
        font-size: 13px;
        border-radius: 6px;
      }
      .lrow .t {
        color: var(--ck-muted);
        font-size: 12px;
        padding-top: 1px;
      }
      .lrow ha-icon {
        --mdc-icon-size: 18px;
        color: var(--c);
      }
      .lrow.sys ha-icon {
        color: var(--ck-info);
      }
      .lrow.sys {
        border-top: 1px dashed var(--ck-line);
        border-radius: 0;
      }
      .ltext b {
        font-weight: 500;
      }
      .more {
        display: block;
        margin: 8px auto 0;
        padding: 6px 16px;
        border-radius: 16px;
        border: 1px solid var(--ck-line);
        background: transparent;
        font-size: 13px;
        color: var(--primary-color);
      }

      /* catalogue */
      .search {
        width: 140px;
        padding: 6px 10px;
        border-radius: 16px;
        border: 1px solid var(--ck-line);
        background: transparent;
        color: var(--primary-text-color);
        font: inherit;
        font-size: 13px;
      }
      .levels {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
        gap: 4px 16px;
        margin: 0 0 6px;
        font-size: 12px;
        color: var(--ck-muted);
      }
      .levels span {
        display: flex;
        align-items: center;
        gap: 6px;
      }
      .levels i,
      .dot {
        flex: none;
        width: 9px;
        height: 9px;
        border-radius: 50%;
      }
      .levels b {
        color: var(--primary-text-color);
        font-weight: 500;
      }
      .area {
        display: flex;
        align-items: center;
        gap: 4px;
        width: calc(100% + 16px);
        margin: 4px -8px 0;
        padding: 8px 8px 6px 4px;
        border: 0;
        border-top: 1px solid var(--ck-line);
        border-radius: 0;
        background: transparent;
        text-align: left;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--ck-muted);
      }
      .area ha-icon {
        --mdc-icon-size: 18px;
      }
      .area .dim {
        margin-left: 6px;
        font-weight: 400;
        letter-spacing: 0;
        text-transform: none;
      }
      .area b {
        color: ${PREVIOUS_COLOR_CSS};
        font-weight: 500;
      }
      .crow {
        display: flex;
        align-items: center;
        gap: 10px;
        min-height: 34px;
        margin: 0 -8px;
        padding: 0 8px;
        border-radius: 6px;
        border-top: 1px solid var(--ck-line);
        font-size: 14px;
        cursor: pointer;
      }
      .crow:hover {
        background: rgba(127, 127, 127, 0.08);
      }
      .cname {
        flex: 1;
        min-width: 0;
      }
      .state {
        padding: 1px 8px;
        border-radius: 10px;
        font-size: 12px;
        font-weight: 500;
        color: var(--c);
        background: color-mix(in srgb, var(--c) 15%, transparent);
      }
      .state.off {
        --c: var(--ck-muted);
      }
      .chev {
        --mdc-icon-size: 18px;
        color: var(--ck-muted);
      }
      .cmore {
        display: grid;
        gap: 4px;
        margin: 0 -8px;
        padding: 2px 8px 10px 27px;
        font-size: 13px;
        line-height: 1.4;
      }
      .cmore a {
        color: var(--primary-color);
        cursor: pointer;
      }
    `,
  ];
}

define("cellkeeper-events-card", CellKeeperEventsCard);
