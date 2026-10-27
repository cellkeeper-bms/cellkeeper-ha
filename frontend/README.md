# The CellKeeper dashboard (source)

The dashboard strategy (`custom:cellkeeper`) and its cards, built with esbuild and Lit. The
integration serves the build on every page; see the [README](../README.md) for what it shows and
its options. This folder came from the CellKeeper-Dashboard repository, with its history.

```sh
npm install
npm run build      # writes ../custom_components/cellkeeper/frontend/cellkeeper-dashboard.js
npm run build:dev  # writes dist-dev/cellkeeper-dashboard-dev.js, the test copy
npm run check      # generates the dashboard from test/fixtures/ck0.json and checks it
```

`npm run watch` rebuilds on every change. The version is the integration's
(`../custom_components/cellkeeper/manifest.json`).

**Trying a build beside the installed one.** The integration loads its copy of the cards on every
page, so a second copy with the same names would race it. `npm run build:dev` makes a copy with every
card renamed (`cellkeeper-dev-…`) and its own strategy, `custom:cellkeeper-dev`:

1. Copy `dist-dev/cellkeeper-dashboard-dev.js` to `/config/www/cellkeeper-dashboard-dev/`.
2. Add it as a resource, `/local/cellkeeper-dashboard-dev/cellkeeper-dashboard-dev.js?v=t<unix time>`
   (JavaScript module), with a new `?v=` after each copy (`/local` files are cached for 31 days).
3. Make an admin-only dashboard with `strategy: {type: custom:cellkeeper-dev, prefix: ck0}`.

That dashboard always runs the test copy, and the others never do. The test copy keeps its own
remembered view settings.

`python tools/make_events.py` rebuilds `src/events/catalog.js` from the firmware's event table, and
`python tools/make_icons.py` rebuilds `src/icons.js` from the CellKeeper logo; both read the other
CellKeeper repositories beside this one.
`python tools/make_brand.py` (needs Pillow) draws the integration's icon and logo, in
`../custom_components/cellkeeper/brand/`, from `src/icons.js`.
