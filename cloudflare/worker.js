/**
 * vigil instant tier - Cloudflare Worker.
 *
 * Polls the CURATED company boards (Greenhouse / Lever / Ashby) every minute and
 * instant-pushes new intern roles to ntfy. Curated = hand-picked companies, so
 * there is NO include_keywords GATE (a vague "Engineering Intern" is still wanted);
 * relevance only sets PRIORITY - hardware/robotics titles push high, clearly
 * off-target ones (IT/generic-SWE/ML/biomed w/ no hardware signal) push low.
 * Filtering (intern gate, excludes, title-season drop, geography) + the relevance
 * split are shared with watch.py via filters.mjs so the two can't drift - see parity.
 *
 * Aggregators, Workday, and Tier A/B scoring run in watch.py on GitHub Actions.
 * Config is read from the repo at runtime (cached 5 min); KV keys are versioned,
 * so bump the suffix to force a silent reseed after a filter-behavior change.
 */

import { makeFilters, curatedTitlePass, hardMismatch, curatedRelevant } from "./filters.mjs";

// GROUPS=1: on Workers Paid (see wrangler.toml [limits] cpu_ms) every board is
// polled every minute in one parallel pass, so posting-to-phone latency is ~1-2
// min. Raise this only if you drop back to the Free plan (10ms CPU), where each
// invocation must parse far fewer boards to fit the limit.
const GROUPS = 1;
const SUBREQUEST_CAP = 1000;   // Workers Paid per-invocation subrequest limit (Free = 50)
const UA = { "User-Agent": "vigil/2.1 (github.com/AbhigyaGoel/vigil)" };
let cfgCache = { at: 0, cfg: null };

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
const toMs = (v) => (v ? (typeof v === "number" ? (v > 1e12 ? v : v * 1000) : Date.parse(v) || 0) : 0);

async function fetchBoard(b) {
  if (b.kind === "gh") {
    const d = await gj(`https://boards-api.greenhouse.io/v1/boards/${b.slug}/jobs`);
    return (d.jobs || []).map((j) => ({
      id: `gh:${b.slug}:${j.id}`, company: b.slug, title: j.title || "",
      location: (j.location || {}).name || "", url: j.absolute_url || "", posted: toMs(j.updated_at),
      detail: `https://boards-api.greenhouse.io/v1/boards/${b.slug}/jobs/${j.id}`,  // per-job desc
    }));
  }
  if (b.kind === "lv") {
    const d = await gj(`https://api.lever.co/v0/postings/${b.slug}?mode=json`);
    return d.map((j) => ({
      id: `lv:${b.slug}:${j.id}`, company: b.slug, title: j.text || "",
      location: (j.categories || {}).location || "", url: j.hostedUrl || "", posted: toMs(j.createdAt),
      desc: j.descriptionPlain || "",   // free in the board pull -> enables grad/degree demotion
    }));
  }
  const d = await gj(`https://api.ashbyhq.com/posting-api/job-board/${b.slug}`);
  return (d.jobs || []).map((j) => ({
    id: `ab:${b.slug}:${j.id}`, company: b.slug, title: j.title || "",
    location: j.location || "", url: j.jobUrl || "", posted: toMs(j.publishedAt || j.updatedAt),
    desc: j.descriptionPlain || "",   // free in the board pull -> enables grad/degree demotion
  }));
}

function ageStr(posted) {
  if (!posted) return "";                 // omit age rather than fake "0d"
  const secs = (Date.now() - posted) / 1000;
  if (secs < 0) return "";
  return secs < 86400 ? `${Math.floor(secs / 3600)}h` : `${Math.floor(secs / 86400)}d`;
}

async function ntfy(env, job, priority = "high") {
  if (!env.NTFY_TOPIC) return;
  const age = ageStr(job.posted);
  const body = (job.location || "location n/a") + (age ? `\nPosted ${age} ago` : "");
  await fetch(`https://ntfy.sh/${env.NTFY_TOPIC}`, {
    method: "POST",
    body,   // header (company - title) is NOT repeated here
    headers: {
      ...UA, Title: `${job.company} - ${job.title}`.slice(0, 140).replace(/[^\x20-\x7e]/g, ""),
      Priority: priority, Click: job.url, Actions: `view, Apply, ${job.url}`,
    },
  });
}

