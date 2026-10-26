import * as esbuild from "esbuild";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";

const { version } = JSON.parse(readFileSync("package.json", "utf8"));

// `--dev` builds a test copy that can load beside the release: every card is renamed
// (cellkeeper-pack-card -> cellkeeper-dev-pack-card) and the strategy is custom:cellkeeper-dev,
// so a dashboard with `strategy: {type: custom:cellkeeper-dev}` always runs this build, and the
// release's dashboards never do. It goes to dist-dev/, which isn't committed.
const dev = process.argv.includes("--dev");
const outfile = dev ? "dist-dev/cellkeeper-dashboard-dev.js" : "dist/cellkeeper-dashboard.js";
const shown = dev ? `${version}-dev` : version;

const options = {
  entryPoints: ["src/index.js"],
  outfile,
  bundle: true,
  format: "esm",
  target: "es2021",
  minify: true,
  legalComments: "none",
  banner: { js: `/* CellKeeper Dashboard ${shown} - https://github.com/clowrey/CellKeeper-Dashboard */` },
  define: { __VERSION__: JSON.stringify(shown) },
};

// Element and card names as string literals in the bundle (the release's names never appear
// in the dev build afterwards; build --dev checks that).
function renameForDev(code) {
  const out = code
    .replaceAll('"ll-strategy-dashboard-cellkeeper"', '"ll-strategy-dashboard-cellkeeper-dev"')
    .replace(/(["'`])custom:cellkeeper-/g, "$1custom:cellkeeper-dev-")
    .replace(/(["'`])cellkeeper-/g, "$1cellkeeper-dev-");
  const left = out.match(/["'`](custom:)?cellkeeper-(?!dev-)[a-z-]+/g);
  if (left) throw new Error(`release names left in the dev build: ${[...new Set(left)].join(", ")}`);
  return out;
}

if (process.argv.includes("--watch")) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  console.log("watching src/ ...");
} else if (dev) {
  const result = await esbuild.build({ ...options, write: false });
  mkdirSync("dist-dev", { recursive: true });
  writeFileSync(outfile, renameForDev(result.outputFiles[0].text));
  console.log(`built ${outfile} (${shown})`);
} else {
  await esbuild.build(options);
  console.log(`built ${outfile} (${version})`);
}
