import { EVENT_CATALOG } from "./catalog.js";

// Reading the HMI's event sensors: sensor.<prefix>_event_<label>_<name>, state "INACTIVE",
// "INACTIVE (n)" (happened n times since the BMS booted, not active now) or "<LEVEL> (n)".

export const LEVELS = ["INFO", "WARNING", "CRITICAL", "FATAL"];
export const LEVEL_COLORS = { FATAL: "#D50000", CRITICAL: "#F44336", WARNING: "#FFC107", INFO: "#2196F3" };
export const PREVIOUS_COLOR = "#9C27B0";
export const LEVEL_ICONS = {
  FATAL: "mdi:alert-octagon",
  CRITICAL: "mdi:alert",
  WARNING: "mdi:alert-circle-outline",
  INFO: "mdi:information-outline",
};

export function parseState(state) {
  // HMI firmware with the event data adds the latest occurrence's, " d=<hex>", after the count.
  const d = / d=([0-9A-Fa-f]{1,16})$/.exec(state ?? "");
  const data = d ? d[1].toUpperCase() : undefined;
  const m = /^(INFO|WARNING|CRITICAL|FATAL) \((\d+)\)( d=[0-9A-Fa-f]+)?$/.exec(state ?? "");
  if (m) return { active: true, level: m[1], count: +m[2], data };
  const p = /^INACTIVE \((\d+)\)( d=[0-9A-Fa-f]+)?$/.exec(state ?? "");
  if (p) return { active: false, count: +p[1], data };
  if (state === "INACTIVE") return { active: false, count: 0 };
  return { unavailable: true, count: 0 };
}

export const levelName = (l) => l[0] + l.slice(1).toLowerCase();

// The pack's event sensors with what the catalogue knows about each, in firmware order.
export function packEvents(hass, prefix) {
  const start = `sensor.${prefix}_event_`;
  const out = [];
  for (const id of Object.keys(hass.states)) {
    if (!id.startsWith(start)) continue;
    const key = id.slice(start.length);
    const m = /^(info|warning|critical|fatal)_/.exec(key);
    if (!m) continue;
    const info = EVENT_CATALOG[key];
    const st = hass.states[id];
    out.push({
      id,
      key,
      name: info?.name ?? (st.attributes.friendly_name ?? key).replace(/^.*?Event (Fatal|Critical|Warning|Info) /i, ""),
      level: info?.level ?? m[1].toUpperCase(),
      escalate: info?.escalate,
      area: info?.area ?? "Other",
      desc: info?.desc ?? "",
      dataSpec: info?.data,
      order: info?.id ?? 1000,
      state: st.state,
      since: st.last_changed,
      ...parseState(st.state),
    });
  }
  return out.sort((a, b) => a.order - b.order || a.name.localeCompare(b.name));
}

// "turns fatal after 20 occurrences", "turns fatal after 2 s active", "turns fatal at once"
export function escalationText(e) {
  if (e.level === "FATAL") return "Fatal: opens the contactors and stays until the BMS restarts";
  if (!e.escalate) return e.level === "INFO" ? "" : "Doesn't escalate";
  if (e.escalate.count) return `Turns fatal after ${e.escalate.count} occurrences`;
  const ms = e.escalate.ms;
  if (!ms) return "Turns fatal at once";
  if (ms >= 3600000) return `Turns fatal after ${ms / 3600000} h active`;
  if (ms >= 60000) return `Turns fatal after ${ms / 60000} min active`;
  return `Turns fatal after ${ms / 1000} s active`;
}
