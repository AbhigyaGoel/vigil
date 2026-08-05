# ⚡ vigil

**Get pinged on your phone within minutes of a hardware internship posting — before it hits the aggregator repos everyone else refreshes.**

Runs entirely on free cloud infrastructure. No server, no laptop, no accounts beyond GitHub + Cloudflare's free tiers. One Python file, one Cloudflare Worker, one JSON config.

## Why this exists

Everyone watches the same GitHub internship repos (SimplifyJobs etc.). Two problems:

1. **The README is a filtered view.** Those repos render their README from a machine-readable
   `listings.json` that holds ~10x more roles — the README filters to a single season term,
   but hardware recruiting runs off-cycle (Fall / Spring / "N/A" tags get dropped). At the time
   of writing, one repo's README showed **8** hardware roles while its `listings.json` had **200+** active.
2. **Aggregators are the slow lane.** By the time a role lands there, hundreds have applied.
   Companies' own job boards (Greenhouse / Lever / Ashby) expose public JSON APIs — a posting
   appears there the *minute* it goes live.

vigil watches both, in two tiers:

| Tier | Runs on | Cadence | Covers |
|---|---|---|---|
| **Instant** | Cloudflare Worker | every 1 min | your target companies' job boards |
| **Wide net** | GitHub Actions | every ~5 min | hundreds of companies via `listings.json` |

New matches hit your phone as [ntfy](https://ntfy.sh) push notifications with a tappable **Apply** button. A shared `seen.json` / KV store means you're never pinged twice.

## Quick start

### 1. Wide net — 5 minutes, works with your laptop off

1. **Fork this repo** (keep it public — public repos get unlimited free Actions minutes).
2. Make up a private topic name, e.g. `vigil-` + 10 random characters. In your fork:
   **Settings → Secrets and variables → Actions → New repository secret** — name `NTFY_TOPIC`, value your topic.
3. **Actions tab → enable workflows → Run workflow.** The first run seeds silently
   (records what's already posted, alerts on nothing).
4. On your phone: install the **[ntfy](https://ntfy.sh)** app and subscribe to your topic.

Done. Every genuinely new hardware internship now buzzes your phone.

### 2. Instant tier — add the 1-minute Cloudflare Worker (optional, still free)

GitHub's scheduler is the slow lane (5-min floor, often 15+ min under load). For roles within
**~1–3 minutes** of posting, deploy the worker — free plan, no credit card:

```powershell
cd cloudflare
npx wrangler login       # opens a browser — sign in or create a free account
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

`deploy.ps1` creates the dedupe store, deploys the worker, wires your topic, and sets the
`SKIP_ATS` repo variable so Actions hands the company boards over to the worker (nothing
alerts twice). The worker reads `config.json` straight from your repo, so tuning filters or
adding company slugs is a normal `git push` — no redeploy.

## How it works

```
every 1 min  ──►  Cloudflare Worker  ──►  Greenhouse / Lever / Ashby boards  ──┐
                                                                               ├─►  dedupe  ──►  ntfy push  ──►  📱
every ~5 min ──►  GitHub Actions     ──►  aggregator listings.json feeds     ──┘   (+ optional SMS)
```

- **First run seeds silently** — records everything currently posted and alerts on nothing,
  so you're only pinged for *genuinely new* roles.
- **State is the dedupe memory** — `seen.json` (committed back by Actions) and the worker's KV.
  Delete them to re-seed.

## Tuning it to your taste

All knobs live in `config.json`; override any of them per-machine in `config.local.json`
(gitignored, so your topic and personal filters never end up in a fork or PR):

| Key | What it does |
|---|---|
| `include_keywords` / `exclude_keywords` | Regex filters (case-insensitive) on titles |
| `include_categories` | Aggregator category match, e.g. `"hardware"` |
| `ats_require` | Terms required on company-board roles (default: intern/co-op) |
| `greenhouse` / `lever` / `ashby` | Company board slugs — **add your targets!** |
| `listings_repos` | Aggregator repos (update when the season rolls over) |
| `max_age_days` / `max_alerts_per_run` | Freshness window and per-run alert cap |

Finding a company's slug: open their careers page and look at the URL —
`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, or `jobs.ashbyhq.com/<slug>`.
Bad slugs log a WARN and get skipped, so guessing is safe.

**Tune without noise:** `python watch.py --dry` prints everything that currently matches
your filters, touching nothing. Iterate until the list looks like roles you'd apply to.

## Optional: real SMS

ntfy push is free and has the Apply button, but for literal text messages set four env vars
(repo secrets for Actions, or `wrangler secret put` for the worker): `TWILIO_SID`, `TWILIO_TOKEN`,
`TWILIO_FROM`, `TWILIO_TO` (≈$1.15/mo for a Twilio number). More than 4 new roles in one pass
batch into a single digest so you're never spammed.

## FAQ

**Is hitting these APIs okay?** They're public, unauthenticated endpoints that the companies'
own careers pages call. vigil polls politely (sequential, identified User-Agent) at a fraction
of a human browser's request volume.

**Why ntfy and not email/Discord/Slack?** Fastest path from "new posting" to "phone buzz"
with zero accounts. The notification surface is one function — swap in anything.

**It shipped with defense contractors excluded — why?** Personal preference of the author.
It's just a list in `config.json`; delete those lines in your fork.

## License

MIT. Fork it, retarget it, go get hired.
