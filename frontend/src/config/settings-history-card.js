import { html, css, nothing } from "lit";
import { PackCard, baseStyles } from "../overview/base.js";
import { define } from "../context.js";
import { withoutGlitches } from "../events/records.js";

// custom:cellkeeper-settings-history-card
//   prefix: ck0
//
// Who changed which setting, and when: the CellKeeper integration's setting records
// (cellkeeper/records/list kinds setting), newest first. A change made at the HMI shows as such;
// one read from Home Assistant's history before the integration was installed has no "by". A
// value read for a moment while the BMS restarts, then back, is left out (withoutGlitches).

const PAGE = 15;

// "max_charge_current" -> "Max charge current"
const settingName = (key) => key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

function who(data) {
  if (data?.by === "user") return data.user ? `by ${data.user}` : "by a user";
  if (data?.by === "automation") return "by an automation";
  if (data?.by === "hmi") return "at the HMI";
  return "";
}

class CellKeeperSettingsHistoryCard extends PackCard {
  static properties = { ...PackCard.properties, _rows: { state: true }, _limit: { state: true }, _filter: { state: true } };

  constructor() {
    super();
    this._limit = PAGE;
    this._filter = "";
  }

  watched() {
    // Read again after a setting changes.
    return Object.keys(this.hass?.states ?? {}).filter((id) => id.startsWith(`number.${this.prefix}_`));
  }

  updated(changed) {
    if (!changed.has("hass") || !this.hass) return;
    const sig = this.watched().map((id) => this.hass.states[id]?.state).join("|");
    if (sig !== this._sig) {
      const first = this._sig === undefined;
      this._sig = sig;
      clearTimeout(this._reload);
      this._reload = setTimeout(() => this._load(), first ? 0 : 3000);
    }
  }

  async _load() {
    try {
      const { records } = await this.hass.callWS({ type: "cellkeeper/records/list", prefix: this.prefix, kinds: ["setting"], limit: 1000 });
      this._rows = withoutGlitches(records);
    } catch (err) {
      this._rows = { error: err.message ?? String(err) };
    }
  }

  render() {
    if (!this.hass || !this._config) return nothing;
    const rows = this._rows;
    const unit = (key) => this.hass.states[`number.${this.prefix}_${key}`]?.attributes?.unit_of_measurement;
    let body;
    if (rows === undefined) body = html`<div class="dim small">Reading…</div>`;
    else if (rows.error) body = html`<div class="dim small">Couldn't read the settings history: ${rows.error}</div>`;
    else {
      const keys = [...new Set(rows.map((r) => r.key))].sort();
      const shown = rows.filter((r) => !this._filter || r.key === this._filter);
      body = html`
        ${keys.length > 1
          ? html`<select class="filter" @change=${(e) => ((this._filter = e.target.value), (this._limit = PAGE))}>
              <option value="">Every setting</option>
              ${keys.map((k) => html`<option value=${k} ?selected=${k === this._filter}>${settingName(k)}</option>`)}
            </select>`
          : nothing}
        ${shown.length
          ? html`<div class="rows">
              ${shown.slice(0, this._limit).map((r) => {
                const u = unit(r.key) ? ` ${unit(r.key)}` : "";
                const when = new Date(r.t * 1000).toLocaleString(this.hass.locale?.language, {
                  day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
                });
                return html`<div class="row">
                  <div class="when small dim">${r.time_known ? when : `before ${when}`}</div>
                  <div class="what">
                    <span class="name">${settingName(r.key)}</span>
                    <span class="num">${r.old ?? "–"}${u} → <b>${r.new}${u}</b></span>
                    <span class="small dim">${who(r.data)}</span>
                  </div>
                </div>`;
              })}
            </div>`
          : html`<div class="dim small">No setting has changed since logging began.</div>`}
        ${shown.length > this._limit
          ? html`<button class="more" @click=${() => (this._limit += PAGE * 2)}>Show more (${shown.length - this._limit})</button>`
          : nothing}
      `;
    }
    return html`<ha-card><div class="wrap">
      <div class="head"><ha-icon icon="mdi:history"></ha-icon><span class="title">Settings history</span></div>
      ${body}
    </div></ha-card>`;
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
      .filter {
        font: inherit;
        margin-bottom: 8px;
        padding: 4px 6px;
        border-radius: 6px;
        border: 1px solid var(--ck-line);
        background: none;
        color: inherit;
      }
      .row {
        display: grid;
        grid-template-columns: 150px 1fr;
        gap: 8px;
        padding: 6px 0;
        border-top: 1px solid var(--ck-line);
      }
      .what {
        display: flex;
        flex-wrap: wrap;
        gap: 4px 10px;
        align-items: baseline;
      }
      .name {
        font-weight: 500;
      }
      .more {
        font: inherit;
        color: var(--primary-color);
        background: none;
        border: none;
        cursor: pointer;
        padding: 8px 0 0;
      }
      @media (max-width: 500px) {
        .row {
          grid-template-columns: 1fr;
          gap: 0;
        }
      }
    `,
  ];
}

define("cellkeeper-settings-history-card", CellKeeperSettingsHistoryCard);
