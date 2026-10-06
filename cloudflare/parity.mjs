// JS side of the parity test. `node cloudflare/parity.mjs` (also run by test_parity.py).
import { readFileSync } from "node:fs";
import { makeFilters, curatedInstant, hardMismatch, curatedRelevant, extractPay, isBigCo } from "./filters.mjs";
import { fmtPay, fmtDate, fmtFound, ghPay, leverPay, ashbyPay } from "./display.mjs";

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
  const got = curatedInstant(c.job, f, cfg);
  if (got !== c.expect) { fail++; console.log(`CURATED FAIL ${c.job.title} @ ${c.job.location} -> ${got} (want ${c.expect})`); }
}
for (const c of cases.pay) {
  const got = extractPay(c.desc);
  if (got !== c.expect) { fail++; console.log(`PAY FAIL ${JSON.stringify(c.desc)} -> ${got} (want ${c.expect})`); }
}
for (const c of cases.grad) {
  const got = hardMismatch(c.desc);
  if (got !== c.expect) { fail++; console.log(`GRAD FAIL ${JSON.stringify(c.desc)} -> ${got} (want ${c.expect})`); }
}
for (const c of cases.relevance) {
  const got = curatedRelevant({ title: c.title }, f);
  if (got !== c.expect) { fail++; console.log(`RELEVANCE FAIL ${c.title} -> ${got} (want ${c.expect})`); }
}
for (const c of cases.bigco) {
  const got = isBigCo(c.company, f);
  if (got !== c.expect) { fail++; console.log(`BIGCO FAIL ${JSON.stringify(c.company)} -> ${got} (want ${c.expect})`); }
}
for (const c of cases.paydisplay) {
  const got = fmtPay(c.range);
  if (got !== c.expect) { fail++; console.log(`PAYDISPLAY FAIL ${JSON.stringify(c.range)} -> ${got} (want ${c.expect})`); }
}
const STRUCT = { gh: ghPay, lv: leverPay, ab: ashbyPay };
for (const c of cases.structpay) {
  const got = fmtPay(STRUCT[c.kind](c.raw));
  if (got !== c.expect) { fail++; console.log(`STRUCTPAY FAIL ${c.kind} ${JSON.stringify(c.raw)} -> ${got} (want ${c.expect})`); }
}
for (const c of cases.stamp) {
  const d = fmtDate(c.epoch), fo = fmtFound(c.epoch);
  if (d !== c.date || fo !== c.found) { fail++; console.log(`STAMP FAIL ${c.epoch} -> ${d} | ${fo} (want ${c.date} | ${c.found})`); }
}
const extra = cases.bigco.length + cases.paydisplay.length + cases.structpay.length + cases.stamp.length;
const n = extra + cases.geo.length + cases.season.length + cases.curated.length + cases.grad.length + cases.relevance.length + cases.pay.length;
console.log(`COUNT geo=${cases.geo.length} season=${cases.season.length} curated=${cases.curated.length} grad=${cases.grad.length} relevance=${cases.relevance.length} pay=${cases.pay.length} bigco=${cases.bigco.length} paydisplay=${cases.paydisplay.length} structpay=${cases.structpay.length} stamp=${cases.stamp.length} total=${n}`);
console.log(fail ? `JS parity: ${fail}/${n} FAIL` : `JS parity: all ${n} pass`);
process.exit(fail ? 1 : 0);
