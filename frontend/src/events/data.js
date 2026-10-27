// An event's data, decoded: what tripped it. The HMI sends the latest occurrence's 8 bytes as
// hex after the count ("WARNING (7) d=FA0FFFF2D4C"), and the catalogue has each event's layout
// from the BMS's events.h (make_events.py): fields at a bit with a type and unit, or fixed codes
// (a stale reading...).

const BITS = { i8: 8, u8: 8, i16: 16, u16: 16, i32: 32, u32: 32, i64: 64, u64: 64, hex8: 8, hex16: 16, hex32: 32, hex64: 64 };

// unit -> [divide by, digits, shown unit]
const UNITS = {
  mV: [1000, 3, "V"],
  mA: [1000, 1, "A"],
  dA: [10, 1, "A"],
  dC: [10, 1, "A·s"],
  dDegC: [10, 1, "°C"],
  C: [1, 0, "°C"],
  ms: [1, 0, "ms"],
  count: [1, 0, ""],
  "-": [1, 0, ""],
};

function field(value, f) {
  const bits = BITS[f.type];
  const mask = (1n << BigInt(bits)) - 1n;
  let v = (value >> BigInt(f.bit)) & mask;
  if (f.type.startsWith("hex") || f.unit === "id") return `0x${v.toString(16).toUpperCase()}`;
  if (f.type.startsWith("i") && v >= 1n << BigInt(bits - 1)) v -= 1n << BigInt(bits);
  const n = Number(v);
  if (f.unit === "index") return `#${n + 1}`;
  const [div, digits, unit] = UNITS[f.unit] ?? [1, 0, f.unit];
  const x = n / div;
  // Pack voltages read better in volts with one decimal
  let d = digits;
  if (f.unit === "mV" && Math.abs(x) >= 100) d = 1;
  const s = x.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }).replace("-", "−");
  return unit ? `${s} ${unit}` : s;
}

// "current −52.4 A, limit 25.0 A", a code's meaning, or "" when there's nothing to say.
export function decodeEventData(entry, hex) {
  if (!hex) return "";
  let value;
  try {
    value = BigInt(`0x${hex}`);
  } catch {
    return "";
  }
  const spec = entry?.data;
  if (!spec) return `data 0x${hex}`;
  const code = spec.codes?.[hex.replace(/^0+/, "") || "0"];
  if (code) return code;
  if (spec.none || !spec.fields?.length) return "";
  return spec.fields.map((f) => `${f.name} ${field(value, f)}`).join(", ");
}
