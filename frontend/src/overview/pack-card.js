import { html, svg, css, nothing } from "lit";
import { PackCard, baseStyles, fmt, fmtPower, fmtDuration } from "./base.js";
import { fetchDay } from "./history.js";
import { BATTERY } from "../icons.js";
import { define } from "../context.js";
import { dashboardUpdate } from "../update.js";
import { EVENT_COLORS, PREVIOUS_COLOR } from "../cards/event-grid.js";

// custom:cellkeeper-pack-card
//   prefix: ck0
//   event_path: event     view to open from the event banners/chips
//
// The Overview's hero: state of charge on the logo battery, what the pack is doing and what
// it is allowed to do (current against the live limits, and which limit is in force), the last
// 24 h of SOC and power, and anything the BMS is complaining about.

const IDLE_A = 0.5; // below this the pack counts as idle
const NO_LIMIT_A = 6000; // the BMS reports 6553.5 A for "not limiting"

// Components of the effective current limits, in the order they're named when several tie.
const CHARGE_LIMITS = [
  ["current_limit_hard_charge", "hard limit"],
  ["current_limit_temperature_charge", "temperature"],
  ["current_limit_cell_voltage_charge", "cell voltage"],
  ["current_limit_working_charge", "top of working range"],
  ["current_limit_delta_charge", "cell spread"],
  ["number:max_charge_current", "max charge setting"],
];
const DISCHARGE_LIMITS = [
  ["current_limit_hard_discharge", "hard limit"],
  ["current_limit_temperature_discharge", "temperature"],
  ["current_limit_cell_voltage_discharge", "cell voltage"],
  ["current_limit_working_discharge", "bottom of working range"],
  ["number:max_discharge_current", "max discharge setting"],
];

const STATE_COLORS = { OPERATING: "var(--ck-good)", FAULT: "var(--ck-bad)", CALIB: "var(--ck-info)", INIT: "var(--ck-info)" };
const SEVERE = ["FATAL", "CRITICAL", "WARNING"];
const LEVEL_ICONS = { FATAL: "mdi:alert-octagon", CRITICAL: "mdi:alert", WARNING: "mdi:alert-circle-outline" };

function socColor(soc) {
  if (soc === undefined) return "var(--ck-muted)";
  if (soc < 10) return "var(--ck-bad)";
  if (soc < 25) return "var(--ck-warn)";
  return "var(--ck-good)";
}

class CellKeeperPackCard extends PackCard {
  static properties = { ...PackCard.properties, _day: { state: true }, _hover: { state: true } };

  setConfig(config) {
    super.setConfig({ event_path: "event", ...config });
  }

  watched() {
    return [
      "soc", "current", "power_w", "pack_internal_voltage", "pack_output_voltage", "capacity", "cell_count",
      "inverter_can_soc", "inverter_can_voltage_limit_max", "inverter_can_voltage_limit_min",
      "inverter_can_capacity_full", "inverter_can_capacity_remaining",
      "system_state_text", "contactor_state_text", "balance_charge_status", "current_limit_effective_charge",
      "current_limit_effective_discharge", ...CHARGE_LIMITS.map(([k]) => k), ...DISCHARGE_LIMITS.map(([k]) => k),
      ...this._eventIds(),
      ...[dashboardUpdate(this.hass)?.entity_id].filter(Boolean),
    ];
  }

  _eventIds() {
    if (!this.hass) return [];
    const start = `sensor.${this.prefix}_event_`;
    return Object.keys(this.hass.states).filter((id) => id.startsWith(start) && /_(fatal|critical|warning|info)_/.test(id));
  }

