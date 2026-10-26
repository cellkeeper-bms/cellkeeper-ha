// The dashboard's own update. It comes with the CellKeeper integration, so through HACS's update
// entity for the integration's repository (update.cellkeeper_update), which knows the installed
// and the latest version and can install the latest; Home Assistant then needs a restart.

/* global __VERSION__ */
// The version of the file this page is running, the integration's at build time.
export const RUNNING_VERSION = typeof __VERSION__ === "undefined" ? "" : __VERSION__;

let cachedId; // the entity found last time, checked again on each call

// The update entity's state object, or undefined when the integration wasn't installed through HACS.
export function dashboardUpdate(hass) {
  if (!hass?.states) return undefined;
  if (cachedId && hass.states[cachedId]) return hass.states[cachedId];
  cachedId = undefined;
  for (const [id, entity] of Object.entries(hass.entities ?? {})) {
    if (entity.platform !== "hacs" || !id.startsWith("update.")) continue;
    const url = hass.states[id]?.attributes.release_url ?? "";
    // Not the old separate dashboard install (CellKeeper-Dashboard), if that's still there.
    if (/\/CellKeeper-HA\b/i.test(url) || (id === "update.cellkeeper_update" && !/CellKeeper-Dashboard/i.test(url))) {
      cachedId = id;
      return hass.states[id];
    }
  }
  return undefined;
}

// "v0.11.6" and "0.11.6" are the same version.
export const sameVersion = (a, b) => !!a && !!b && a.replace(/^v/, "") === b.replace(/^v/, "");
