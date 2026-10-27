import { html, css, svg, nothing } from "lit";
import { PackCard, baseStyles, fmt } from "../overview/base.js";
import { define } from "../context.js";
import { LEVEL_COLORS } from "../events/model.js";

// custom:cellkeeper-longterm-card
//   prefix: ck0
//
// The pack's health over the years, from the CellKeeper integration's daily records
// (cellkeeper/records/list kinds daily, full_charge, empty):
//   - the cell spread at each full charge, and how far each cell sits from the pack's mean
//     (first full charge against the latest: a cell that drifts shows here)
//   - equivalent full cycles and Ah out per month
//   - balancing time per cell per month (a cell that needs more bleeding month after month
//     has a higher self-discharge)
//   - contactor operations per month, and openings under load
//   - events per month by level
//   - which cell was lowest at each day's lowest point

const RANGES = { "1y": 365, all: null };
const KINDS = ["daily", "full_charge", "empty"];
const W = 600;
const H = 110;
const PAD = 6;

const pad2 = (n) => String(n).padStart(2, "0");

// The charts' buckets: months, or days while the history is shorter than SHORT_DAYS.
const SHORT_DAYS = 62;
const MONTHS = {
  name: "month",
  key: (t) => {
    const d = new Date(t * 1000);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}`;
  },
  label: (hass, key) => {
    const [y, m] = key.split("-").map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString(hass.locale?.language, { month: "short", year: "2-digit" });
  },
  next: (key) => {
    const [y, m] = key.split("-").map(Number);
    return m === 12 ? `${y + 1}-01` : `${y}-${pad2(m + 1)}`;
  },
};
const DAYS = {
  name: "day",
  key: (t) => {
    const d = new Date(t * 1000);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  },
  label: (hass, key) => {
    const [y, m, d] = key.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(hass.locale?.language, { day: "numeric", month: "short" });
  },
  next: (key) => {
    const [y, m, d] = key.split("-").map(Number);
    const n = new Date(y, m - 1, d + 1);
    return `${n.getFullYear()}-${pad2(n.getMonth() + 1)}-${pad2(n.getDate())}`;
  },
};

// The buckets from the first to the last key, none skipped.
function bucketsBetween(B, keys) {
  if (!keys.length) return [];
  const sorted = [...keys].sort();
  const out = [sorted[0]];
  while (out[out.length - 1] < sorted[sorted.length - 1] && out.length < 5000) out.push(B.next(out[out.length - 1]));
  return out;
}

// The charts stretch to the card's width at a fixed height (preserveAspectRatio none, strokes
// that don't scale); their labels are HTML underneath, so text stays text-sized.

// A dot that stays round in a stretched chart: a zero-length line with round caps.
const dot = (x, y, color, tip) =>
  svg`<line x1=${x} x2=${x} y1=${y} y2=${y} stroke=${color} stroke-width="6" stroke-linecap="round" vector-effect="non-scaling-stroke"><title>${tip}</title></line>`;

function axis(left, right) {
  return html`<div class="axis-labels small dim"><span>${left}</span><span>${right}</span></div>`;
}

// Stacked bars, one per bucket: series [{ label, color, values: {bucket: n} }].
function bars(hass, B, months, series, unit, digits = 0) {
  const totals = months.map((mo) => series.reduce((a, s) => a + (s.values[mo] ?? 0), 0));
  const max = Math.max(...totals, 0);
  if (!months.length || max <= 0) return html`<div class="dim small">Nothing yet.</div>`;
  const bw = W / months.length;
  const y = (v) => ((H - PAD) * v) / max;
  return html`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="chart">
      ${months.map((mo, i) => {
        let acc = 0;
        return svg`<g>
          <title>${B.label(hass, mo)}: ${series.map((s) => `${s.label} ${fmt(hass, s.values[mo] ?? 0, digits)}${unit}`).join(", ")}</title>
          <rect x=${i * bw} y="0" width=${bw} height=${H} fill="transparent"></rect>
          ${series.map((s) => {
            const v = s.values[mo] ?? 0;
            const h = y(v);
            acc += h;
            return v ? svg`<rect x=${i * bw + bw * 0.15} y=${H - acc} width=${bw * 0.7} height=${h} fill=${s.color}></rect>` : "";
          })}
        </g>`;
      })}
    </svg>
    ${axis(B.label(hass, months[0]), B.label(hass, months[months.length - 1]))}
    <div class="legend small dim">
      ${series.map((s) => html`<span><i style="background:${s.color}"></i>${s.label}</span>`)}
      <span class="spacer"></span><span>up to ${fmt(hass, max, digits)}${unit} a ${B.name}</span>
    </div>`;
}

// Points over time: series [{ label, color, points: [[t, v]] }].
function lines(hass, series, unit, digits, minSpan = 0) {
  const all = series.flatMap((s) => s.points);
  if (!all.length) return html`<div class="dim small">Nothing yet.</div>`;
  let t0 = Math.min(...all.map((p) => p[0]));
  let t1 = Math.max(...all.map((p) => p[0]));
  if (t1 - t0 < 86400) {
    t0 -= 43200;
    t1 += 43200;
  }
  let lo = Math.min(...all.map((p) => p[1]));
  let hi = Math.max(...all.map((p) => p[1]));
  if (hi - lo < minSpan) {
    const mid = (hi + lo) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
  }
  const x = (t) => PAD + ((t - t0) / (t1 - t0)) * (W - 2 * PAD);
  const y = (v) => H - PAD - ((v - lo) / (hi - lo || 1)) * (H - 2 * PAD);
  const fmtDate = (t) => new Date(t * 1000).toLocaleDateString(hass.locale?.language, { day: "numeric", month: "short", year: "2-digit" });
  return html`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="chart">
      ${series.map(
        (s) => svg`
          <path d=${s.points.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("")}
            fill="none" stroke=${s.color} stroke-width="1.5" opacity="0.6" vector-effect="non-scaling-stroke"></path>
          ${s.points.map((p) => dot(x(p[0]), y(p[1]), s.color, `${fmtDate(p[0])}: ${fmt(hass, p[1], digits)} ${unit}`))}
        `
      )}
    </svg>
    ${axis(fmtDate(t0), fmtDate(t1))}
    <div class="legend small dim">
      ${series.map((s) => html`<span><i style="background:${s.color}"></i>${s.label}</span>`)}
      <span class="spacer"></span><span>${fmt(hass, lo, digits)}–${fmt(hass, hi, digits)} ${unit}</span>
    </div>`;
}

// Each cell's deviation from the pack's mean, in mV: the first full charge as a faint bar, the
// latest as a solid one.
function drift(hass, first, last) {
  const dev = (c) => (c?.v ?? []).map((v) => (v === null || v === undefined ? null : (v - c.mean) * 1000));
  const a = first ? dev(first.data.cells) : [];
  const b = dev(last.data.cells);
  // Up to the last cell with a reading (older records list all 108 of the HMI's cells).
  const n = b.findLastIndex((v) => v !== null) + 1;
  if (!n) return html`<div class="dim small">No cell readings.</div>`;
  const max = Math.max(5, ...[...a, ...b].filter((v) => v !== null).map(Math.abs));
  const bw = W / n;
  const mid = H / 2;
  const y = (v) => (v / max) * (H / 2 - PAD);
  const bar = (v, i, cls) =>
    v === null || v === undefined
      ? ""
      : svg`<rect class=${cls} x=${i * bw + bw * 0.15} y=${v >= 0 ? mid - y(v) : mid} width=${bw * 0.7} height=${Math.max(Math.abs(y(v)), 0.5)}>
          <title>Cell ${i + 1}: ${fmt(hass, v, 1)} mV</title></rect>`;
  return html`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="chart">
      <line x1="0" x2=${W} y1=${mid} y2=${mid} class="axis" vector-effect="non-scaling-stroke"></line>
      ${a.slice(0, n).map((v, i) => bar(v, i, "first"))}
      ${b.map((v, i) => bar(v, i, "last"))}
    </svg>
    ${axis("cell 1", `cell ${n}`)}
    <div class="legend small dim">
      ${first ? html`<span><i class="first"></i>first full charge</span>` : nothing}
      <span><i class="last"></i>latest</span>
      <span class="spacer"></span><span>±${fmt(hass, max, 0)} mV from the mean</span>
    </div>`;
}

// Balancing time per cell per bucket, as a heatmap (buckets down, cells across).
function heatmap(hass, B, months, perMonth, cells) {
  const max = Math.max(0, ...months.flatMap((mo) => Object.values(perMonth[mo] ?? {})));
  if (!months.length || !cells || max <= 0) return html`<div class="dim small">No balancing yet.</div>`;
  return html`<div class="heat">
      ${months.map(
        (mo) => html`<div class="heat-label small dim">${B.label(hass, mo)}</div>
          <svg viewBox="0 0 ${cells} 1" preserveAspectRatio="none" class="heat-row">
            ${Array.from({ length: cells }, (_, c) => {
              const s = perMonth[mo]?.[c + 1] ?? 0;
              return s
                ? svg`<rect x=${c} y="0" width="0.92" height="1" fill="var(--ck-discharge)" opacity=${0.15 + 0.85 * (s / max)}>
                    <title>Cell ${c + 1}, ${B.label(hass, mo)}: ${fmt(hass, s / 60, 0)} min</title></rect>`
                : "";
            })}
          </svg>`
      )}
      <div></div>
      ${axis("cell 1", `cell ${cells}`)}
    </div>
    <div class="legend small dim"><span>darker: more balancing, up to ${fmt(hass, max / 60, 0)} min a ${B.name}</span></div>`;
}

class CellKeeperLongTermCard extends PackCard {
  static properties = { ...PackCard.properties, _data: { state: true }, _range: { state: true } };

  constructor() {
    super();
    this._range = "1y";
  }

  watched() {
    return ["contactor_operations", "equivalent_full_cycles", "full_charge_spread"];
  }

  updated(changed) {
    if (changed.has("hass") && this.hass && this._data === undefined) this._load();
  }

  _setRange(range) {
    this._range = range;
    this._data = undefined;
    this._load();
  }

  async _load() {
    this._data = null; // loading
    const days = RANGES[this._range];
    const start = days ? Date.now() / 1000 - days * 86400 : undefined;
    try {
      const rows = [];
      let before;
      // Newest first, a page at a time (a full charge carries every cell).
      for (let page = 0; page < 20; page++) {
        const msg = { type: "cellkeeper/records/list", prefix: this.prefix, kinds: KINDS, limit: 2000 };
        if (start) msg.start = start;
        if (before) msg.before_id = before;
        const { records } = await this.hass.callWS(msg);
        rows.push(...records);
        if (records.length < 2000) break;
        before = Math.min(...records.map((r) => r.id));
      }
      rows.sort((a, b) => a.t - b.t);
      this._data = {
        daily: rows.filter((r) => r.kind === "daily"),
        full: rows.filter((r) => r.kind === "full_charge" && r.data?.cells),
        empty: rows.filter((r) => r.kind === "empty" && r.data?.cells),
      };
    } catch (err) {
      this._data = { error: err.message ?? String(err) };
    }
  }

  render() {
    if (!this.hass || !this._config) return nothing;
    const d = this._data;
    const head = html`<div class="head">
      <ha-icon icon="mdi:chart-timeline-variant"></ha-icon>
      <span class="title">Long-term health</span>
      <span class="spacer"></span>
      ${Object.keys(RANGES).map(
        (r) => html`<button class="pill ${this._range === r ? "on" : ""}" @click=${() => this._setRange(r)}>${r === "all" ? "All" : "1 y"}</button>`
      )}
    </div>`;
    if (!d) return html`<ha-card><div class="wrap">${head}<div class="dim small">Reading the pack's history…</div></div></ha-card>`;
    if (d.error) return html`<ha-card><div class="wrap">${head}<div class="dim small">Couldn't read it: ${d.error}</div></div></ha-card>`;
    if (!d.daily.length && !d.full.length)
      return html`<ha-card><div class="wrap">${head}<div class="dim small">
        Nothing yet: the integration works out each day once it's over, from Home Assistant's history.
      </div></div></ha-card>`;

    const first = d.daily[0]?.t;
    const B = first !== undefined && d.daily[d.daily.length - 1].t - first < SHORT_DAYS * 86400 ? DAYS : MONTHS;
    const months = bucketsBetween(B, d.daily.map((r) => B.key(r.t)));
    const sum = (field) => {
      const out = {};
      for (const r of d.daily) {
        const v = typeof field === "function" ? field(r.data) : r.data?.[field];
        if (Number.isFinite(v)) out[B.key(r.t)] = (out[B.key(r.t)] ?? 0) + v;
      }
      return out;
    };
    const cycles = d.daily.reduce((a, r) => a + (r.data?.cycles ?? 0), 0);
    const ahOut = d.daily.reduce((a, r) => a + (r.data?.ah_out ?? 0), 0);
    const kwhOut = d.daily.reduce((a, r) => a + (r.data?.kwh_out ?? 0), 0);
    const ops = d.daily.reduce((a, r) => a + (r.data?.contactor?.closed ?? 0) + (r.data?.contactor?.opened ?? 0), 0);
    const underLoad = d.daily.reduce((a, r) => a + (r.data?.contactor?.under_load ?? 0), 0);

    const balancing = {};
    let cellCount = 0;
    for (const r of d.daily) {
      const mo = B.key(r.t);
      for (const [cell, s] of Object.entries(r.data?.balancing ?? {})) {
        (balancing[mo] ??= {})[cell] = (balancing[mo][cell] ?? 0) + s;
        cellCount = Math.max(cellCount, Number(cell));
      }
    }
    const lastFull = d.full[d.full.length - 1];
    cellCount = Math.max(cellCount, lastFull ? lastFull.data.cells.v.findLastIndex((v) => v !== null) + 1 : 0);

    // Which cell was lowest at the day's lowest point, most often.
    const lowest = {};
    for (const r of d.empty) lowest[r.data.cells.min_cell] = (lowest[r.data.cells.min_cell] ?? 0) + 1;
    const weakest = Object.entries(lowest).sort((a, b) => b[1] - a[1]).slice(0, 5);

    return html`<ha-card><div class="wrap">
      ${head}
      <div class="tiles">
        ${this._tile("Equivalent full cycles", fmt(this.hass, cycles, 1), `${fmt(this.hass, ahOut, 0)} Ah, ${fmt(this.hass, kwhOut, 0)} kWh out`)}
        ${this._tile("Full charges", String(d.full.length), lastFull ? `last spread ${fmt(this.hass, lastFull.data.cells.spread * 1000, 0)} mV` : "none yet")}
        ${this._tile("Contactor operations", String(ops), `${underLoad} opened under load`)}
        ${this._tile("Days logged", String(d.daily.length), first !== undefined ? `since ${DAYS.label(this.hass, DAYS.key(first))}` : "")}
      </div>

      <div class="section">
        <div class="label">Cell spread at full charge</div>
        ${lines(this.hass, [{ label: "spread", color: "var(--ck-discharge)", points: d.full.map((r) => [r.t, r.data.cells.spread * 1000]) }], "mV", 0, 5)}
      </div>
      ${lastFull
        ? html`<div class="section">
            <div class="label">Each cell against the mean, at full charge</div>
            ${drift(this.hass, d.full.length > 1 ? d.full[0] : null, lastFull)}
          </div>`
        : nothing}
      <div class="section">
        <div class="label">Lowest and highest cell at the day's lowest point</div>
        ${lines(
          this.hass,
          [
            { label: "lowest", color: "var(--ck-cell-low)", points: d.empty.map((r) => [r.t, r.data.cells.min]) },
            { label: "highest", color: "var(--ck-cell-high)", points: d.empty.map((r) => [r.t, r.data.cells.max]) },
          ],
          "V",
          3,
          0.01
        )}
        ${weakest.length
          ? html`<div class="small dim">Most often the lowest: ${weakest.map(([cell, n]) => `cell ${cell} (${n} day${n > 1 ? "s" : ""})`).join(", ")}</div>`
          : nothing}
      </div>
      <div class="section">
        <div class="label">Equivalent full cycles a ${B.name}</div>
        ${bars(this.hass, B, months, [{ label: "cycles", color: "var(--ck-charge)", values: sum("cycles") }], "", 1)}
      </div>
      <div class="section">
        <div class="label">Balancing per cell</div>
        ${heatmap(this.hass, B, months, balancing, cellCount)}
      </div>
      <div class="section">
        <div class="label">Contactor operations a ${B.name}</div>
        ${bars(this.hass, B, months, [
          { label: "under load", color: "var(--ck-bad)", values: sum((x) => (x?.contactor?.under_load ?? 0) * 1) },
          {
            label: "other",
            color: "var(--ck-info)",
            values: sum((x) => (x?.contactor?.closed ?? 0) + (x?.contactor?.opened ?? 0) - (x?.contactor?.under_load ?? 0)),
          },
        ], "")}
      </div>
      <div class="section">
        <div class="label">Events a ${B.name}</div>
        ${bars(
          this.hass,
          B,
          months,
          ["FATAL", "CRITICAL", "WARNING", "INFO"].map((level) => ({
            label: level.toLowerCase(),
            color: LEVEL_COLORS[level],
            values: sum((x) => x?.events?.[level] ?? 0),
          })),
          ""
        )}
      </div>
    </div></ha-card>`;
  }

  _tile(label, value, sub) {
    return html`<div class="tile"><div class="label">${label}</div><div class="big num">${value}</div><div class="small dim">${sub}</div></div>`;
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
      .head {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
        margin-bottom: 10px;
      }
      .head > ha-icon {
        --mdc-icon-size: 20px;
        color: var(--state-icon-color, var(--primary-color));
      }
      .title {
        font-size: 16px;
        font-weight: 500;
      }
      .pill {
        font: inherit;
        font-size: 12px;
        color: inherit;
        cursor: pointer;
        padding: 2px 10px;
        border-radius: 12px;
        border: 1px solid var(--ck-line);
        background: none;
        white-space: nowrap;
      }
      .pill.on {
        background: var(--primary-color);
        border-color: var(--primary-color);
        color: var(--text-primary-color, #fff);
      }
      .tiles {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 12px;
        margin-bottom: 8px;
      }
      .tile .big {
        font-size: 26px;
      }
      .section {
        margin-top: 16px;
      }
      .section .label {
        margin-bottom: 4px;
      }
      .chart {
        width: 100%;
        height: 110px;
        display: block;
        background: var(--ck-track);
        border-radius: 6px;
      }
      .axis-labels {
        display: flex;
        justify-content: space-between;
        font-size: 11px;
        margin-top: 2px;
      }
      .heat {
        display: grid;
        grid-template-columns: 56px 1fr;
        align-items: center;
        gap: 2px 8px;
      }
      .heat-label {
        font-size: 11px;
      }
      .heat-row {
        width: 100%;
        height: 12px;
        display: block;
        background: var(--ck-track);
      }
      .axis {
        stroke: var(--ck-line);
      }
      rect.first {
        fill: var(--ck-muted);
        opacity: 0.35;
      }
      rect.last {
        fill: var(--ck-discharge);
        opacity: 0.85;
      }
      .legend {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
        margin-top: 2px;
      }
      .legend i {
        display: inline-block;
        width: 10px;
        height: 10px;
        border-radius: 2px;
        margin-right: 4px;
        vertical-align: -1px;
      }
      .legend i.first {
        background: var(--ck-muted);
        opacity: 0.35;
      }
      .legend i.last {
        background: var(--ck-discharge);
      }
    `,
  ];
}

define("cellkeeper-longterm-card", CellKeeperLongTermCard);
