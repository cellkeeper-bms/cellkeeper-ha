import { html, css, nothing } from "lit";
import { PackCard, baseStyles, fmt } from "../overview/base.js";
import { define } from "../context.js";
import { LEVEL_COLORS, eventCatalog, packEvents } from "./model.js";

// custom:cellkeeper-fault-logic-card
//   prefix: ck0
//
// How a fault happens, as a chart with the pack's own situation marked on it; the pack's
// display has the same chart on its Events page. The rules are the firmware's (bms/sys/events,
// bms/app/state_machines): only a FATAL event acts, a CRITICAL one turns FATAL if it stays, a
// few WARNINGs turn FATAL after 20 repeats, a FATAL one never clears, and FAULT ends only with
// a restart. Unlike the display, each step of the way to closed contactors lists the events
// that belong to it, with their level and when they turn fatal, from the catalogue.

// The way from start to closed contactors. `events` are the firmware's names of the events
// raised by that step's own checks (contactors.c); everything else can strike at any step.
const STEPS = [
  { name: "Starting", what: "Every reading must be fresh within 10 s.", events: [], note: "No event is raised yet. If a reading is still missing after 10 s the BMS goes to FAULT." },
  { name: "Inactive", what: "Waits for Run.", events: [], note: "Contactors open. Run and the calibrations are only accepted here." },
  { name: "Operating", what: "Asks the contactors to close, and tries again if that fails.", events: ["CONTACTOR_CLOSING_FAILED"] },
  { name: "Supervisor", what: "The second chip must allow it.", events: ["SUPERVISOR_NOT_ENABLED", "SUPERVISOR_DISABLED_CONTACTORS"] },
  {
    name: "Tests",
    what: "Each contactor is closed and opened in turn.",
    events: [
      "CONTACTOR_PRE_STUCK_OPEN", "CONTACTOR_PRE_STUCK_CLOSED", "CONTACTOR_POS_STUCK_OPEN",
      "CONTACTOR_POS_STUCK_CLOSED", "CONTACTOR_NEG_STUCK_OPEN", "CONTACTOR_NEG_STUCK_CLOSED",
    ],
  },
  {
    name: "Precharge",
    what: "The output must reach the pack's voltage.",
    events: ["CONTACTOR_PRECHARGE_VOLTAGE_TOO_HIGH", "CONTACTOR_PRECHARGE_CURRENT_TOO_HIGH"],
  },
  {
    name: "Closed",
    what: "Current is allowed half a second later.",
    events: ["CONTACTOR_POS_UNEXPECTED_OPEN", "CONTACTOR_NEG_UNEXPECTED_OPEN", "LINK_VOLTAGE_MISMATCH", "FUSE_VOLTAGE_DROP_HIGH"],
  },
];

// For a catalogue: the sensor key of each firmware event name, and the keys the steps list
const INDEX = new WeakMap();
function indexOf(catalog) {
  if (!INDEX.has(catalog)) {
    const keyOfType = Object.fromEntries(Object.entries(catalog).map(([key, e]) => [e.type, key]));
    INDEX.set(catalog, { keyOfType, stepKeys: new Set(STEPS.flatMap((s) => s.events.map((type) => keyOfType[type]))) });
  }
  return INDEX.get(catalog);
}

// When an event of the catalogue turns fatal, in a few words: "fatal", "at once", "after 2 s",
// "after 20 times"; "" for one that never does.
export function fatalAfter(info) {
  if (!info) return "";
  if (info.level === "FATAL") return "fatal";
  const e = info.escalate;
  if (!e) return "";
  if (e.count) return `after ${e.count} times`;
  if (!e.ms) return "at once";
  if (e.ms >= 3600000) return `after ${e.ms / 3600000} h`;
  if (e.ms >= 60000) return `after ${e.ms / 60000} min`;
  return `after ${e.ms / 1000} s`;
}

