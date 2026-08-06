# Vigil

Phone notification within minutes of a hardware internship posting. Runs free on
GitHub Actions (and optionally a Cloudflare Worker).

## How it works

Sources (all public JSON): curated company boards (Greenhouse/Lever/Ashby),
the `listings.json` behind SimplifyJobs-style repos, markdown/atom scrapes, and
Workday (NVIDIA, Micron, ADI, KLA). Each source has a `policy` that decides how
hard it's filtered - curated boards skip the keyword gate ("Engineering Intern"
at a hand-picked company is wanted); bulk/aggregator sources require keywords.

Every role is filtered (season, geography, seniority, defense), then **scored**
hardware-first (hardware +4, robotics-software/perception/CV +3, wrong-skill -2)
and split into two tiers:

- **Tier A** - score >= 3, US, Summer-2027-eligible -> **instant** ntfy push.
- **Tier B** - everything else that survives -> **one grouped digest per day.**

Two runners: a Cloudflare Worker polls curated boards every minute (instant),
and GitHub Actions runs `watch.py` every ~5 min over the aggregators + Workday
(with description enrichment and scoring). Delivery is [ntfy](https://ntfy.sh)
(free app, no account). Dropped roles are logged; a weekly digest samples them.

## Setup

1. Fork this repo. Keep it public so Actions minutes stay free.
2. Add a repo secret `NTFY_TOPIC` (Settings, Secrets and variables, Actions).
   Pick something unguessable, e.g. `vigil-3f9a2c8b`.
3. Actions tab, enable workflows, Run workflow. The first run seeds silently
   (records what is already posted, alerts on nothing).
4. Install the ntfy app and subscribe to your topic.

Laptop can be off. Alerts keep coming.

### Optional: 1-minute latency

```
cd cloudflare
npx wrangler login
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

This deploys the worker and sets a `SKIP_ATS` repo variable so Actions and the
worker never alert the same role twice.

## Config

Edit `config.json`, or put personal changes in `config.local.json` (gitignored,
so your topic and filters stay out of the repo):

- `greenhouse`, `lever`, `ashby`, `workday`: company boards. Add your targets.
- `scoring`: `[weight, regex]` rows - the hardware-vs-software ranking lives here.
- `season_drop_terms` / `season_a_terms`: what counts as off-season vs Tier-A-eligible.
- `include_keywords`, `exclude_keywords`, `exclude_companies`: regex filters.
- `tagged_subregex`: pulls robotics-software out of the AI/ML and Software buckets.

Tools: `python watch.py --dry` (per-source/per-tier table, sends nothing) and
`python watch.py --explain <job-id>` (full decision trace for one role). Changing
any filter auto-forces a silent reseed, so widening never floods you with backfill.

## License

MIT.
