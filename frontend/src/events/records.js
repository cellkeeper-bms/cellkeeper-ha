import { html, svg } from "lit";
import { fmt, fmtDuration } from "../overview/base.js";

// The CellKeeper integration's records (cellkeeper/records/list) as event log entries, and an
// event's snapshot (cellkeeper/snapshot) as small charts of the minute before it.

// A contactor opening at more current than this is "under load": that's what wears them.
const LOAD_A = 5;

// A setting that changes and changes back this quickly, not by a user, is the HMI reading it
// while the BMS restarts (0, 1 or 65535 for a moment): both are left out.
const GLITCH_S = 120;

// The records without those setting glitches. Any order in, the same order out.
export function withoutGlitches(records) {
  const drop = new Set();
  const settings = records.filter((r) => r.kind === "setting").sort((a, b) => a.t - b.t || a.id - b.id);
  for (let i = 0; i + 1 < settings.length; i++) {
    const a = settings[i];
    const b = settings.slice(i + 1).find((r) => r.key === a.key);
    if (!b || drop.has(a.id) || b.t - a.t > GLITCH_S) continue;
    const byUser = a.data?.by === "user" || b.data?.by === "user";
    if (!byUser && a.old === b.new && a.new === b.old) {
      drop.add(a.id);
      drop.add(b.id);
    }
  }
  return drop.size ? records.filter((r) => !drop.has(r.id)) : records;
}

// The records the log shows (the daily health records are the Long Term view's).
export const ACTIVITY_KINDS = ["contactor", "system_state", "system_request", "restart", "firmware", "setting", "balance_charge", "modules"];

const STATES = { UNINIT: "Starting", INIT: "Initialising", CALIB: "Calibrating", INACTIVE: "Inactive", OPERATING: "Operating", FAULT: "Fault" };
const REQUESTS = { NONE: "nothing", RUN: "run", STOP: "stop", CALIBRATE: "calibrate" };

const BOOTING = ["UNINIT", "INIT"];
const stateName = (s) => STATES[s] ?? s;

// "max_charge_current" -> "Max charge current"
const settingName = (key) => key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

function num(hass, v, digits) {
  return v === undefined || v === null ? "–" : fmt(hass, v, digits);
}

// "5.0 A, 401 V" from a record's values
function valuesText(hass, v) {
  if (!v) return "";
  const parts = [];
  if (v.current !== undefined && v.current !== null) parts.push(`${num(hass, v.current, 1)} A`);
  if (v.pack_voltage !== undefined && v.pack_voltage !== null) parts.push(`${num(hass, v.pack_voltage, 0)} V`);
  return parts.join(", ");
}

const mv = (hass, v) => (v === undefined || v === null ? "–" : `${fmt(hass, v * 1000, 0)} mV`);

function who(data) {
  if (data.by === "user") return data.user ? ` · by ${data.user}` : " · by a user";
  if (data.by === "automation") return " · by an automation";
  if (data.by === "hmi") return " · at the HMI";
  return "";
}

