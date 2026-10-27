// The Long Term view: the pack's health over the years, from the CellKeeper integration's daily
// records. Only with the integration installed: nothing else keeps them.
export function longTermView(ctx) {
  if (!ctx.hass.config?.components?.includes("cellkeeper")) return null;
  return {
    title: "Long Term",
    type: "sections",
    max_columns: 4,
    sections: [{ column_span: 4, cards: [{ type: "custom:cellkeeper-longterm-card", prefix: ctx.prefix }] }],
  };
}
