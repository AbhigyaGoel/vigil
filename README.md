# ⚡ vigil — never miss a hardware internship again

**Get pinged on your phone the minute a hardware internship posts — before it hits the aggregator repos.**

Zero dependencies, zero accounts, zero cost. One Python file + a JSON config. Runs quietly in the background on your own machine.

## Why this exists

Everyone watches the same GitHub internship repos (SimplifyJobs etc.). Two problems:

1. **The README is a filtered view.** Those repos render their README from a machine-readable
   `listings.json` that holds ~10x more roles — the README filters to a single season term,
   but hardware recruiting runs off-cycle (Fall / Spring / "N/A" tags get dropped). At the time
   of writing, one repo's README showed **8** hardware roles while its `listings.json` had **200+** active.
2. **Aggregators are the slow lane.** By the time a role lands there, hundreds have applied.
   Companies' own job boards (Greenhouse / Lever / Ashby) expose public JSON APIs — a posting
   appears there the *minute* it goes live.

vigil polls both: the company boards every **60 seconds** (the instant tier) and the
aggregators' hidden `listings.json` every 15 minutes (the wide net). New matches hit your
phone as push notifications with a tappable **Apply** button.

## Quick start (5 minutes, no laptop required)

Runs free on GitHub's servers every ~15 minutes:

1. **Fork this repo** (keep it public — public repos get unlimited free Actions minutes).
2. Make up a private topic name, e.g. `vigil-` + 10 random characters. In your fork:
   **Settings → Secrets and variables → Actions → New repository secret** —
   name `NTFY_TOPIC`, value your topic.
3. **Actions tab → enable workflows → vigil → Run workflow.** The first run
   seeds silently (records what's already posted, alerts on nothing).
4. On your phone: install the **[ntfy](https://ntfy.sh)** app and subscribe to your topic.

Done. From now on, every genuinely new hardware internship buzzes your phone
with a tappable **Apply** button.

## Instant mode (~1–3 min latency, still free, still no laptop)

GitHub's scheduler is the slow lane (~5–20 min real-world). For roles-within-minutes,
deploy the **instant tier** to Cloudflare Workers — free plan, no credit card, a
1-minute cron polls the company boards from Cloudflare's edge:

```powershell
cd cloudflare
npx wrangler login       # opens browser - sign in or create a free account
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

The deploy script wires your topic, creates the dedupe store, and tells the
GitHub Actions run to hand over the company boards (so nothing alerts twice).
The worker reads `config.json` straight from your repo — tune filters or add
company slugs with a normal git push, no redeploy.

## Optional: local mode (60s latency, runs on your machine)

GitHub's scheduler has ~15–40 min real-world latency. If you want the 60-second
version while your machine is on:

```powershell
# Windows — installs a hidden background daemon that starts at logon
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

```bash
# macOS / Linux
python3 -c "import secrets; print('{\"ntfy_topic\": \"vigil-%s\"}' % secrets.token_hex(5))" > config.local.json
python3 watch.py --once                            # seed (silent first pass)
nohup python3 watch.py --watch >/dev/null 2>&1 &   # or a systemd/launchd unit
```

Running both is fine for latency, but each keeps its own `seen.json`, so you may
occasionally get the same role from both. Pick one unless you love notifications.

## How it works

```
every 60s ──► Greenhouse / Lever / Ashby boards ──┐   new match?
                 (instant tier, per-company)      ├─► dedupe against seen.json
every 15m ──► aggregator listings.json feeds  ────┘   └─► ntfy push ─► your phone 📱
                 (wide net, hundreds of companies)              (+ optional SMS)
```

- **First run seeds silently** — it records everything currently posted and alerts on nothing,
  so you only ever get pinged for *genuinely new* roles.
- **`seen.json`** is the whole database. Delete it to re-seed.
- **`watch.log`** is the daemon's journal (auto-rotates at 5 MB).

## Tuning it to your taste

All knobs live in `config.json`; override any of them in `config.local.json`
(gitignored — your topic and personal filters never end up in a fork or PR):

| Key | What it does |
|---|---|
| `include_keywords` / `exclude_keywords` | Regex filters (case-insensitive) on titles |
| `include_categories` | Aggregator category match, e.g. `"hardware"` |
| `ats_require` | Terms required on company-board roles (default: intern/co-op) |
| `greenhouse` / `lever` / `ashby` | Company board slugs — **add your targets!** |
| `listings_repos` | Aggregator repos (update when the season rolls over) |
| `poll_seconds` / `feed_poll_seconds` | Poll cadence per tier |

Finding a company's slug: open their careers page and look at the URL —
`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, or `jobs.ashbyhq.com/<slug>`.
Bad slugs log a WARN and get skipped, so guessing is safe.

**Tune without noise:** `python watch.py --dry` prints everything that currently matches
your filters, touching nothing. Iterate until the list looks like roles you'd apply to.

## Real SMS (optional)

ntfy push is free and has the Apply button, but if you want literal text messages,
set four env vars for the daemon: `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM`, `TWILIO_TO`
(≈$1.15/mo for a Twilio number). More than 4 new roles in one pass batches into a single
digest text instead of spamming you.

## Daemon control (Windows)

```powershell
Get-Content .\watch.log -Wait -Tail 20          # watch it live
Get-CimInstance Win32_Process |
  ? { $_.CommandLine -like "*watch.py*" } |
  % { Stop-Process -Id $_.ProcessId -Force }    # stop it
# disable autostart: delete vigil.vbs from shell:startup
```

A localhost lock port (`lock_port`) guarantees only one instance runs, so
re-running setup or double-starting is always safe.

## FAQ

**Is hitting these APIs okay?** They're public, unauthenticated endpoints that the
companies' own careers pages call. vigil polls politely (sequential, delayed,
identified User-Agent) at a fraction of a human browser's request volume.

**Does it run when my laptop is off?** Yes — the default GitHub Actions mode runs
entirely on GitHub's servers. `seen.json` committed back to the repo is its memory.

**Why ntfy and not email/Discord/Slack?** Fastest path from "new posting" to "phone buzz"
with zero accounts. The webhook surface is one function (`ntfy()`) — swap in anything.

## License

MIT. Fork it, gut it, point it at PM roles instead. Go get hired. 🤝