// Where the pack is on the way: { at: 0..6 } or { at: -1 } with fault or unknown.
export function position(system, contactor, supervisorHolds) {
  if (system === undefined) return { at: -1, unknown: true };
  if (system === "FAULT") return { at: -1, fault: true };
  if (system === "UNINIT" || system === "INIT") return { at: 0 };
  if (system === "CALIB") return { at: 1, calibrating: true };
  if (system === "INACTIVE") return { at: 1 };
  if (system !== "OPERATING") return { at: -1, unknown: true };
  if (contactor === "CLOSED" || contactor === "AWAIT_OPEN") return { at: 6 };
  if (/^PRECH/.test(contactor ?? "")) return { at: 5 };
  if (/^TST_(?!FAIL)/.test(contactor ?? "")) return { at: 4 };
  if (supervisorHolds) return { at: 3, held: true };
  return { at: 2, retrying: contactor === "TST_FAIL" ? "the tests" : contactor === "PRE_FAIL" ? "precharge" : undefined };
}

class CellKeeperFaultLogicCard extends PackCard {
  watched() {
    const count = Object.keys(this.hass?.states ?? {}).length;
    if (this._eventCount !== count) {
      this._eventCount = count;
      const start = `sensor.${this.prefix}_event_`;
      this._eventIds = Object.keys(this.hass?.states ?? {}).filter((id) => id.startsWith(start));
    }
    return [
      "system_state_text", "contactor_state_text", "supervisor_state", "supervisor_reasons",
      "pack_internal_voltage", "pack_output_voltage", ...this._eventIds,
    ];
  }

  static properties = { ...PackCard.properties, _anyOpen: { state: true }, _wide: { state: true } };

  // The list of events that can strike at any step starts folded on a phone, where it is long
  firstUpdated() {
    this._wide = this.offsetWidth >= 700;
  }

  getCardSize() {
    return 8;
  }

  getGridOptions() {
    return { columns: "full", rows: "auto" };
  }

  render() {
    if (!this.hass || !this._config) return nothing;
    const events = packEvents(this.hass, this.prefix);
    const catalog = eventCatalog(this.hass, this.prefix);
    const { keyOfType, stepKeys } = indexOf(catalog);
    const byKey = Object.fromEntries(events.map((e) => [e.key, e]));
    const active = (level) => events.filter((e) => e.active && e.level === level);
    const warnings = active("WARNING"), criticals = active("CRITICAL"), fatals = active("FATAL");

    const supervisor = this.str("supervisor_state");
    const holds = supervisor !== undefined ? supervisor !== "Enabled" : !!byKey.warning_supervisor_not_enabled?.active;
    const pos = position(this.str("system_state_text"), this.str("contactor_state_text"), holds);

    // The events that belong to no step: they can strike wherever the pack is. Only those
    // that can end in FAULT are listed; the rest only inform.
    const anyTime = events.filter((e) => !stepKeys.has(e.key) && fatalAfter(catalog[e.key]));
    const areas = [...new Set(anyTime.map((e) => e.area))];
    const anyActive = anyTime.filter((e) => e.active).length;
    const informing = events.filter((e) => !stepKeys.has(e.key) && !fatalAfter(catalog[e.key])).length;

    return html`
      <ha-card>
        <div class="wrap">
          <div class="head">
            <ha-icon icon="mdi:sitemap-outline"></ha-icon>
            <div class="title">How a fault happens</div>
          </div>

          <div class="now">
            <div class="label">Now</div>
            <div class="sentence">${this._now(pos, warnings, criticals, fatals)}</div>
          </div>

          <div class="label">1 · What an event does</div>
          <div class="levels">
            <div class="stack">
              ${this._level("WARNING", "Warning", "Informs, nothing more.", "a few turn fatal after 20 repeats", warnings.length)}
              ${this._level("CRITICAL", "Critical", "A hard limit passed, or a reading lost.", "turns fatal if it stays active, most after 1 to 20 s", criticals.length)}
            </div>
            <div class="arrow"></div>
            ${this._level("FATAL", "Fatal", "Never clears.", "", fatals.length)}
            <div class="arrow"></div>
            <div class="box fault ${pos.fault ? "lit" : ""}" style="--c:${LEVEL_COLORS.FATAL}">
              <div class="name">BMS in FAULT</div>
              <div class="note">Contactors forced open. Only a BMS restart ends it; then it waits for Run.</div>
            </div>
          </div>

          <div class="label">2 · From start to closed contactors</div>
          <div class="steps">
            ${STEPS.map((step, i) => this._step(step, i, pos, byKey, catalog, keyOfType))}
          </div>

          ${anyTime.length
            ? html`
                <details ?open=${this._anyOpen ?? (this._wide || anyActive > 0)} @toggle=${(ev) => (this._anyOpen = ev.target.open)}>
                  <summary>
                    <span class="label">At any step</span>
                    <span class="summary">
                      ${anyTime.length} events end in FAULT wherever the pack is${anyActive ? html`, <b>${anyActive} active now</b>` : ""}
                    </span>
                  </summary>
                <div class="any">
                  ${areas.map(
                    (area) => html`
                      <div class="area">
                        <div class="area-name">${area}</div>
                        <div class="chips">${anyTime.filter((e) => e.area === area).map((e) => this._chip(catalog, e.key, e))}</div>
                      </div>
                    `
                  )}
                </div>
                <div class="foot">
                  These are checked all the time, whatever step the pack is at.
                  ${informing ? `${informing} more events only inform and never open the contactors.` : ""}
                </div>
                </details>
              `
            : nothing}

        </div>
      </ha-card>
    `;
  }

