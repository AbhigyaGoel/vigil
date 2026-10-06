/**
 * vigil instant tier - Cloudflare Worker.
 *
 * Polls the CURATED company boards (Greenhouse / Lever / Ashby) every minute and
 * instant-pushes new intern roles to ntfy. Curated = hand-picked companies, so
 * there is NO include_keywords GATE (a vague "Engineering Intern" is still wanted).
 * Hardware/robotics-relevant roles push: startups at high priority, big companies
 * (and mid-band pay on a weak-fit title) at default. Off-target functions are
 * recorded on the roles board only. The board is served by fetch() below and merges this worker's
 * finds (KV) with watch.py's roles.json from the repo.
 * Filtering (intern gate, excludes, title-season drop, geography) + the relevance
 * split are shared with watch.py via filters.mjs so the two can't drift - see parity.
 *
 * Aggregators, Workday, and Tier A/B scoring run in watch.py on GitHub Actions.
 * Config is read from the repo at runtime (cached 5 min); KV keys are versioned,
 * so bump the suffix to force a silent reseed after a filter-behavior change.
 */

import { makeFilters, curatedTitlePass, hardMismatch, curatedRelevant, payDrop, payMidbandWeak, payRange, isBigCo } from "./filters.mjs";
import { ghPay, leverPay, ashbyPay, fmtPay, fmtDate, fmtFound, pushBody, displayCompany } from "./display.mjs";
import { renderBoard } from "./board.mjs";

// GROUPS=1: on Workers Paid (see wrangler.toml [limits] cpu_ms) every board is
// polled every minute in one parallel pass, so posting-to-phone latency is ~1-2
// min. Raise this only if you drop back to the Free plan (10ms CPU), where each
// invocation must parse far fewer boards to fit the limit.
const GROUPS = 1;
const SUBREQUEST_CAP = 1000;   // Workers Paid per-invocation subrequest limit (Free = 50)
const UA = { "User-Agent": "vigil/2.1 (github.com/AbhigyaGoel/vigil)" };
let cfgCache = { at: 0, cfg: null };
let rolesCache = { at: 0, rows: [] };
const BOARD_KEY = "board_v1";
const BOARD_MAX = 800;              // worker-side finds; watch.py's roles.json holds the rest
const BOARD_KEEP_SEC = 60 * 86400;
const BOARDS_KNOWN_KEY = "boards_known_v1";

async function getConfig(env) {
  if (cfgCache.cfg && Date.now() - cfgCache.at < 300_000) return cfgCache.cfg;
  const url = env.CONFIG_URL || "https://raw.githubusercontent.com/AbhigyaGoel/vigil/master/config.json";
  const cfg = await (await fetch(url, { headers: UA })).json();
  cfgCache = { at: Date.now(), cfg };
  return cfg;
}

