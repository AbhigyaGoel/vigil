// JS side of the parity test. `node cloudflare/parity.mjs` (also run by test_parity.py).
import { readFileSync } from "node:fs";
import { makeFilters, curatedInstant, hardMismatch } from "./filters.mjs";

const cfg = JSON.parse(readFileSync(new URL("../config.json", import.meta.url)));
const cases = JSON.parse(readFileSync(new URL("./parity_cases.json", import.meta.url)));
const f = makeFilters(cfg);
let fail = 0;

for (const c of cases.geo) {
  const got = f.geo(c.loc);
  if (got !== c.expect) { fail++; console.log(`GEO FAIL ${JSON.stringify(c.loc)} -> ${got} (want ${c.expect})`); }
}
for (const c of cases.season) {
  const got = f.seasonDrop(c.title);
  if (got !== c.expect) { fail++; console.log(`SEASON FAIL ${c.title} -> ${got} (want ${c.expect})`); }
}
for (const c of cases.curated) {
  const got = curatedInstant(c.job, f);
  if (got !== c.expect) { fail++; console.log(`CURATED FAIL ${c.job.title} @ ${c.job.location} -> ${got} (want ${c.expect})`); }
}
for (const c of cases.grad) {
  const got = hardMismatch(c.desc);
  if (got !== c.expect) { fail++; console.log(`GRAD FAIL ${JSON.stringify(c.desc)} -> ${got} (want ${c.expect})`); }
}
const n = cases.geo.length + cases.season.length + cases.curated.length + cases.grad.length;
console.log(`COUNT geo=${cases.geo.length} season=${cases.season.length} curated=${cases.curated.length} grad=${cases.grad.length} total=${n}`);
console.log(fail ? `JS parity: ${fail}/${n} FAIL` : `JS parity: all ${n} pass`);
process.exit(fail ? 1 : 0);