export default {
  async scheduled(event, env, ctx) {
    // Keep the aggregator on schedule first (independent of the curated board pass).
    ctx.waitUntil(maybeDispatchActions(env));
    const cfg = await getConfig(env);
    const f = makeFilters(cfg);
    const maxAgeMs = (cfg.max_age_days || 21) * 86_400_000;
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
        fetchBoard(b)
          .then((jobs) => ({ b, jobs }))
          .catch((e) => {
            console.log(`WARN ${b.kind}:${b.slug} -> ${e.message}`);
            return { b, jobs: [] };
          })
      )
    );
    console.log(`boards=${boards.length} ok=${pulls.filter((p) => p.jobs.length).length}`);

    // GH grad-check detail fetches share the per-invocation subrequest budget with
    // the board pulls (already spent) and the ntfy sends (cap + headroom).
    let ghBudget = Math.max(0, SUBREQUEST_CAP - boards.length - cap - 5);
    const candidates = [];
    for (const { jobs } of pulls) {
      for (const job of jobs) {
        if (seen.has(job.id)) continue;
        if (job.posted && Date.now() - job.posted > maxAgeMs) continue;
        if (!curatedTitlePass(job, f)) continue;   // intern + excludes + season + US (cheap)
        // grad/degree demotion: Lever/Ashby carry desc inline; Greenhouse needs a
        // per-job fetch, but only for a role about to be pushed (0-2/run).
        let desc = job.desc || "";
        if (!desc && job.detail && ghBudget > 0) {
          ghBudget--;
          try { const jd = await gj(job.detail); desc = (jd.content || "").replace(/<[^>]+>/g, " "); } catch {}
          if (ghBudget === 0) console.log("WARN gh_detail_budget spent; remaining GH roles push without grad/degree check");
        }
        // budget spent -> desc stays "" -> hardMismatch false -> push anyway (recall-first)
        if (hardMismatch(desc)) continue;
        candidates.push(job);
      }
    }

    let dirty = false;
    if (seeding) {
      // First run for this bucket: suppress everything currently open (no alert flood).
      for (const j of candidates) seen.add(j.id);
      dirty = candidates.length > 0;
      await env.SEEN.put(seedKey, new Date().toISOString());
      console.log(`SEEDED bucket ${bucket}: ${seen.size} tracked`);
    } else if (candidates.length) {
      // Push up to the per-run cap, and mark ONLY what we actually pushed as seen so
      // any overflow re-surfaces next minute instead of being silently swallowed.
      const push = candidates.slice(0, cap);
      // Hardware/robotics-relevant -> high-priority instant; clearly off-target
      // (IT, generic SWE, ML, biomed with no hardware signal) -> low-priority so it
      // still lands but doesn't buzz. Mirrors watch.py's curated Tier-A/B split.
      for (const j of push) { await ntfy(env, j, curatedRelevant(j, f) ? "high" : "low"); seen.add(j.id); }
      dirty = push.length > 0;
      if (candidates.length > cap)
        console.log(`WARN ${candidates.length} new > cap ${cap}; ${candidates.length - cap} deferred to next run`);
      console.log(`PING x${push.length}: ${push.map((j) => j.title).join(" | ")}`);
    }
    if (dirty) await env.SEEN.put("seen_v4", JSON.stringify([...seen]));
  },

  async fetch(req, env) {
    if (new URL(req.url).searchParams.has("test")) {   // exercise the worker's push path
      const job = { company: "Figure", title: "[TEST] Hardware Test Intern (worker)",
        location: "Sunnyvale, CA", url: "https://github.com/AbhigyaGoel/vigil" };
      await ntfy(env, job);
      return new Response(`worker test push sent: ${job.title} -> ${job.url}\n`);
    }
    const n = JSON.parse((await env.SEEN.get("seen_v4")) || "[]").length;
    return new Response(`vigil instant tier (curated boards): alive, tracking ${n}.\n`,
      { headers: { "Content-Type": "text/plain" } });
  },
};