// Log entries (like the events card's): { t, kind: "rec", icon, text, detail, warn, after }.
export function recordRows(records, hass, prefix) {
  const rows = [];
  for (const r of withoutGlitches(records)) {
    const t = r.t * 1000;
    const after = r.time_known ? undefined : r.after ? r.after * 1000 : null;
    const d = r.data ?? {};
    const row = { t, kind: "rec", rec: r, after };
    if (r.kind === "contactor") {
      const at = valuesText(hass, d.values);
      const amps = Math.abs(d.values?.current ?? 0);
      if (r.key === "closed") Object.assign(row, { icon: "mdi:electric-switch-closed", text: "Contactors closed", detail: at });
      else if (r.key === "opened")
        Object.assign(row, {
          icon: "mdi:electric-switch",
          text: amps > LOAD_A ? "Contactors opened under load" : "Contactors opened",
          detail: at && `at ${at}`,
          warn: amps > LOAD_A,
        });
      else Object.assign(row, { icon: "mdi:alert-octagon-outline", text: "Contactor check failed", detail: [r.new, at].filter(Boolean).join(" · "), warn: true });
    } else if (r.kind === "system_state") {
      // Starting up is part of a restart, which has its own row.
      if (BOOTING.includes(r.new)) continue;
      const was = BOOTING.includes(r.old) ? "after starting" : `was ${stateName(r.old).toLowerCase()}`;
      Object.assign(row, { icon: "mdi:state-machine", text: `BMS ${stateName(r.new).toLowerCase()}`, detail: was, warn: r.new === "FAULT" });
    } else if (r.kind === "system_request") {
      Object.assign(row, { icon: "mdi:gesture-tap-button", text: `Asked to ${REQUESTS[r.new] ?? r.new.toLowerCase()}` });
    } else if (r.kind === "restart") {
      const up = d.uptime_before ? fmtDuration(d.uptime_before / 3600) : "";
      Object.assign(row, {
        icon: "mdi:restart",
        text: r.key === "bms" ? "BMS restarted" : "HMI restarted",
        detail: up && `after ${up} up`,
        bms: r.key === "bms",
      });
    } else if (r.kind === "firmware") {
      const what = r.key === "bms" ? "BMS" : "HMI";
      Object.assign(row, {
        icon: "mdi:chip",
        text: r.old ? `${what} firmware updated` : `${what} firmware`,
        detail: r.old ? `${r.old} → ${r.new}` : r.new,
      });
    } else if (r.kind === "modules") {
      Object.assign(row, {
        icon: "mdi:identifier",
        text: r.old ? "Battery modules changed" : "Battery modules",
        detail: r.old ? `${r.old} → ${r.new}` : r.new,
        warn: !!r.old,
      });
    } else if (r.kind === "setting") {
      const unit = hass.states[`number.${prefix}_${r.key}`]?.attributes?.unit_of_measurement;
      const u = unit ? ` ${unit}` : "";
      Object.assign(row, {
        icon: "mdi:tune-variant",
        text: `${settingName(r.key)} set to ${r.new}${u}`,
        detail: `was ${r.old}${u}${who(d)}`,
      });
    } else if (r.kind === "balance_charge") {
      const spread = d.before && d.after ? `spread ${mv(hass, d.before.spread)} → ${mv(hass, d.after.spread)}` : d.before ? `spread ${mv(hass, d.before.spread)}` : "";
      const bled = d.balanced ? Object.keys(d.balanced).length : undefined;
      const cells = bled === undefined ? "" : bled ? `${bled} cell${bled > 1 ? "s" : ""} bled` : "no cells bled";
      if (r.end) {
        const took = r.time_known && !r.end_after ? fmtDuration((r.end - r.t) / 3600) : "";
        Object.assign(row, {
          t: r.end * 1000,
          after: r.end_after ? r.end_after * 1000 : undefined,
          icon: "mdi:battery-sync",
          text: took ? `Balance charge finished after ${took}` : "Balance charge finished",
          detail: [spread, cells].filter(Boolean).join(" · "),
        });
      } else {
        Object.assign(row, { icon: "mdi:battery-sync", text: "Balance charge started", detail: spread });
      }
    } else continue;
    rows.push(row);
  }
  return rows;
}

// ---- snapshots ----

const W = 300;
const H = 54;

