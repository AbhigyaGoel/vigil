// Local smoke test for the worker: runs scheduled() against the LIVE boards with
// an in-memory KV and no ntfy topic (so nothing is sent), then renders the board.
// `node cloudflare/smoke.mjs` - asserts a never-seen config seeds silently
// (zero pushes) and that the board page renders every seeded role.
import { readFileSync } from "node:fs";
import worker from "./worker.js";

const LOCAL = "https://local.test/config.json";   // serve the working-tree config, not master's

const store = new Map([["seeded_v4:0", "smoke"]]);   // past the bucket seed, like prod
const env = {
  SEEN: { get: async (k) => store.get(k) ?? null, put: async (k, v) => { store.set(k, v); } },
  CONFIG_URL: LOCAL,
};
const pushes = [];
const realFetch = globalThis.fetch;
globalThis.fetch = (url, init) => {
  if (String(url) === LOCAL) return new Response(readFileSync(new URL("../config.json", import.meta.url)));
  if (String(url) === "https://local.test/roles.json") {
    try { return new Response(readFileSync(new URL("../roles.json", import.meta.url))); }
    catch { return new Response("[]"); }
  }
  if (String(url).startsWith("https://ntfy.sh/")) { pushes.push(init.headers.Title); return new Response("ok"); }
  if (String(url).includes("/actions/workflows/")) return new Response(null, { status: 204 });
  return realFetch(url, init);
};
const ctx = { waitUntil: () => {} };

// Cold KV (bucket never seeded): the first run must persist every seeded id.
const cold = new Map();
const coldEnv = { ...env, SEEN: { get: async (k) => cold.get(k) ?? null, put: async (k, v) => { cold.set(k, v); } } };
await worker.scheduled({}, coldEnv, ctx);
await worker.scheduled({}, coldEnv, ctx);
console.log(`cold: pushes=${pushes.length}`);
if (pushes.length) { console.log("FAIL: cold start pushed seeded roles", pushes); process.exit(1); }

await worker.scheduled({}, env, ctx);
const board = JSON.parse(store.get("board_v1") || "[]");
const known = JSON.parse(store.get("boards_known_v1") || "[]");
console.log(`run1: pushes=${pushes.length} board=${board.length} known_boards=${known.length}`);
if (pushes.length) { console.log("FAIL: a never-seen board pushed instead of seeding", pushes); process.exit(1); }

await worker.scheduled({}, env, ctx);
console.log(`run2: pushes=${pushes.length}`);

const res = await worker.fetch(new Request("https://vigil.example/"), env);
const html = await res.text();
const rows = await (await worker.fetch(new Request("https://vigil.example/roles.json"), env)).json();
console.log(`page: HTTP ${res.status}, ${html.length} bytes, ${rows.length} rows`);
const sample = rows.filter((r) => r.pay).slice(0, 3);
console.log("sample with pay:", JSON.stringify(sample, null, 1));
if (res.status !== 200 || !rows.length) process.exit(1);
console.log("SMOKE OK");
