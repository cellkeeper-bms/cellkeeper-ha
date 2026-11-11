// Made-up packs for the preview page: a fake `hass` for each situation worth looking at. The
// same situations as the LCD simulator's scenarios (cellkeeper-hmi, sim/scenarios.h), so a card
// here and a page there can be compared.

const PREFIX = "ck0";

// Every entity of the fixture pack, unknown until a scenario gives it a value
async function blankStates() {
  const fixture = await (await fetch("../test/fixtures/ck0.json")).json();
  const states = {};
  const entities = {};
  for (const [id, name] of fixture.entities) {
    states[id] = { entity_id: id, state: "unknown", attributes: { friendly_name: `${PREFIX} ${name}` } };
    entities[id] = { entity_id: id, device_id: "dev", platform: "esphome" };
  }
  return { states, entities };
}

// The first entity whose id ends with this
const find = (states, ending) => Object.keys(states).find((id) => id.endsWith(ending));

function set(states, key, value, unit) {
  const id = key.includes(".") ? key : `sensor.${PREFIX}_${key}`;
  if (!states[id]) {
    console.warn(`the fixture has no ${id}`);
    return;
  }
  states[id] = { ...states[id], state: String(value), attributes: { ...states[id].attributes } };
  if (unit) states[id].attributes.unit_of_measurement = unit;
}

function event(states, ending, state) {
  const id = find(states, ending);
  if (id) states[id] = { ...states[id], state };
  else console.warn(`no event sensor ends with ${ending}`);
}

// Cell voltages around `mean` volts, the same pseudo-random spread the LCD simulator uses
function cells(states, count, mean, spreadMv) {
  let seed = 12345;
  let lo = Infinity;
  let hi = -Infinity;
  let sum = 0;
  for (let i = 1; i <= count; i++) {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const r = ((seed >>> 8) & 0xffff) / 65535 - 0.5;
    const v = mean + (r * spreadMv) / 1000;
    set(states, `cell_voltage_${String(i).padStart(3, "0")}`, v.toFixed(3), "V");
    set(states, `cell_balancing_time_${String(i).padStart(3, "0")}`, 0, "s");
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
    sum += v;
  }
  set(states, "cell_count", count);
  set(states, "cell_voltage_min", lo.toFixed(3), "V");
  set(states, "cell_voltage_max", hi.toFixed(3), "V");
  set(states, "cell_voltage_delta", (hi - lo).toFixed(3), "V");
  set(states, "pack_internal_voltage", sum.toFixed(1), "V");
  set(states, "pack_internal_voltage_bmb", sum.toFixed(1), "V");
  set(states, "pack_output_voltage", (sum - 0.2).toFixed(1), "V");
  return sum;
}

function limits(states, charge, discharge) {
  const names = { hard: "hard", temperature: "temperature", cell_voltage: "cell_voltage", working: "working", delta: "delta" };
  for (const [dir, parts] of [["charge", charge], ["discharge", discharge]]) {
    let effective = Infinity;
    for (const [part, amps] of Object.entries(parts)) {
      if (part === "setting") set(states, `number.${PREFIX}_max_${dir}_current`, amps, "A");
      else set(states, `current_limit_${names[part]}_${dir}`, amps, "A");
      effective = Math.min(effective, amps);
    }
    set(states, `current_limit_effective_${dir}`, effective, "A");
  }
}

// A healthy 96-cell NMC pack, operating with the contactors closed
function healthy(states) {
  const volts = cells(states, 96, 3.912, 14);
  set(states, "system_state_text", "OPERATING");
  set(states, "contactor_state_text", "CLOSED");
  set(states, "soc", 72.4, "%");
  set(states, "capacity", 228, "Ah");
  set(states, "module_temperature_min", 24.6, "°C");
  set(states, "module_temperature_max", 25.8, "°C");
  [24.6, 24.9, 25.1, 25.4, 25.8, 25.5, 25.0, 24.9].forEach((t, i) => set(states, `module_temperature_${i + 1}`, t, "°C"));
  limits(states, { hard: 150, temperature: 150, cell_voltage: 150, working: 80, delta: 6553.5, setting: 100 },
    { hard: 150, temperature: 150, cell_voltage: 150, working: 150, setting: 120 });
  set(states, "energy_daily_charge", 21.4, "kWh");
  set(states, "energy_daily_discharge", 13.9, "kWh");
  set(states, "energy_daily_net", 7.5, "kWh");
  set(states, "inverter_can_soc", 72, "%");
  set(states, "inverter_can_voltage_limit_max", 398.4, "V");
  set(states, "inverter_can_voltage_limit_min", 288.0, "V");
  set(states, "inverter_can_capacity_full", 228, "Ah");
  set(states, "inverter_can_capacity_remaining", 165, "Ah");
  set(states, "isolation_resistance", 100, "MΩ");
  set(states, "pack_pos_to_chassis", 187.4, "V");
  set(states, "pack_neg_to_chassis", -196.3, "V");
  set(states, "isolation_check_age", 76860, "s");
  set(states, "supply_voltage_3v3", 3.28, "V");
  set(states, "supply_voltage_5v", 5.22, "V");
  set(states, "supply_voltage_12v", 13.18, "V");
  set(states, "supply_voltage_contactor", 13.37, "V");
  set(states, "contactor_voltage_positive", 0.1, "V");
  set(states, "contactor_voltage_negative", 0.1, "V");
  set(states, "balance_charge_status", "Floating, next Sunday 10:00");
  for (const id of Object.keys(states)) {
    if (id.includes("_event_")) states[id] = { ...states[id], state: "INACTIVE" };
  }
  event(states, "_boot_normal", "INACTIVE (1)");
  event(states, "_inverter_detected", "INFO (1)");
  event(states, "_overcurrent_charging", "INACTIVE (2)");
  return volts;
}