// Keep the AGGREGATOR (watch.py on GitHub Actions) running on time. GitHub throttles
// `schedule` workflows to every ~30-45 min instead of the configured 5, so aggregator
// roles (Simplify/markdown/Workday - everything NOT on a curated board) arrive late.
// This reliable 1-min worker cron fires a workflow_dispatch once per 5-min window,
// which GitHub does NOT throttle. If GH_DISPATCH_TOKEN is unset it no-ops and the
// (throttled) GitHub schedule remains as fallback - so this degrades gracefully.
async function maybeDispatchActions(env) {
  if (!env.GH_DISPATCH_TOKEN) return;
  const repo = env.GH_REPO || "AbhigyaGoel/vigil";
  const wf = env.GH_WORKFLOW || "watch.yml";
  const ref = env.GH_REF || "master";
  const bucket = String(Math.floor(Date.now() / 300_000));   // one dispatch per 5-min window
  if ((await env.SEEN.get("dispatch_bucket")) === bucket) return;
  try {
    const r = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/${wf}/dispatches`, {
      method: "POST",
      headers: {
        ...UA, Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref }),
    });
    if (r.status === 204) {
      await env.SEEN.put("dispatch_bucket", bucket);
      console.log(`DISPATCH ${wf} ok (5-min window ${bucket})`);
    } else {
      // Mark the window spent on a definitive auth/config error so a bad token can't
      // retry-storm every minute; transient 5xx/network falls through to retry.
      if (r.status === 401 || r.status === 403 || r.status === 404) await env.SEEN.put("dispatch_bucket", bucket);
      console.log(`WARN dispatch -> HTTP ${r.status} ${(await r.text()).slice(0, 80)}`);
    }
  } catch (e) {
    console.log(`WARN dispatch -> ${e.message}`);
  }
}

function boardsOf(cfg) {
  return [
    ...(cfg.greenhouse || []).map((s) => ({ kind: "gh", slug: s })),
    ...(cfg.lever || []).map((s) => ({ kind: "lv", slug: s })),
    ...(cfg.ashby || []).map((s) => ({ kind: "ab", slug: s })),
  ];
}

async function gj(url) {
  const r = await fetch(url, { headers: UA });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
// Lever splits a posting: descriptionPlain + `lists` (requirements, where the
// degree line usually is) + additionalPlain. Mirrors watch.py lever_text().
function leverText(j) {
  const lists = (j.lists || []).map((x) => `${x.text || ""} ${(x.content || "").replace(/<[^>]+>/g, " ")}`).join(" ");
  return [j.descriptionPlain || "", lists, j.additionalPlain || ""].join(" ");
}
const toMs = (v) => (v ? (typeof v === "number" ? (v > 1e12 ? v : v * 1000) : Date.parse(v) || 0) : 0);

// `company` stays the slug (filters match on it); `name` is what the user reads.
// payStruct is the ATS's structured pay range (display only).
async function fetchBoard(b, names) {
  if (b.kind === "gh") {
    const d = await gj(`https://boards-api.greenhouse.io/v1/boards/${b.slug}/jobs?pay_transparency=true`);
    return (d.jobs || []).map((j) => ({
      id: `gh:${b.slug}:${j.id}`, company: b.slug, name: (j.company_name || "").trim() || displayCompany(b.slug, names),
      title: j.title || "", location: (j.location || {}).name || "", url: j.absolute_url || "",
      posted: toMs(j.updated_at), published: toMs(j.first_published), payStruct: ghPay(j.pay_input_ranges),
      detail: `https://boards-api.greenhouse.io/v1/boards/${b.slug}/jobs/${j.id}`,  // per-job desc
    }));
  }
  if (b.kind === "lv") {
    // "slug@eu" = a board on Lever's EU host (e.g. Cirrus Logic).
    const [slug, region] = b.slug.split("@");
    const host = region === "eu" ? "api.eu.lever.co" : "api.lever.co";
    const d = await gj(`https://${host}/v0/postings/${slug}?mode=json`);
    return d.map((j) => ({
      id: `lv:${slug}:${j.id}`, company: slug, name: displayCompany(slug, names), title: j.text || "",
      location: (j.categories || {}).location || "", url: j.hostedUrl || "", posted: toMs(j.createdAt),
      payStruct: leverPay(j.salaryRange),
      desc: leverText(j),   // free in the board pull -> enables grad/degree demotion
    }));
  }
  const d = await gj(`https://api.ashbyhq.com/posting-api/job-board/${b.slug}?includeCompensation=true`);
  return (d.jobs || []).map((j) => ({
    id: `ab:${b.slug}:${j.id}`, company: b.slug, name: displayCompany(b.slug, names), title: j.title || "",
    location: j.location || "", url: j.jobUrl || "", posted: toMs(j.publishedAt || j.updatedAt),
    payStruct: ashbyPay(j.compensation),
    desc: j.descriptionPlain || "",   // free in the board pull -> enables grad/degree demotion
  }));
}

// One board/push record. Times are epoch SECONDS (same as watch.py's roles.json).
function roleRecord(job, f, delivery, nowSec) {
  return {
    id: job.id, company: job.name || job.company, title: job.title, location: job.location || "",
    url: job.url, pay: fmtPay(job.payStruct || payRange(job.desc || "")), season: "",
    posted: Math.floor((job.posted || 0) / 1000), published: Math.floor((job.published || 0) / 1000),
    found: nowSec, delivery, bigco: isBigCo(job.company, f) || isBigCo(job.name, f),
  };
}

async function ntfy(env, rec, priority) {
  if (!env.NTFY_TOPIC) return;
  const body = pushBody(rec.location, rec.pay, rec.season, rec.published || rec.posted, rec.found);
  await fetch(`https://ntfy.sh/${env.NTFY_TOPIC}`, {
    method: "POST",
    body,   // header (company - title) is NOT repeated here
    headers: {
      ...UA, Title: `${rec.company} - ${rec.title}`.slice(0, 140).replace(/[^\x20-\x7e]/g, ""),
      Priority: priority, Click: rec.url, Actions: `view, Apply, ${rec.url}`,
    },
  });
}

// Prepend new rows to this worker's KV board. Backfilled rows (found=0) are kept
// until the cap pushes them out; stamped rows age out after BOARD_KEEP_SEC.
async function addToBoard(env, recs, nowSec) {
  const board = JSON.parse((await env.SEEN.get(BOARD_KEY)) || "[]");
  const ids = new Set(recs.map((r) => r.id));
  const kept = board.filter((r) => !ids.has(r.id) && (!r.found || r.found >= nowSec - BOARD_KEEP_SEC));
  await env.SEEN.put(BOARD_KEY, JSON.stringify([...recs, ...kept].slice(0, BOARD_MAX)));
}