  _level(level, name, note, then, count) {
    return html`
      <div class="box ${count ? "lit" : ""}" style="--c:${LEVEL_COLORS[level]}">
        <div class="name">
          <span class="dot"></span>${name}${count ? html`<span class="count">${count} active</span>` : nothing}
        </div>
        <div class="note">${note}</div>
        ${then ? html`<div class="then">${then}</div>` : nothing}
      </div>
    `;
  }

  _step(step, i, pos, byKey, catalog, keyOfType) {
    const here = i === pos.at, done = pos.at > i;
    return html`
      <div class="step ${here ? (pos.held ? "here held" : "here") : done ? "done" : ""}">
        <div class="stepbox">
          <div class="name"><span class="n">${i + 1}</span>${step.name}</div>
          <div class="note">${step.what}</div>
        </div>
        <div class="chips">
          ${step.events.map((type) => this._chip(catalog, keyOfType[type], byKey[keyOfType[type]]))}
          ${step.note ? html`<div class="stepnote">${step.note}</div>` : nothing}
        </div>
      </div>
    `;
  }

  // One event: its level's colour, its name, when it turns fatal; lit while it is active, with
  // a count if it happened earlier. `e` is the pack's sensor for it, if the pack has one.
  _chip(catalog, key, e) {
    const info = catalog[key];
    if (!info) return nothing;
    const level = e?.active ? e.level : info.level;
    const after = fatalAfter(info) || (info.level === "INFO" ? "" : "never fatal");
    return html`
      <div
        class="chip ${e?.active ? "active" : ""} ${e ? "clickable" : ""}"
        style="--c:${LEVEL_COLORS[level] ?? "var(--ck-muted)"}"
        title=${info.desc ?? ""}
        @click=${e ? () => this.moreInfo(e.id) : undefined}
      >
        <span class="dot"></span>
        <span class="chip-name">${info.name}</span>
        <span class="after">${e?.active ? `${level.toLowerCase()} now` : after}</span>
        ${e && !e.active && e.count ? html`<span class="times">×${e.count}</span>` : nothing}
      </div>
    `;
  }

  _now(pos, warnings, criticals, fatals) {
    if (pos.unknown) return "No state from the pack.";
    if (pos.fault) {
      if (!fatals.length) {
        return "In FAULT with no fatal event: the readings were not all fresh 10 seconds after the start. The contactors are forced open. Restart the BMS; it then waits in Inactive for Run.";
      }
      const names = fatals.slice(0, 3).map((e) => e.name).join(", ");
      const more = fatals.length > 3 ? ` and ${fatals.length - 3} more` : "";
      return `In FAULT because of ${names}${more}. The contactors are forced open and stay open. Restart the BMS when the cause is fixed; it then waits in Inactive for Run.`;
    }
    switch (pos.at) {
      case 0:
        return "Starting: waiting for every reading to be fresh.";
      case 1:
        return pos.calibrating
          ? "Calibrating. It goes back to Inactive when that is done."
          : "Inactive: the contactors stay open until Run is pressed.";
      case 2:
        return pos.retrying
          ? `Operating: ${pos.retrying} failed on the last try; it asks the contactors to close again.`
          : "Operating: asking the contactors to close.";
      case 3: {
        const reasons = this.str("supervisor_reasons");
        const why = reasons && reasons !== "none" ? `: ${reasons}` : "";
        return `Waiting for the supervisor, which holds the contactors off${why}. It lets go by itself when its checks pass.`;
      }
      case 4:
        return "Testing the contactors: each is closed and opened in turn before precharge.";
      case 5: {
        const out = this.numIn("pack_output_voltage", "V"), pack = this.numIn("pack_internal_voltage", "V");
        return out === undefined || pack === undefined
          ? "Precharging: the output must reach the pack's voltage."
          : `Precharging: the output is at ${fmt(this.hass, out, 0)} V and must reach the pack's ${fmt(this.hass, pack, 0)} V.`;
      }
      default:
        if (criticals.length) return "Running, contactors closed. A critical event is active: if it stays, the BMS goes to FAULT.";
        if (warnings.length) return "Running, contactors closed. A warning is active; it does not stop the pack.";
        return "Running, contactors closed. Nothing is in the way.";
    }
  }