function flow(states, volts, amps) {
  set(states, "current", amps, "A");
  set(states, "power_w", (volts * amps).toFixed(0), "W");
  set(states, "power", ((volts * amps) / 1000).toFixed(2), "kW");
}

const SCENARIOS = {
  charging: { what: "healthy pack charging from solar at 8.4 kW", hour: 13.67, build(states) { flow(states, healthy(states), 22.4); } },
  discharging: {
    what: "evening load, 3.1 kW out, one warning active",
    hour: 20.25,
    build(states) {
      flow(states, healthy(states), -8.3);
      set(states, "soc", 41.0, "%");
      set(states, "inverter_can_soc", 41, "%");
      set(states, "energy_daily_charge", 38.2, "kWh");
      set(states, "energy_daily_discharge", 24.7, "kWh");
      event(states, "_battery_temperature_high", "WARNING (2)");
    },
  },
  idle: {
    what: "full, idle, charging blocked by cell voltage",
    hour: 15.08,
    build(states) {
      flow(states, healthy(states), 0.02);
      set(states, "soc", 100, "%");
      set(states, "inverter_can_soc", 100, "%");
      set(states, "current_limit_cell_voltage_charge", 0, "A");
      set(states, "current_limit_effective_charge", 0, "A");
    },
  },
  precharge: {
    what: "starting: the output is charging up to pack voltage",
    hour: 13.67,
    build(states) {
      flow(states, healthy(states), -0.3);
      set(states, "contactor_state_text", "PRECHARGING");
      set(states, "pack_output_voltage", 212.0, "V");
      set(states, "contactor_voltage_positive", 163.6, "V");
    },
  },
  fault: {
    what: "bare desk board: FAULT, no cells, five events",
    hour: 13.67,
    build(states) {
      for (const id of Object.keys(states)) {
        if (id.includes("_event_")) states[id] = { ...states[id], state: "INACTIVE" };
      }
      set(states, "system_state_text", "FAULT");
      set(states, "contactor_state_text", "OPEN");
      set(states, "pack_internal_voltage", 0, "V");
      set(states, "pack_output_voltage", 0, "V");
      set(states, "current", 0.03, "A");
      set(states, "power_w", 0, "W");
      set(states, "capacity", 1, "Ah");
      set(states, "cell_count", 0);
      limits(states, { hard: 30, temperature: 0, cell_voltage: 0, working: 30, delta: 6553.5, setting: 30 },
        { hard: 30, temperature: 0, cell_voltage: 0, working: 0, setting: 30 });
      set(states, "energy_daily_charge", 0, "kWh");
      set(states, "energy_daily_discharge", 0, "kWh");
      set(states, "supply_voltage_3v3", 3.28, "V");
      set(states, "supply_voltage_5v", 5.22, "V");
      set(states, "supply_voltage_12v", 13.18, "V");
      set(states, "supply_voltage_contactor", 13.37, "V");
      set(states, "balance_charge_status", "Off");
      event(states, "_boot_normal", "INFO (1)");
      event(states, "_cell_voltages_stale", "FATAL (1)");
      event(states, "_battery_temperature_stale", "FATAL (1)");
      event(states, "_battery_voltage_very_low", "FATAL (1)");
      event(states, "_bmb_read_error", "WARNING (37283) d=103");
    },
  },
};

// A made-up day ending now at `socNow`: load through the night, sun from 8 to 17 h. Five-minute
// points, as the recorder's statistics give them.
function day(socNow, hourNow, capacityKwh) {
  const n = 288;
  let start = 50;
  let soc = [];
  let power = [];
  for (let attempt = 0; attempt < 12; attempt++) {
    let s = start;
    soc = [];
    power = [];
    for (let i = 0; i < n; i++) {
      let hour = hourNow - ((n - 1 - i) * 5) / 60;
      while (hour < 0) hour += 24;
      const sun = hour > 7.5 && hour < 17.5 ? 9.5 * Math.sin(((hour - 7.5) / 10) * Math.PI) : 0;
      const load = 1.1 + (hour > 17.5 && hour < 22.5 ? 2.4 : 0) + (hour > 6 && hour < 8 ? 1 : 0);
      let kw = sun - load;
      if (s >= 99.9 && kw > 0) kw = 0.05;
      if (s <= 8 && kw < 0) kw = 0;
      s = Math.min(100, Math.max(8, s + ((kw * 5) / 60 / capacityKwh) * 100));
      const at = Date.now() - (n - 1 - i) * 5 * 60 * 1000;
      soc.push({ start: at, mean: s });
      power.push({ start: at, mean: kw * 1000 });
    }
    if (Math.abs(s - socNow) < 0.3) break;
    start = Math.min(100, Math.max(8, start + socNow - s));
  }
  return { soc, power };
}

export const scenarioNames = Object.keys(SCENARIOS);

export async function fakeHass(name) {
  const scenario = SCENARIOS[name] ?? SCENARIOS.charging;
  const { states, entities } = await blankStates();
  scenario.build(states);
  const soc = parseFloat(states[`sensor.${PREFIX}_soc`].state);
  const history = Number.isFinite(soc) ? day(soc, scenario.hour, 86) : { soc: [], power: [] };
  return {
    states,
    entities,
    devices: { dev: { name: PREFIX.toUpperCase(), name_by_user: PREFIX } },
    locale: { language: "en" },
    themes: { darkMode: true },
    user: { is_admin: true },
    what: scenario.what,
    async callWS(message) {
      if (message.type === "recorder/statistics_during_period") {
        return Object.fromEntries(message.statistic_ids.map((id) => [id, id.endsWith("_soc") ? history.soc : history.power]));
      }
      throw new Error(`the preview has no answer for ${message.type}`);
    },
  };
}