// Board rows from both producers: this worker's KV finds + watch.py's roles.json.
async function loadBoard(env) {
  const mine = JSON.parse((await env.SEEN.get(BOARD_KEY)) || "[]");
  if (Date.now() - rolesCache.at > 60_000) {
    const url = (env.CONFIG_URL || "https://raw.githubusercontent.com/AbhigyaGoel/vigil/master/config.json")
      .replace(/config\.json$/, "roles.json");
    try {
      const r = await fetch(url, { headers: UA, cf: { cacheTtl: 60 } });
      if (r.ok) rolesCache = { at: Date.now(), rows: await r.json() };
      else console.log(`WARN roles.json -> HTTP ${r.status}`);
    } catch (e) {
      console.log(`WARN roles.json -> ${e.message}`);
    }
  }
  const byId = new Map();
  for (const r of [...mine, ...rolesCache.rows]) {
    const have = byId.get(r.id);
    if (!have || (!have.found && r.found)) byId.set(r.id, r);   // a real found stamp beats a backfill
  }
  const rows = [...byId.values()].sort((a, b) =>
    (b.found || 0) - (a.found || 0) || (b.published || b.posted || 0) - (a.published || a.posted || 0));
  const nowSec = Date.now() / 1000;
  const today = fmtDate(nowSec), yday = fmtDate(nowSec - 86400);
  return rows.map((r) => {
    const d = r.found ? fmtDate(r.found) : "";
    return {
      id: r.id, company: r.company, title: r.title, location: r.location || "", url: r.url,
      pay: r.pay || "", season: r.season || "", bigco: !!r.bigco, delivery: r.delivery || "board",
      found: r.found || 0, foundStr: fmtFound(r.found), postedStr: fmtDate(r.published || r.posted),
      day: !d ? "Found before tracking started" : d === today ? "Today" : d === yday ? "Yesterday" : d,
    };
  });
}

