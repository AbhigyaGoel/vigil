/**
 * vigil instant tier - Cloudflare Worker.
 *
 * Polls the CURATED company boards (Greenhouse / Lever / Ashby) every minute and
 * instant-pushes new intern roles to ntfy. Curated = hand-picked companies, so
 * there is NO include_keywords gate: a vague "Engineering Intern" there is wanted.
 * Filtering (intern gate, excludes, title-season drop, and geography) is shared
 * with watch.py via filters.mjs so the two can't drift - see parity tests.
 *
 * Aggregators, Workday, and Tier A/B scoring run in watch.py on GitHub Actions.
 * Config is read from the repo at runtime (cached 5 min); KV keys are versioned,
 * so bump the suffix to force a silent reseed after a filter-behavior change.
 */

import { makeFilters, curatedTitlePass, hardMismatch } from "./filters.mjs";

const GROUPS = 4;
const UA = { "User-Agent": "vigil/2.1 (github.com/AbhigyaGoel/vigil)" };
let cfgCache = { at: 0, cfg: null };

async function getConfig(env) {
  if (cfgCache.cfg && Date.now() - cfgCache.at < 300_000) return cfgCache.cfg;
  const url = env.CONFIG_URL || "https://raw.githubusercontent.com/AbhigyaGoel/vigil/master/config.json";
  const cfg = await (await fetch(url, { headers: UA })).json();
  cfgCache = { at: Date.now(), cfg };
  return cfg;
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

async function ntfy(env, job) {
  if (!env.NTFY_TOPIC) return;
  await fetch(`https://ntfy.sh/${env.NTFY_TOPIC}`, {
    method: "POST",
    body: `${job.company} - ${job.title}\n${job.location || "location n/a"}`,
    headers: {
      ...UA, Title: `${job.company}: ${job.title}`.slice(0, 100).replace(/[^\x20-\x7e]/g, ""),
      Tags: "zap", Priority: "high", Click: job.url, Actions: `view, Apply, ${job.url}`,
    },
  });
}

export default {
  async scheduled(event, env, ctx) {
    const cfg = await getConfig(env);
    const f = makeFilters(cfg);
    const maxAgeMs = (cfg.max_age_days || 21) * 86_400_000;
    const cap = cfg.max_alerts_per_run || 25;

    const bucket = Math.floor(Date.now() / 60_000) % GROUPS;
    const boards = boardsOf(cfg).filter((_, i) => i % GROUPS === bucket);

    const seen = new Set(JSON.parse((await env.SEEN.get("seen_v4")) || "[]"));
    const seedKey = `seeded_v4:${bucket}`;
    const seeding = !(await env.SEEN.get(seedKey));

    const found = [];
    let dirty = false;
    for (const b of boards) {
      try {
        for (const job of await fetchBoard(b)) {
          if (seen.has(job.id)) continue;
          if (job.posted && Date.now() - job.posted > maxAgeMs) continue;
          if (!curatedTitlePass(job, f)) continue;   // intern + excludes + season + US (cheap)
          // grad/degree demotion: Lever/Ashby carry desc inline; Greenhouse needs a
          // per-job fetch, but only for a role about to be pushed (0-2/run).
          let desc = job.desc || "";
          if (!desc && job.detail) {
            try { const jd = await gj(job.detail); desc = (jd.content || "").replace(/<[^>]+>/g, " "); } catch {}
          }
          if (hardMismatch(desc)) continue;
          seen.add(job.id);
          dirty = true;
          found.push(job);
        }
      } catch (e) {
        console.log(`WARN ${b.kind}:${b.slug} -> ${e.message}`);
      }
    }

    if (!seeding && found.length) {
      for (const j of found.slice(0, cap)) await ntfy(env, j);
      console.log(`PING x${Math.min(found.length, cap)}: ${found.slice(0, cap).map((j) => j.title).join(" | ")}`);
    }
    if (seeding) {
      await env.SEEN.put(seedKey, new Date().toISOString());
      console.log(`SEEDED bucket ${bucket}: ${seen.size} tracked`);
    }
    if (dirty) await env.SEEN.put("seen_v4", JSON.stringify([...seen]));
  },

  async fetch(req, env) {
    if (new URL(req.url).searchParams.has("test")) {   // exercise the worker's push path
      const job = { company: "Figure", title: "Hardware Test Intern (WORKER TEST)",
        location: "Sunnyvale, CA", url: "https://github.com/AbhigyaGoel/vigil" };
      await ntfy(env, job);
      return new Response(`worker test push sent: ${job.title} -> ${job.url}\n`);
    }
    const n = JSON.parse((await env.SEEN.get("seen_v4")) || "[]").length;
    return new Response(`vigil instant tier (curated boards): alive, tracking ${n}.\n`,
      { headers: { "Content-Type": "text/plain" } });
  },
};