  static styles = [
    baseStyles,
    css`
      .wrap {
        padding: 14px 16px 16px;
        container-type: inline-size;
      }
      .head {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 12px;
      }
      .head > ha-icon {
        --mdc-icon-size: 20px;
        color: var(--state-icon-color, var(--primary-color));
      }
      .title {
        font-size: 16px;
        font-weight: 500;
      }
      .label {
        margin: 18px 0 8px;
      }

      /* A box of the chart: outlined, and lit in its colour (--c) when it applies now */
      .box,
      .stepbox {
        border: 1px solid var(--ck-line);
        border-radius: 10px;
        padding: 8px 12px 9px;
        background: rgba(127, 127, 127, 0.05);
        min-width: 0;
      }
      .box.lit {
        border: 2px solid var(--c);
        padding: 7px 11px 8px;
        background: color-mix(in srgb, var(--c) 20%, transparent);
      }
      .name {
        font-size: 15px;
        font-weight: 500;
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .box:not(.lit) .name,
      .step:not(.here) .stepbox .name {
        color: var(--ck-muted);
      }
      .note {
        font-size: 13px;
        color: var(--ck-muted);
        margin-top: 2px;
      }
      .then {
        font-size: 12px;
        color: var(--ck-muted);
        margin-top: 4px;
      }
      .then::before {
        content: "→ ";
      }
      .dot {
        flex: none;
        width: 9px;
        height: 9px;
        border-radius: 50%;
        background: var(--c);
      }
      .count {
        margin-left: auto;
        font-size: 12px;
        font-weight: 500;
        color: var(--primary-text-color);
      }

      /* 1: warning and critical lead into fatal, fatal into FAULT */
      .levels {
        display: grid;
        grid-template-columns: minmax(0, 1.5fr) 34px minmax(0, 1fr) 34px minmax(0, 1.4fr);
        align-items: center;
      }
      .stack {
        display: grid;
        gap: 8px;
      }
      .arrow {
        height: 2px;
        margin: 0 5px;
        background: var(--ck-muted);
        position: relative;
      }
      .arrow::after {
        content: "";
        position: absolute;
        right: -1px;
        top: -4px;
        border: 5px solid transparent;
        border-left: 8px solid var(--ck-muted);
        border-right: 0;
      }

      /* 2: the steps side by side, each with its events under it */
      .steps {
        display: grid;
        grid-template-columns: repeat(7, minmax(0, 1fr));
        gap: 0 14px;
        align-items: start;
      }
      .step {
        display: grid;
        gap: 8px;
        min-width: 0;
      }
      .stepbox {
        position: relative;
        min-height: 76px;
        box-sizing: border-box;
      }
      /* the line to the next step */
      .step:not(:last-child) .stepbox::after {
        content: "";
        position: absolute;
        left: 100%;
        top: 22px;
        width: 14px;
        height: 2px;
        margin-left: 1px;
        background: var(--ck-line);
      }
      .step.done .stepbox {
        border-color: color-mix(in srgb, var(--ck-good) 55%, transparent);
      }
      .step.done .stepbox::after {
        background: color-mix(in srgb, var(--ck-good) 55%, transparent);
      }
      .step.done .stepbox .name {
        color: var(--primary-text-color);
      }
      .step.here .stepbox {
        --c: var(--ck-good);
        border: 2px solid var(--c);
        padding: 7px 11px 8px;
        background: color-mix(in srgb, var(--c) 20%, transparent);
      }
      .step.here.held .stepbox {
        --c: var(--ck-warn);
      }
      .n {
        font-size: 11px;
        font-weight: 500;
        color: var(--ck-muted);
      }
      .stepnote {
        font-size: 12px;
        color: var(--ck-muted);
        padding: 0 2px;
      }

      /* An event */
      .chips {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        align-content: start;
        min-width: 0;
      }
      .steps .chips {
        display: grid;
      }
      .chip {
        display: grid;
        grid-template-columns: auto minmax(0, 1fr) auto;
        column-gap: 6px;
        align-items: baseline;
        border: 1px solid var(--ck-line);
        border-radius: 8px;
        padding: 4px 8px 5px;
        font-size: 13px;
        min-width: 0;
      }
      .chip .dot {
        width: 7px;
        height: 7px;
        align-self: center;
      }
      .chip .after {
        grid-column: 2 / 4;
        font-size: 11px;
        color: var(--ck-muted);
      }
      .chip .times {
        grid-row: 1;
        grid-column: 3;
        font-size: 11px;
        color: var(--ck-muted);
      }
      .chip.active {
        border-color: var(--c);
        background: color-mix(in srgb, var(--c) 20%, transparent);
      }
      .chip.active .chip-name {
        font-weight: 500;
      }
      .chip.active .after {
        color: var(--primary-text-color);
      }

      .any {
        display: grid;
        gap: 8px;
      }
      .area {
        display: grid;
        grid-template-columns: 120px minmax(0, 1fr);
        gap: 8px;
        align-items: start;
      }
      .area-name {
        font-size: 13px;
        color: var(--ck-muted);
        padding-top: 6px;
      }
      .foot {
        font-size: 12px;
        color: var(--ck-muted);
        margin-top: 8px;
      }

      .now {
        margin-bottom: 4px;
        padding-bottom: 14px;
        border-bottom: 1px solid var(--ck-line);
      }
      .now .label {
        margin: 0 0 4px;
      }
      .now + .label {
        margin-top: 14px;
      }
      details {
        margin-top: 18px;
      }
      summary {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 2px 10px;
        cursor: pointer;
        list-style: none;
        margin-bottom: 8px;
      }
      summary::-webkit-details-marker {
        display: none;
      }
      summary .label {
        margin: 0;
      }
      summary .label::before {
        content: "▸ ";
      }
      details[open] summary .label::before {
        content: "▾ ";
      }
      .summary {
        font-size: 13px;
        color: var(--ck-muted);
      }
      .summary b {
        color: var(--primary-text-color);
        font-weight: 500;
      }
      .sentence {
        font-size: 17px;
        line-height: 1.4;
      }

      /* Narrower than a desktop column: the steps become rows, each with its events beside it */
      @container (max-width: 980px) {
        .steps {
          grid-template-columns: minmax(0, 1fr);
          gap: 10px;
        }
        .step {
          grid-template-columns: 210px minmax(0, 1fr);
          gap: 12px;
          align-items: start;
        }
        .stepbox {
          min-height: 0;
        }
        .step:not(:last-child) .stepbox::after {
          left: 22px;
          top: 100%;
          width: 2px;
          height: 10px;
          margin: 1px 0 0;
        }
        .steps .chips {
          display: flex;
        }
      }
      /* A phone: everything in one column, arrows pointing down */
      @container (max-width: 560px) {
        .levels {
          grid-template-columns: minmax(0, 1fr);
        }
        .arrow {
          width: 2px;
          height: 18px;
          margin: 3px auto;
        }
        .arrow::after {
          right: -4px;
          top: auto;
          bottom: -1px;
          border: 5px solid transparent;
          border-top: 8px solid var(--ck-muted);
          border-bottom: 0;
        }
        .step {
          grid-template-columns: minmax(0, 1fr);
          gap: 6px;
        }
        .step:not(:last-child) .stepbox::after {
          display: none;
        }
        .area {
          grid-template-columns: minmax(0, 1fr);
          gap: 2px;
        }
        .area-name {
          padding-top: 4px;
        }
        .sentence {
          font-size: 16px;
        }
      }
    `,
  ];
}

define("cellkeeper-fault-logic-card", CellKeeperFaultLogicCard);