export default {
  async scheduled(event, env, ctx) {
    // Keep the aggregator on schedule first (independent of the curated board pass).
    ctx.waitUntil(maybeDispatchActions(env));
    const cfg = await getConfig(env);
    const f = makeFilters(cfg);
    // No posting-age cutoff here: `seen` already stops repeats, and a still-open
    // role at a curated company is worth applying to however old it is. An age
    // gate silently skipped roles that became eligible late (a filter fix, a newly
    // added board) - e.g. a 30-day-old Fab2 internship was never pushed.
    const cap = cfg.max_alerts_per_run || 25;

    const bucket = Math.floor(Date.now() / 60_000) % GROUPS;
    const boards = boardsOf(cfg).filter((_, i) => i % GROUPS === bucket);

    const seen = new Set(JSON.parse((await env.SEEN.get("seen_v4")) || "[]"));
    const seedKey = `seeded_v4:${bucket}`;
    const seeding = !(await env.SEEN.get(seedKey));

    // Fetch every board in this pass concurrently. Network is I/O (not CPU-billed),
    // so the pass costs one slow board's wall-time instead of the sum, and one dead
    // board can't sink the run - each rejection is isolated to an empty result.
    const pulls = await Promise.all(
      boards.map((b) =>
        fetchBoard(b, cfg.display_names)
          .then((jobs) => ({ b, jobs, ok: true }))
          .catch((e) => {
            console.log(`WARN ${b.kind}:${b.slug} -> ${e.message}`);
            return { b, jobs: [], ok: false };
          })
      )
    );
    console.log(`boards=${boards.length} ok=${pulls.filter((p) => p.jobs.length).length}`);

    // GH grad-check detail fetches share the per-invocation subrequest budget with
    // the board pulls (already spent) and the ntfy sends (cap + headroom).
    let ghBudget = Math.max(0, SUBREQUEST_CAP - boards.length - cap - 5);

    // A board newly added to config.json: its open roles are new to the user, so
    // they go through the normal push path (cap + defer keeps a big board from
    // spamming one minute). Only the bootstrap - no known-boards list yet, i.e.
    // the first run after this feature shipped - seeds silently; a board already
    // seeded before has ids in `seen`.
    const knownRaw = await env.SEEN.get(BOARDS_KNOWN_KEY);
    const known = new Set(knownRaw ? JSON.parse(knownRaw) : pulls
      .filter(({ jobs }) => jobs.some((j) => seen.has(j.id))).map(({ b }) => `${b.kind}:${b.slug}`));
    let knownDirty = !knownRaw;
    const backfill = [];

    const candidates = [];
    for (const { b, jobs, ok } of pulls) {
      const key = `${b.kind}:${b.slug}`;
      const fresh = ok && !known.has(key);
      if (fresh) { known.add(key); knownDirty = true; }
      const silent = fresh && !knownRaw;
      for (const job of jobs) {
        if (seen.has(job.id)) continue;
        if (!curatedTitlePass(job, f)) continue;   // intern + excludes + season + US (cheap)
        if (silent) {
          seen.add(job.id);
          backfill.push(roleRecord(job, f, "board", 0));
          continue;
        }
        // grad/degree demotion: Lever/Ashby carry desc inline; Greenhouse needs a
        // per-job fetch, but only for a role about to be pushed (0-2/run).
        let desc = job.desc || "";
        if (!desc && job.detail && ghBudget > 0) {
          ghBudget--;
          try { const jd = await gj(job.detail); desc = (jd.content || "").replace(/<[^>]+>/g, " "); } catch {}
          if (ghBudget === 0) console.log("WARN gh_detail_budget spent; remaining GH roles push without grad/degree check");
        }
        job.desc = desc;   // retain for the pay mid-band priority check at push time
        // budget spent -> desc stays "" -> hardMismatch/payDrop false -> push anyway (recall-first)
        if (hardMismatch(desc)) continue;
        if (payDrop(desc, cfg)) continue;   // stated pay below pay_floor_hourly
        candidates.push(job);
      }
    }

    let dirty = backfill.length > 0;
    if (backfill.length) console.log(`NEW BOARDS seeded silently: ${backfill.length} roles -> board`);
    if (knownDirty) await env.SEEN.put(BOARDS_KNOWN_KEY, JSON.stringify([...known]));
    if (seeding) {
      // First run for this bucket: suppress everything currently open (no alert flood).
      for (const j of candidates) seen.add(j.id);
      dirty = dirty || candidates.length > 0;   // keep any new-board backfill ids too
      await env.SEEN.put(seedKey, new Date().toISOString());
      console.log(`SEEDED bucket ${bucket}: ${seen.size} tracked`);
    } else if (candidates.length) {
      // Push up to the per-run cap, and mark ONLY what we actually pushed as seen so
      // any overflow re-surfaces next minute instead of being silently swallowed.
      const push = candidates.slice(0, cap);
      // Push every hardware/robotics-relevant role: startups high; big companies
      // and mid-band pay on a weak-fit title at default (still alerts). Only
      // off-target functions (IT, generic SWE, ML, biomed, ops...) stay unpushed.
      // Mirrors watch.py deliver().
      const nowSec = Math.floor(Date.now() / 1000);
      const boardAdds = [];
      for (const j of push) {
        const big = isBigCo(j.company, f) || isBigCo(j.name, f);
        const level = !curatedRelevant(j, f) ? "board"
          : big || payMidbandWeak(j, j.desc, cfg, f) ? "default" : "high";
        const rec = roleRecord(j, f, level, nowSec);
        if (level !== "board") await ntfy(env, rec, level);
        boardAdds.push(rec);
        seen.add(j.id);
      }
      dirty = true;
      await addToBoard(env, [...boardAdds, ...backfill], nowSec);
      if (candidates.length > cap)
        console.log(`WARN ${candidates.length} new > cap ${cap}; ${candidates.length - cap} deferred to next run`);
      console.log(`NEW x${push.length} (${boardAdds.filter((r) => r.delivery !== "board").length} pushed): ${push.map((j) => j.title).join(" | ")}`);
    }
    if (backfill.length && !(candidates.length && !seeding)) {
      await addToBoard(env, backfill, Math.floor(Date.now() / 1000));
    }
    if (dirty) await env.SEEN.put("seen_v4", JSON.stringify([...seen]));
  },

  async fetch(req, env) {
    const url = new URL(req.url);
    const cfg = await getConfig(env);
    if (url.searchParams.has("test")) {   // exercise the worker's push path
      const nowSec = Math.floor(Date.now() / 1000);
      const rec = { company: "Figure", title: "[TEST] Hardware Test Intern (worker)",
        location: "Sunnyvale, CA", url: "https://github.com/AbhigyaGoel/vigil", pay: fmtPay([38, 45]),
        season: "Summer 2027", posted: nowSec - 3 * 86400, found: nowSec };
      await ntfy(env, rec, "high");
      return new Response(`worker test push sent: ${rec.title} -> ${rec.url}\n`);
    }
    if (url.pathname !== "/" && url.pathname !== "/roles.json") {
      return new Response("not found\n", { status: 404 });
    }
    const rows = await loadBoard(env);
    if (url.pathname === "/roles.json") {
      return new Response(JSON.stringify(rows), { headers: { "Content-Type": "application/json" } });
    }
    return new Response(renderBoard(rows, fmtFound(Date.now() / 1000)), {
      headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
    });
  },
};
