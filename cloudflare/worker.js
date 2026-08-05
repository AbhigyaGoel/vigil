/**
 * vigil instant tier - Cloudflare Worker.
 *
 * Polls the company ATS boards (Greenhouse / Lever / Ashby) on a 1-minute cron
 * and pushes new matches to ntfy. This is what gets you a role within ~1-2
 * minutes of it posting, with no machine of your own running.
 *
 * Design notes:
 *  - Filters and board slugs are fetched at runtime from config.json in the
 *    GitHub repo (cached 5 min) - single source of truth, no redeploy to tune.
 *  - Boards rotate across GROUPS buckets (one bucket per minute) to stay
 *    comfortably inside the free plan's per-invocation CPU budget.
 *  - Dedupe state is one KV key; written only when new postings appear, which
 *    keeps writes far under the free tier's daily quota.
 *  - Each bucket seeds silently on its first pass (records everything, alerts
 *    on nothing), exactly like the Python watcher.
 *
 * The aggregator feeds (10-25 MB listings.json) stay on GitHub Actions - too
 * heavy for a worker, and 5-min latency is fine for that tier.
 */

const GROUPS = 2; // each board polled every GROUPS minutes
const UA = { "User-Agent": "vigil/1.0 (github.com/AbhigyaGoel/vigil)" };

let cfgCache = { at: 0, cfg: null };

async function getConfig(env) {
  if (cfgCache.cfg && Date.now() - cfgCache.at < 300_000) return cfgCache.cfg;
  const url = env.CONFIG_URL ||
    "https://raw.githubusercontent.com/AbhigyaGoel/vigil/master/config.json";
  const cfg = await (await fetch(url, { headers: UA })).json();
  cfgCache = { at: Date.now(), cfg };
  return cfg;
}

const rx = (arr) => (arr && arr.length ? new RegExp(arr.join("|"), "i") : null);

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
      location: j.location?.name || "", url: j.absolute_url || "", posted: toMs(j.updated_at),
    }));
  }
  if (b.kind === "lv") {
    const d = await gj(`https://api.lever.co/v0/postings/${b.slug}?mode=json`);
    return d.map((j) => ({
      id: `lv:${b.slug}:${j.id}`, company: b.slug, title: j.text || "",
      location: j.categories?.location || "", url: j.hostedUrl || "", posted: toMs(j.createdAt),
    }));
  }
  const d = await gj(`https://api.ashbyhq.com/posting-api/job-board/${b.slug}`);
  return (d.jobs || []).map((j) => ({
    id: `ab:${b.slug}:${j.id}`, company: b.slug, title: j.title || "",
    location: j.location || "", url: j.jobUrl || "", posted: toMs(j.publishedAt || j.updatedAt),
  }));
}

async function ntfy(env, job) {
  if (!env.NTFY_TOPIC) return;
  await fetch(`https://ntfy.sh/${env.NTFY_TOPIC}`, {
    method: "POST",
    body: `${job.company} - ${job.title}\n${job.location || "location n/a"}`,
    headers: {
      ...UA,
      Title: `${job.company}: ${job.title}`.slice(0, 90).replace(/[^\x20-\x7e]/g, ""),
      Tags: "zap", Priority: "high", Click: job.url,
      Actions: `view, Apply, ${job.url}`,
    },
  });
}

async function sms(env, body) {
  const { TWILIO_SID: sid, TWILIO_TOKEN: tok, TWILIO_FROM: from, TWILIO_TO: to } = env;
  if (!(sid && tok && from && to)) return;
  await fetch(`https://api.twilio.com/2010-04-01/Accounts/${sid}/Messages.json`, {
    method: "POST",
    headers: { Authorization: "Basic " + btoa(`${sid}:${tok}`),
               "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ From: from, To: to, Body: body.slice(0, 1500) }),
  });
}

export default {
  async scheduled(event, env, ctx) {
    const cfg = await getConfig(env);
    const exclude = rx(cfg.exclude_keywords);
    const include = rx(cfg.include_keywords);
    const require_ = rx(cfg.ats_require);
    const maxAgeMs = (cfg.max_age_days || 21) * 86_400_000;

    const bucket = Math.floor(Date.now() / 60_000) % GROUPS;
    const boards = boardsOf(cfg).filter((_, i) => i % GROUPS === bucket);

    const seen = new Set(JSON.parse((await env.SEEN.get("seen")) || "[]"));
    const seedKey = `seeded:${bucket}`;
    const seeding = !(await env.SEEN.get(seedKey));

    const found = [];
    let dirty = false;
    for (const b of boards) {
      try {
        for (const job of await fetchBoard(b)) {
          if (!job.url || seen.has(job.id)) continue;
          seen.add(job.id);
          dirty = true;
          if (job.posted && Date.now() - job.posted > maxAgeMs) continue;
          const blob = `${job.title} ${job.company}`;
          if (exclude && exclude.test(blob)) continue;
          if (require_ && !require_.test(job.title)) continue;
          if (include && !include.test(job.title)) continue;
          found.push(job);
        }
      } catch (e) {
        console.log(`WARN ${b.kind}:${b.slug} -> ${e.message}`);
      }
    }

    if (!seeding && found.length) {
      const capped = found.slice(0, cfg.max_alerts_per_run || 25);
      for (const j of capped) await ntfy(env, j);
      if (capped.length <= (cfg.sms_digest_threshold || 4)) {
        for (const j of capped) await sms(env, `${j.company}: ${j.title}\n${j.location}\n${j.url}`);
      } else {
        await sms(env, `${capped.length} new hardware roles:\n` +
          capped.slice(0, 10).map((j) => `${j.company}: ${j.title.slice(0, 45)}`).join("\n"));
      }
      console.log(`PING x${capped.length}: ${capped.map((j) => j.title).join(" | ")}`);
    }
    if (seeding) {
      await env.SEEN.put(seedKey, new Date().toISOString());
      console.log(`SEEDED bucket ${bucket}: ${seen.size} tracked`);
    }
    if (dirty) await env.SEEN.put("seen", JSON.stringify([...seen]));
  },

  // Tiny status page so you can sanity-check it from a browser.
  async fetch(_req, env) {
    const n = JSON.parse((await env.SEEN.get("seen")) || "[]").length;
    return new Response(
      `vigil instant tier: alive, tracking ${n} postings across the ATS boards.\n`,
      { headers: { "Content-Type": "text/plain" } });
  },
};