// One small chart: lines (and a band between two series) over the snapshot's seconds, with the
// moment of the event marked.
function chart(snap, lines, band, minSpan = 0) {
  const n = snap.series.current?.length ?? lines[0]?.values?.length ?? 0;
  const all = [...lines.flatMap((l) => l.values), ...(band ? [...band[0], ...band[1]] : [])].filter((v) => v !== null && v !== undefined);
  if (!n || !all.length) return null;
  let lo = Math.min(...all);
  let hi = Math.max(...all);
  // A near-constant value stays a flat line rather than its noise filling the chart.
  if (hi - lo < Math.max(minSpan, 1e-9)) {
    const mid = (hi + lo) / 2;
    const half = Math.max(minSpan, 2) / 2;
    lo = mid - half;
    hi = mid + half;
  }
  const x = (i) => (i / (n - 1)) * W;
  const y = (v) => H - 4 - ((v - lo) / (hi - lo)) * (H - 8);
  const path = (vals) => {
    let d = "";
    vals.forEach((v, i) => {
      if (v === null || v === undefined) return;
      d += `${d && vals[i - 1] !== null && vals[i - 1] !== undefined ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    });
    return d;
  };
  let area = "";
  if (band) {
    const [a, b] = band;
    const top = a.map((v, i) => (v === null || b[i] === null ? null : [x(i), y(b[i])])).filter(Boolean);
    const bottom = a.map((v, i) => (v === null || b[i] === null ? null : [x(i), y(v)])).filter(Boolean).reverse();
    if (top.length) area = `M${[...top, ...bottom].map((p) => p.map((c) => c.toFixed(1)).join(",")).join("L")}Z`;
  }
  const at = x((snap.t - snap.start) / snap.step);
  return { lo, hi, body: svg`
    ${area ? svg`<path d=${area} fill="var(--c, var(--primary-color))" opacity="0.18"></path>` : ""}
    ${lines.map((l) => svg`<path d=${path(l.values)} fill="none" stroke=${l.color} stroke-width="1.5"></path>`)}
    <line x1=${at} x2=${at} y1="0" y2=${H} stroke="var(--c, var(--primary-color))" stroke-dasharray="3 3"></line>
  ` };
}

function panel(hass, title, unit, digits, c, extra) {
  if (!c) return "";
  return html`<div class="snap-row">
    <div class="snap-label">${title}<span class="dim">${fmt(hass, c.lo, digits)}–${fmt(hass, c.hi, digits)} ${unit}</span>${extra ?? ""}</div>
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="snap-svg">${c.body}</svg>
  </div>`;
}

// Temperatures in the user's unit (the snapshot keeps °C).
function temps(hass, values) {
  const f = hass.config?.unit_system?.temperature === "°F";
  return [f ? values.map((v) => (v === null ? null : (v * 9) / 5 + 32)) : values, f ? "°F" : "°C"];
}

export function snapshotView(hass, snap) {
  if (!snap) return html`<div class="snap dim small">Reading the snapshot…</div>`;
  if (snap.error) return html`<div class="snap dim small">Couldn't read the snapshot: ${snap.error}</div>`;
  const s = snap.series;
  const i = Math.round((snap.t - snap.start) / snap.step);
  const n = s.current?.length ?? s.pack_voltage?.length ?? 1;
  const afterS = Math.round(snap.start + (n - 1) * snap.step - snap.t);
  const fg = "var(--primary-text-color)";
  const current = s.current && chart(snap, [{ values: s.current, color: fg }], s.current_min && [s.current_min, s.current_max], 2);
  const voltage = s.pack_voltage && chart(snap, [{ values: s.pack_voltage, color: fg }], null, 1);
  const outAt = s.output_voltage?.[i] !== undefined && s.output_voltage[i] !== null ? html` · output ${fmt(hass, s.output_voltage[i], 1)} V at the event` : "";
  // The cells as their spread (highest minus lowest), in mV: on one scale the two lines are flat.
  const spread = s.cell_min && s.cell_min.map((lo, k) => (lo === null || s.cell_max[k] === null ? null : (s.cell_max[k] - lo) * 1000));
  const cells = spread && chart(snap, [{ values: spread, color: fg }], null, 5);
  const [tmax, tunit] = s.temp_max ? temps(hass, s.temp_max) : [];
  const [tmin] = s.temp_min ? temps(hass, s.temp_min) : [];
  // The highest drawn last, so it shows where the two are the same.
  const temp = tmax && chart(snap, [...(tmin ? [{ values: tmin, color: "#2196F3" }] : []), { values: tmax, color: "#F44336" }], null, 2);
  const cellAt =
    s.cell_min?.[i] !== undefined && s.cell_min[i] !== null
      ? html` · at the event: lowest #${s.cell_min_n[i]} ${fmt(hass, s.cell_min[i], 3)} V, highest #${s.cell_max_n[i]} ${fmt(hass, s.cell_max[i], 3)} V`
      : "";
  const limits =
    s.limit_charge?.[i] !== undefined || s.limit_discharge?.[i] !== undefined
      ? html`<div class="dim small">Limits at the event: charge ${num(hass, s.limit_charge?.[i], 0)} A, discharge ${num(hass, s.limit_discharge?.[i], 0)} A${
          s.soc?.[i] !== undefined && s.soc[i] !== null ? html` · SOC ${num(hass, s.soc[i], 1)} %` : ""
        }</div>`
      : "";
  return html`<div class="snap">
    <div class="dim small">The minute before it and ${afterS} s after; the dashed line is the event.</div>
    ${panel(hass, "Current", "A", 1, current)}
    ${panel(hass, "Pack voltage", "V", 1, voltage, outAt)}
    ${panel(hass, "Cell spread", "mV", 0, cells, cellAt)}
    ${panel(hass, "Module temperatures", tunit, 1, temp, html` · <span style="color:#F44336">highest</span>, <span style="color:#2196F3">lowest</span>`)}
    ${limits}
  </div>`;
}