  connectedCallback() {
    super.connectedCallback();
    this._timer = setInterval(() => this._loadDay(), 5 * 60 * 1000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  updated(changed) {
    if (changed.has("hass") && !this._day && !this._loading && this.hass) this._loadDay();
  }

  async _loadDay() {
    if (!this.hass) return;
    this._loading = true;
    const ids = [this.eid("soc"), this.eid("power_w")];
    const data = await fetchDay(this.hass, ids);
    this._day = { soc: data[ids[0]], power: data[ids[1]] };
    this._loading = false;
  }

  // ---- data ----

  _events() {
    const active = [];
    const previous = [];
    for (const id of this._eventIds()) {
      const state = this.hass.states[id].state;
      const name = (this.hass.states[id].attributes.friendly_name ?? id)
        .replace(/^.*?Event (Fatal|Critical|Warning|Info) /i, "")
        .trim();
      const count = state.match(/\((\d+)\)/)?.[1];
      const level = ["FATAL", "CRITICAL", "WARNING", "INFO"].find((l) => state.includes(l));
      if (level) active.push({ id, name, level, count });
      else if (state.startsWith("INACTIVE (")) previous.push({ id, name, count });
    }
    const rank = (e) => ["FATAL", "CRITICAL", "WARNING", "INFO"].indexOf(e.level);
    active.sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
    return { active, previous };
  }

  // Effective limit and the component that sets it ("" when nothing reported).
  _limit(effectiveKey, components) {
    const effective = this.numIn(effectiveKey, "A");
    if (effective === undefined) return undefined;
    const values = components.map(([k, name]) => [this.numIn(k, "A"), name]).filter(([v]) => v !== undefined);
    const match = values.find(([v]) => Math.abs(v - effective) < 0.05);
    return { value: effective, reason: match?.[1] ?? "" };
  }

  // ---- render ----

  render() {
    if (!this.hass || !this._config) return nothing;
    const soc = this.num("soc");
    const current = this.numIn("current", "A");
    const voltage = this.numIn("pack_internal_voltage", "V");
    const capacity = this.num("capacity");
    const power = this.numIn("power_w", "W") ?? (current !== undefined && voltage !== undefined ? current * voltage : undefined);
    const mode = current === undefined ? "unknown" : current > IDLE_A ? "charging" : current < -IDLE_A ? "discharging" : "idle";
    const events = this._events();
    const severe = events.active.filter((e) => SEVERE.includes(e.level));

    return html`
      <ha-card>
        ${severe.map((e) => this._banner(e))}
        <div class="wrap">
          ${this._header(events)}
          ${this._hero(soc, mode, current, power, voltage, capacity)}
          <div class="lower">
            <div class="lower-left">${this._allowed(current)} ${this._sent()}</div>
            ${this._chart()}
          </div>
          ${this._footer(events)}
        </div>
      </ha-card>
    `;
  }

  _banner(e) {
    return html`
      <div class="banner" style="--c:${EVENT_COLORS[e.level]}" @click=${() => this.navigateView(this._config.event_path)}>
        <ha-icon icon=${LEVEL_ICONS[e.level]}></ha-icon>
        <div><b>${e.level[0] + e.level.slice(1).toLowerCase()}:</b> ${e.name}${e.count > 1 ? html` <span class="dim">×${e.count}</span>` : ""}</div>
        <ha-icon class="chev" icon="mdi:chevron-right"></ha-icon>
      </div>
    `;
  }

  // The pack's HMI device name as the user named it, else the prefix.
  _deviceName() {
    const deviceId = this.hass.entities?.[this.eid("soc")]?.device_id;
    const device = deviceId && this.hass.devices?.[deviceId];
    return device?.name_by_user || device?.name || this.prefix.toUpperCase();
  }

  _header() {
    const state = this.str("system_state_text");
    const contactor = this.str("contactor_state_text");
    const cells = this.num("cell_count");
    const capacity = this.num("capacity");
    const balance = this.str("balance_charge_status");
    const update = dashboardUpdate(this.hass);
    return html`
      <div class="header">
        <div class="title">
          <ha-icon icon="cellkeeper:battery"></ha-icon>
          <span>${this._config.title ?? this._deviceName()}</span>
          <span class="dim">${[cells && `${cells}s`, capacity && `${fmt(this.hass, capacity, 0)} Ah`].filter(Boolean).join(" · ")}</span>
        </div>
        <div class="chips">
          ${state
            ? html`<span class="chip" style="--c:${STATE_COLORS[state] ?? "var(--ck-muted)"}" @click=${() => this.moreInfo("system_state_text")}>
                <i></i>${state[0] + state.slice(1).toLowerCase()}</span
              >`
            : ""}
          ${contactor
            ? html`<span
                class="chip"
                style="--c:${contactor === "CLOSED" ? "var(--ck-good)" : contactor === "OPEN" ? "var(--ck-muted)" : "var(--ck-info)"}"
                @click=${() => this.moreInfo("contactor_state_text")}
                ><ha-icon icon=${contactor === "CLOSED" ? "mdi:electric-switch-closed" : "mdi:electric-switch"}></ha-icon>Contactors
                ${contactor.toLowerCase()}</span
              >`
            : ""}
          ${update?.state === "on"
            ? html`<span class="chip" style="--c:var(--ck-info)" title="A new version of this dashboard is out"
                @click=${() => this.moreInfo(update.entity_id)}
                ><ha-icon icon="mdi:arrow-up-circle-outline"></ha-icon>Dashboard ${update.attributes.latest_version}</span
              >`
            : ""}
          ${balance && balance !== "Off"
            ? html`<span class="chip" style="--c:var(--ck-info)" @click=${() => this.moreInfo("balance_charge_status")}
                ><ha-icon icon="mdi:scale-balance"></ha-icon>${balance}</span
              >`
            : ""}
        </div>
      </div>
    `;
  }

  // The hero, drawn as the display's Home page draws it: the pack as a battery filled to its
  // state of charge, the contactors, and the inverter, joined by the line the power runs
  // along, with the power on that line. It is laid out as a grid of three columns (pack, the
  // line, inverter) whose widths follow the card's, so it holds on a phone and on a desktop.
  _hero(soc, mode, current, power, voltage, capacity) {
    const out = this.numIn("pack_output_voltage", "V");
    const contactor = this.str("contactor_state_text");
    const closed = contactor === "CLOSED";
    const precharging = /^PRECH/.test(contactor ?? "");
    const fault = this.str("system_state_text") === "FAULT";
    const moving = mode === "charging" || mode === "discharging";
    const flow = { charging: "var(--ck-charge)", discharging: "var(--ck-discharge)" }[mode] ?? "var(--ck-muted)";
    const label = fault ? "Fault" : { charging: "Charging", discharging: "Discharging", idle: "Idle", unknown: "–" }[mode];
    const [pv, pu] = fmtPower(this.hass, power === undefined ? undefined : Math.abs(power));
    let eta = "";
    if (capacity && soc !== undefined && current !== undefined && Math.abs(current) >= IDLE_A) {
      const hours = mode === "charging" ? (capacity * (100 - soc)) / 100 / current : (capacity * soc) / 100 / -current;
      if (hours < 96) eta = `${mode === "charging" ? "Full" : "Empty"} in ≈ ${fmtDuration(hours)}`;
    }
    const stored = soc !== undefined && capacity && voltage ? (capacity * soc * voltage) / 100 / 1000 : undefined;
    const ah = soc !== undefined && capacity ? (capacity * soc) / 100 : undefined;
    const hourAgo = this._valueAgo(this._day?.soc, 3600e3);
    const delta = soc !== undefined && hourAgo !== undefined ? soc - hourAgo : undefined;
    const level = Math.max(0, Math.min(100, soc ?? 0));
    const word =
      contactor === undefined ? "" : closed ? "closed" : precharging ? "precharging" : contactor === "OPEN" ? "open" : contactor.toLowerCase().replace(/_/g, " ");
    const socText = html`<b>${soc === undefined ? "–" : fmt(this.hass, soc, 0)}<span>%</span></b>`;
    const amps =
      current === undefined ? "" : `${current > 0 ? "+" : ""}${fmt(this.hass, current, Math.abs(current) < 10 ? 2 : 1)} A`;
    return html`
      <div
        class="hero ${closed && moving ? mode : ""} ${soc === undefined ? "unknown" : ""}"
        style="--flow:${flow};--far:${closed ? flow : "var(--ck-track)"};--level:${level}%;--soc:${socColor(soc)}"
      >
        <div class="end pack-end clickable" @click=${() => this.moreInfo("pack_internal_voltage")}>
          <span class="label">Pack</span><span class="num">${fmt(this.hass, voltage, 1)} V</span>
        </div>
        <div class="state label" style="color:${fault ? "var(--ck-bad)" : flow}">${label}</div>
        <div class="end inv-end clickable" @click=${() => this.moreInfo("pack_output_voltage")}>
          <span class="label">Inverter</span><span class="num">${fmt(this.hass, out, 1)} V</span>
        </div>

        <div class="batt clickable" @click=${() => this.moreInfo("soc")}>
          <div class="batt-body">
            <div class="batt-text">${socText}</div>
            <div class="batt-fill"><div class="batt-text">${socText}</div></div>
          </div>
        </div>
        <div class="power clickable" @click=${() => this.moreInfo("power_w")}>
          <span class="hero-num" style=${moving ? "" : "color:var(--ck-muted)"}>${pv}<span class="unit">${pu}</span></span>
        </div>
        <div class="wire clickable" @click=${() => this.moreInfo("contactor_state_text")}>
          <div class="seg"></div>
          <svg class="switch" viewBox="0 0 76 44" role="img" aria-label="Contactors ${word}">
            <line x1="10" y1="22" x2=${closed ? 66 : 62} y2=${closed ? 22 : 7} />
            ${precharging ? svg`<path d="M10 22 Q38 46 66 22" />` : ""}
            <circle cx="10" cy="22" r="7" />
            <circle class="far" cx="66" cy="22" r="7" />
          </svg>
          <div class="seg far"></div>
        </div>
        <div class="under">
          <div class="contactors">${word ? `Contactors ${word}` : ""}</div>
          <div class="amps num">${amps}${amps && eta ? " · " : ""}${eta}</div>
        </div>
        <div class="inv">
          <svg viewBox="0 0 60 60" aria-hidden="true"><path d="M12 30 q9 -22 18 0 t18 0" /></svg>
        </div>

        <div class="stored num">
          ${stored !== undefined ? html`<span>≈ ${fmt(this.hass, stored, 1)} kWh · ${fmt(this.hass, ah, 0)} Ah</span>` : ""}
          ${delta !== undefined
            ? html`<span class="dim"
                >${Math.abs(delta) < 0.05
                  ? "Unchanged over the last hour"
                  : html`${delta > 0 ? "+" : "−"}${fmt(this.hass, Math.abs(delta), 1)} % in the last hour`}</span
              >`
            : ""}
        </div>
      </div>
    `;
  }

  // Value `ms` ago in a [[t, v]] series (nearest point at or before), or undefined.
  _valueAgo(series, ms) {
    if (!series?.length) return undefined;
    const t = Date.now() - ms;
    let best;
    for (const [ts, v] of series) if (ts <= t) best = v;
    return best;
  }

  // The current between the limits it may run in, with what sets each limit
  _allowed(current) {
    const charge = this._limit("current_limit_effective_charge", CHARGE_LIMITS);
    const discharge = this._limit("current_limit_effective_discharge", DISCHARGE_LIMITS);
    return charge || discharge ? this._limits(current, charge, discharge) : "";
  }

  // What the BMS tells the inverter over CAN: the state of charge, the two voltage targets and
  // the capacity. The two current limits it sends are the bar above.
  _sent() {
    const soc = this.num("inverter_can_soc");
    const max = this.numIn("inverter_can_voltage_limit_max", "V");
    const min = this.numIn("inverter_can_voltage_limit_min", "V");
    const full = this.numIn("inverter_can_capacity_full", "Ah");
    const left = this.numIn("inverter_can_capacity_remaining", "Ah");
    if ([soc, max, min, full, left].every((v) => v === undefined) && !this.has("inverter_can_soc")) return "";
    const cell = (key, label, value, digits, unit) => html`
      <div class="clickable" @click=${() => this.moreInfo(key)}>
        <div class="label">${label}</div>
        <div class="mid num">${fmt(this.hass, value, digits)}<span class="unit">${unit}</span></div>
      </div>
    `;
    return html`
      <div class="sent">
        <div class="sent-title">
          <div class="label">Sent to the inverter</div>
          <div class="dim">over CAN</div>
        </div>
        ${cell("inverter_can_soc", "SOC", soc, 0, "%")} ${cell("inverter_can_voltage_limit_max", "Charge to", max, 1, "V")}
        ${cell("inverter_can_voltage_limit_min", "Discharge to", min, 1, "V")}
        ${cell("inverter_can_capacity_remaining", "Capacity left", left, 0, "Ah")}
        ${cell("inverter_can_capacity_full", "Capacity full", full, 0, "Ah")}
      </div>
    `;
  }

  // Centre-zero current bar: discharge to the left, charge to the right, the allowed band
  // between the two effective limits, and a marker at the present current.
  _limits(current, charge, discharge) {
    const lim = (l) => (l && l.value < NO_LIMIT_A ? l.value : undefined);
    const c = lim(charge);
    const d = lim(discharge);
    const hard = Math.max(this.numIn("current_limit_hard_charge", "A") ?? 0, this.numIn("current_limit_hard_discharge", "A") ?? 0);
    const scale = Math.max(hard, c ?? 0, d ?? 0, Math.abs(current ?? 0), 1) * 1.05;
    const pos = (a) => 50 + (Math.max(-scale, Math.min(scale, a)) / scale) * 50;
    const marker = current === undefined ? undefined : pos(current);
    const text = (l, word) =>
      l === undefined ? html`<b>${word}</b> unlimited` : html`<b class="num">${fmt(this.hass, l.value, l.value < 10 ? 1 : 0)} A</b> ${word}${l.reason ? html`<span class="dim"> · ${l.reason}</span>` : ""}`;
    return html`
      <div class="limits">
        <div class="bar">
          <div class="side discharge" style="left:${pos(-(d ?? scale))}%;width:${50 - pos(-(d ?? scale))}%"></div>
          <div class="side charge" style="left:50%;width:${pos(c ?? scale) - 50}%"></div>
          <div class="zero"></div>
          ${marker !== undefined ? html`<div class="marker" style="left:${marker}%"></div>` : ""}
        </div>
        <div class="limit-text">
          <span class="clickable" @click=${() => this.moreInfo("current_limit_effective_discharge")}
            >${text(discharge?.value < NO_LIMIT_A ? discharge : undefined, "discharge")}</span
          >
          <span class="clickable right" @click=${() => this.moreInfo("current_limit_effective_charge")}
            >${text(charge?.value < NO_LIMIT_A ? charge : undefined, "charge")}</span
          >
        </div>
        ${charge?.value === 0 ? html`<div class="note">Charging is blocked (${charge.reason || "limit 0 A"}).</div>` : ""}
        ${discharge?.value === 0 ? html`<div class="note">Discharging is blocked (${discharge.reason || "limit 0 A"}).</div>` : ""}
      </div>
    `;
  }

  _chart() {
    const now = Date.now();
    const t0 = now - 24 * 3600e3;
    const soc = [...(this._day?.soc ?? [])];
    const power = [...(this._day?.power ?? [])];
    const liveSoc = this.num("soc");
    const livePower = this.numIn("power_w", "W");
    if (liveSoc !== undefined) soc.push([now, liveSoc]);
    if (livePower !== undefined) power.push([now, livePower]);
    const W = 1000;
    const H = 100;
    const x = (t) => ((t - t0) / (now - t0)) * W;
    const maxP = Math.max(100, ...power.map(([, p]) => Math.abs(p)));
    const hasNeg = power.some(([, p]) => p < -1);
    const hasPos = power.some(([, p]) => p > 1);
    // Zero line in the middle when the pack both charged and discharged, else at the edge.
    const zero = hasNeg && hasPos ? H / 2 : hasNeg ? 0 : H;
    const py = (p) => (hasNeg && hasPos ? zero - (p / maxP) * (H / 2) : hasNeg ? (-p / maxP) * H : H - (p / maxP) * H);
    const area = (sign) => {
      if (power.length < 2) return "";
      const pts = power.map(([t, p]) => `${x(t).toFixed(1)},${py(sign > 0 ? Math.max(0, p) : Math.min(0, p)).toFixed(1)}`);
      return `M${x(power[0][0]).toFixed(1)},${zero} L${pts.join(" L")} L${x(power[power.length - 1][0]).toFixed(1)},${zero}Z`;
    };
    const sy = (v) => H - 3 - (v / 100) * (H - 6);
    const socLine = soc.length > 1 ? "M" + soc.map(([t, v]) => `${x(t).toFixed(1)},${sy(v).toFixed(1)}`).join(" L") : "";
    const ticks = [24, 18, 12, 6, 0].map((h) => now - h * 3600e3);
    const hover = this._hover;
    const pick = (series, t) => {
      let best;
      for (const p of series) if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
      return best?.[1];
    };
    const time = (t) => new Date(t).toLocaleTimeString(this.hass.locale?.language, { hour: "2-digit", minute: "2-digit" });
    return html`
      <div class="chart">
        <div class="chart-head">
          <span class="label">Last 24 h</span>
          <span class="legend"><i style="background:var(--primary-text-color)"></i>SOC</span>
          ${hasPos ? html`<span class="legend"><i style="background:var(--ck-charge)"></i>Charge</span>` : ""}
          ${hasNeg ? html`<span class="legend"><i style="background:var(--ck-discharge)"></i>Discharge</span>` : ""}
          <span class="dim num scale">±${fmtPower(this.hass, maxP).join(" ")}</span>
        </div>
        <div
          class="plot"
          @mousemove=${(ev) => {
            const r = ev.currentTarget.getBoundingClientRect();
            this._hover = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
          }}
          @mouseleave=${() => (this._hover = undefined)}
          @click=${() => this.moreInfo("soc")}
        >
          <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
            ${svg`
              ${[25, 50, 75].map((v) => svg`<line x1="0" x2=${W} y1=${sy(v)} y2=${sy(v)} class="grid-line"></line>`)}
              <path d=${area(1)} fill="var(--ck-charge)" fill-opacity="0.35"></path>
              <path d=${area(-1)} fill="var(--ck-discharge)" fill-opacity="0.35"></path>
              <path d=${socLine} fill="none" stroke="var(--primary-text-color)" stroke-width="2" vector-effect="non-scaling-stroke"></path>
              ${hover !== undefined ? svg`<line x1=${hover * W} x2=${hover * W} y1="0" y2=${H} class="cursor"></line>` : ""}
            `}
          </svg>
          <div class="yaxis">${[100, 50, 0].map((v) => html`<span style="top:${sy(v)}%">${v}%</span>`)}</div>
          ${!this._day ? html`<div class="loading dim">Loading history…</div>` : ""}
          ${hover !== undefined
            ? (() => {
                const t = t0 + hover * (now - t0);
                const s = pick(soc, t);
                const [p, u] = fmtPower(this.hass, pick(power, t));
                return html`<div class="tip" style="left:${hover * 100}%;${hover > 0.6 ? "transform:translateX(-100%)" : ""}">
                  <div class="dim">${time(t)}</div>
                  <div class="num">SOC ${fmt(this.hass, s, 1)} %</div>
                  <div class="num">${p} ${u}</div>
                </div>`;
              })()
            : ""}
        </div>
        <div class="ticks">${ticks.map((t, i) => html`<span>${i === 4 ? "now" : time(t)}</span>`)}</div>
      </div>
    `;
  }

  _footer({ active, previous }) {
    const info = active.filter((e) => e.level === "INFO");
    if (!info.length && !previous.length) return "";
    const go = () => this.navigateView(this._config.event_path);
    return html`
      <div class="footer">
        ${info.map(
          (e) => html`<span class="chip small" style="--c:${EVENT_COLORS.INFO}" @click=${go}><ha-icon icon="mdi:information-outline"></ha-icon>${e.name}</span>`
        )}
        ${previous.map(
          (e) => html`<span class="chip small" style="--c:${PREVIOUS_COLOR}" @click=${go} title="Was active since the last reset"
            ><ha-icon icon="mdi:history"></ha-icon>${e.name}${e.count > 1 ? ` ×${e.count}` : ""}</span
          >`
        )}
      </div>
    `;
  }

  getGridOptions() {
    return { columns: "full", rows: "auto" };
  }

  static styles = [
    baseStyles,
    css`
      .wrap {
        padding: 16px 20px 18px;
        container-type: inline-size;
      }
      .banner {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 12px 20px;
        background: color-mix(in srgb, var(--c) 18%, transparent);
        border-bottom: 1px solid color-mix(in srgb, var(--c) 45%, transparent);
        cursor: pointer;
      }
      .banner ha-icon {
        color: var(--c);
      }
      .banner div {
        flex: 1;
      }
      .banner .chev {
        color: var(--ck-muted);
      }
      .header {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 8px 16px;
        margin-bottom: 12px;
      }
      .title {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 18px;
        font-weight: 500;
      }
      .title ha-icon {
        --mdc-icon-size: 22px;
        color: var(--state-icon-color, var(--primary-color));
      }
      .title .dim {
        font-size: 14px;
        font-weight: 400;
      }
      .dim {
        color: var(--ck-muted);
      }
      .chips,
      .footer {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
      }
      .footer {
        margin-top: 14px;
        padding-top: 12px;
        border-top: 1px solid var(--ck-line);
      }
      .chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px 12px 4px 10px;
        border-radius: 16px;
        font-size: 13px;
        font-weight: 500;
        color: var(--c);
        background: color-mix(in srgb, var(--c) 14%, transparent);
        cursor: pointer;
        white-space: nowrap;
      }
      .chip i {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--c);
        box-shadow: 0 0 0 3px color-mix(in srgb, var(--c) 25%, transparent);
      }
      .chip ha-icon {
        --mdc-icon-size: 16px;
      }
      .chip.small {
        font-size: 12px;
        padding: 2px 10px 2px 8px;
      }
      .chip.small ha-icon {
        --mdc-icon-size: 14px;
      }

      /* The hero: pack, the line with the contactors, inverter. Rows: the ends' labels and the
         state; the power; the line; what is under the line; what the pack holds. The battery
         and the inverter span the three middle rows, and the rows above and below the line
         are equally tall, so the line meets them at their middle. */
      .hero {
        display: grid;
        grid-template-columns: minmax(104px, 27%) minmax(0, 1fr) minmax(56px, 15%);
        grid-template-rows: auto 1fr auto 1fr auto;
        row-gap: 2px;
        align-items: center;
        max-width: 1040px;
        margin: 10px auto 16px;
        --ink: var(--primary-text-color);
      }
      .hero.unknown {
        --ink: var(--ck-muted);
      }
      .end {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        justify-content: space-between;
        gap: 0 8px;
        padding-bottom: 6px;
        font-size: 15px;
        font-weight: 500;
      }
      .pack-end {
        grid-area: 1 / 1;
        margin-right: 12px;
      }
      .inv-end {
        grid-area: 1 / 3;
        justify-self: end;
        flex-wrap: nowrap;
        white-space: nowrap;
      }
      .state {
        grid-area: 1 / 2;
        text-align: center;
        font-size: 13px;
        font-weight: 500;
        padding-bottom: 6px;
      }
      .batt {
        grid-area: 2 / 1 / 5 / 2;
        padding-right: 12px;
      }
      .batt-body {
        position: relative;
        aspect-ratio: 5 / 3;
        box-sizing: border-box;
        border: 4px solid var(--ink);
        border-radius: 16px;
      }
      /* The terminal */
      .batt-body::after {
        content: "";
        position: absolute;
        left: calc(100% + 3px);
        top: 34%;
        height: 32%;
        width: 9px;
        border-radius: 0 4px 4px 0;
        background: var(--ink);
      }
      /* The number is drawn twice: light on the empty part and, cut off where the charge
         ends, dark on the filled part */
      .batt-text,
      .batt-fill {
        position: absolute;
        inset: 5px;
        border-radius: 9px;
      }
      .batt-text {
        display: grid;
        place-items: center;
        font-size: clamp(30px, 9.5cqw, 76px);
        font-weight: 500;
        line-height: 1;
        letter-spacing: -0.02em;
        font-variant-numeric: tabular-nums;
        color: var(--ink);
      }
      .batt-text b {
        font-weight: 500;
        white-space: nowrap;
      }
      .batt-text span {
        font-size: 0.42em;
        margin-left: 2px;
      }
      .batt-fill {
        inset: 5px;
        background: var(--soc);
        clip-path: inset(0 calc(100% - var(--level)) 0 0 round 9px);
        transition: clip-path 0.8s ease;
      }
      .batt-fill .batt-text {
        inset: 0;
        color: var(--card-background-color, #111);
      }
      .hero.unknown .batt-fill {
        display: none;
      }
      .hero .power {
        grid-area: 2 / 2;
        align-self: end;
        text-align: center;
        margin: 0;
        padding: 0 4px;
        display: block;
      }
      .hero .hero-num {
        font-size: clamp(34px, 10cqw, 76px);
        font-weight: 500;
      }
      .wire {
        grid-area: 3 / 2;
        display: flex;
        align-items: center;
        min-width: 0;
      }
      /* A stretch of the line. Arrow heads run along it the way the power flows. */
      .seg {
        position: relative;
        flex: 1;
        height: 24px;
        min-width: 0;
      }
      .seg::before {
        content: "";
        position: absolute;
        left: 0;
        right: 0;
        top: 10px;
        height: 4px;
        border-radius: 2px;
        background: var(--flow);
      }
      .seg.far::before {
        background: var(--far);
      }
      .hero.charging .seg::after,
      .hero.discharging .seg::after {
        content: "";
        position: absolute;
        inset: 0;
        background: var(--flow);
        -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='56' height='24' viewBox='0 0 56 24'%3E%3Cpath d='M22 3 L32 12 L22 21' fill='none' stroke='black' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")
          0 0 / 56px 24px repeat-x;
        mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='56' height='24' viewBox='0 0 56 24'%3E%3Cpath d='M22 3 L32 12 L22 21' fill='none' stroke='black' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")
          0 0 / 56px 24px repeat-x;
        animation: along 1.6s linear infinite;
      }
      /* Charging runs from the inverter to the pack: the same, mirrored */
      .hero.charging .seg {
        transform: scaleX(-1);
      }
      @keyframes along {
        to {
          -webkit-mask-position: 56px 0;
          mask-position: 56px 0;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        .seg::after {
          animation: none !important;
        }
      }
      .switch {
        flex: none;
        width: 76px;
        height: 44px;
        margin: 0 -4px;
        overflow: visible;
      }
      .switch line,
      .switch path {
        fill: none;
        stroke: var(--flow);
        stroke-width: 4;
        stroke-linecap: round;
      }
      .switch path {
        stroke-width: 2.5;
        stroke-dasharray: 2 6;
      }
      .switch circle {
        fill: var(--flow);
      }
      .switch circle.far {
        fill: var(--far);
      }
      .hero:not(.charging):not(.discharging) .switch circle.far {
        fill: var(--ck-muted);
      }
      .under {
        grid-area: 4 / 2;
        align-self: start;
        text-align: center;
        padding: 0 4px;
      }
      .contactors {
        font-size: 13px;
        color: var(--ck-muted);
      }
      .amps {
        margin-top: 6px;
        font-size: clamp(14px, 2.6cqw, 17px);
      }
      .inv {
        grid-area: 2 / 3 / 5 / 4;
        aspect-ratio: 1;
        box-sizing: border-box;
        border: 4px solid var(--primary-text-color);
        border-radius: 16px;
        display: grid;
        place-items: center;
      }
      .inv svg {
        width: 72%;
      }
      .inv path {
        fill: none;
        stroke: var(--primary-text-color);
        stroke-width: 4;
        stroke-linecap: round;
      }
      .stored {
        grid-area: 5 / 1 / 6 / 4;
        display: flex;
        flex-wrap: wrap;
        gap: 2px 18px;
        padding-top: 8px;
        font-size: 14px;
      }
      .stored .dim {
        color: var(--ck-muted);
      }

      /* On a phone there is no room for the two ends' names beside the state: the voltages
         stay, over the things they belong to */
      @container (max-width: 500px) {
        .end .label {
          display: none;
        }
        .pack-end {
          margin-right: 0;
        }
        .hero {
          grid-template-columns: minmax(96px, 30%) minmax(0, 1fr) minmax(52px, 17%);
        }
      }

      /* Under the hero: the current between its limits and what is sent to the inverter, with
         the last 24 hours beside them when the card is wide enough */
      .lower {
        display: grid;
        gap: 18px 32px;
        grid-template-columns: minmax(0, 1fr);
      }
      .lower-left {
        display: grid;
        gap: 14px;
        align-content: start;
        min-width: 0;
      }
      @container (min-width: 820px) {
        .lower {
          grid-template-columns: minmax(0, 1fr) minmax(0, 1.1fr);
        }
      }

      .gauge {
        display: flex;
        align-items: center;
        gap: 18px;
        padding: 4px;
      }
      .battery {
        width: 74px;
        flex: none;
        overflow: visible;
      }
      .battery .level {
        transition: y 0.8s ease, height 0.8s ease;
      }
      .arrow path {
        fill: var(--card-background-color, #fff);
        opacity: 0.9;
      }
      .arrow.discharging {
        transform: rotate(180deg);
      }
      .arrow.charging path {
        animation: down 2.2s ease-in-out infinite;
      }
      .arrow.discharging path {
        animation: down 2.2s ease-in-out infinite;
      }
      @keyframes down {
        0% { transform: translateY(-12px); opacity: 0; }
        30% { opacity: 0.95; }
        70% { opacity: 0.95; }
        100% { transform: translateY(14px); opacity: 0; }
      }
      @media (prefers-reduced-motion: reduce) {
        .arrow path { animation: none !important; }
      }
      .hero-num {
        font-size: 44px;
        font-weight: 400;
        line-height: 1.05;
        letter-spacing: -0.02em;
        font-variant-numeric: tabular-nums;
      }
      .hero-num .unit {
        font-size: 0.45em;
      }
      .sub {
        display: flex;
        align-items: center;
        gap: 4px;
        margin-top: 4px;
        font-size: 13px;
        color: var(--ck-muted);
      }
      .sub ha-icon {
        --mdc-icon-size: 16px;
      }

      .flow .label {
        display: flex;
        align-items: center;
        gap: 6px;
      }
      .flow .label ha-icon {
        --mdc-icon-size: 18px;
      }
      .power {
        display: flex;
        align-items: baseline;
        flex-wrap: wrap;
        gap: 4px 14px;
        margin: 2px -4px 8px;
        padding: 0 4px;
      }
      .eta {
        font-size: 13px;
        color: var(--ck-muted);
      }
      .sent {
        display: flex;
        flex-wrap: wrap;
        align-items: flex-end;
        gap: 8px 30px;
        padding-top: 12px;
        border-top: 1px dashed var(--ck-line);
      }
      .sent-title {
        margin-right: 6px;
        align-self: center;
      }
      .sent-title .dim {
        font-size: 12px;
        color: var(--ck-muted);
      }
      .mid {
        font-size: 20px;
      }
      .limits .bar {
        position: relative;
        height: 10px;
        border-radius: 5px;
        background: var(--ck-track);
      }
      .side {
        position: absolute;
        top: 0;
        bottom: 0;
      }
      .side.discharge {
        background: color-mix(in srgb, var(--ck-discharge) 45%, transparent);
        border-radius: 5px 0 0 5px;
      }
      .side.charge {
        background: color-mix(in srgb, var(--ck-charge) 45%, transparent);
        border-radius: 0 5px 5px 0;
      }
      .zero {
        position: absolute;
        left: 50%;
        top: -3px;
        bottom: -3px;
        width: 2px;
        margin-left: -1px;
        background: var(--ck-muted);
        opacity: 0.6;
      }
      .marker {
        position: absolute;
        top: 50%;
        width: 16px;
        height: 16px;
        margin: -8px 0 0 -8px;
        border-radius: 50%;
        background: var(--primary-text-color);
        border: 3px solid var(--card-background-color, #fff);
        box-sizing: border-box;
        transition: left 0.6s ease;
      }
      .limit-text {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        margin: 6px -4px 0;
        font-size: 12px;
      }
      .limit-text span {
        padding: 2px 4px;
      }
      .limit-text .right {
        text-align: right;
      }
      .note {
        margin-top: 6px;
        font-size: 12px;
        color: var(--ck-warn);
      }

      .chart {
        min-width: 0;
        display: flex;
        flex-direction: column;
      }
      .chart-head {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 6px;
      }
      .legend {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        font-size: 12px;
        color: var(--ck-muted);
      }
      .legend i {
        width: 10px;
        height: 10px;
        border-radius: 2px;
      }
      .scale {
        margin-left: auto;
        font-size: 12px;
      }
      .plot {
        position: relative;
        cursor: crosshair;
        flex: 1;
        min-height: 130px;
        border-left: 1px solid var(--ck-line);
        border-bottom: 1px solid var(--ck-line);
      }
      .plot svg {
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
      }
      .grid-line {
        stroke: var(--ck-line);
        stroke-dasharray: 4 4;
        vector-effect: non-scaling-stroke;
      }
      .cursor {
        stroke: var(--primary-text-color);
        stroke-opacity: 0.5;
        vector-effect: non-scaling-stroke;
      }
      .yaxis span {
        position: absolute;
        right: 4px;
        transform: translateY(-50%);
        font-size: 10px;
        color: var(--primary-text-color);
        opacity: 0.6;
        pointer-events: none;
      }
      .loading {
        position: absolute;
        inset: 0;
        display: grid;
        place-items: center;
        font-size: 13px;
      }
      .tip {
        position: absolute;
        top: 4px;
        margin: 0 8px;
        padding: 6px 10px;
        border-radius: 8px;
        font-size: 12px;
        line-height: 1.5;
        white-space: nowrap;
        pointer-events: none;
        background: var(--card-background-color, #fff);
        border: 1px solid var(--ck-line);
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
      }
      .ticks {
        display: flex;
        justify-content: space-between;
        margin-top: 4px;
        font-size: 11px;
        color: var(--ck-muted);
      }
    `,
  ];
}

define("cellkeeper-pack-card", CellKeeperPackCard);
