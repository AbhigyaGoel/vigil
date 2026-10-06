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
hardware-first (hardware +4, robotics-software/perception/CV +3, wrong-skill -2).
Every relevant role is pushed; company size only sets how loud:

- **High** - a strong-fit US role (Tier A) at a startup / non-big company.
- **Default** - a strong-fit role at a company on the `big_companies` list
  (NVIDIA, Intel, ...), or a target-grade role that missed Tier A only on an
  unclear location/season or mid-band pay. Still alerts.
- **Not pushed** - weak fits and off-target functions (IT, ops, content, ...).
  They're kept in `roles.json` / the worker's `/` page for reference only.

Each push shows pay when the posting states it (the ATS's
structured range, or an hourly figure from the description; salaries also show
an hourly equivalent), plus absolute **Posted** and **Found** stamps in US
Eastern time, so a notification still makes sense a week later.

Two runners: a Cloudflare Worker polls curated boards every minute (instant),
and GitHub Actions runs `watch.py` every ~5 min over the aggregators + Workday
(with description enrichment and scoring). Delivery is [ntfy](https://ntfy.sh)
(free app, no account). Each notification has an Apply button.

A reference page (`/` on the worker; `/roles.json` for raw data) lists every
surviving role, pushed or not, merged from the worker and `roles.json`.

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
  A newly added board's currently open roles push like new postings.
- `big_companies`: established corporates; their roles push at default priority
  instead of high. Changing this list does not trigger a reseed.
- `us_places`: US city/state names, so "San Francisco" or "Massachusetts" with no
  ", CA"-style code still counts as US.
- `scoring`: `[weight, regex]` rows - the hardware-vs-software ranking lives here.
- `season_drop_terms` / `season_a_terms`: what counts as off-season vs Tier-A-eligible.
- `include_keywords`, `exclude_keywords`, `exclude_companies`: regex filters.
- `tagged_subregex`: pulls robotics-software out of the AI/ML and Software buckets.
- `pay_floor_hourly` / `pay_preferred_hourly` / `pay_midband_min_score`: pay gate.
  Only fires when a posting states an hourly $ figure (pay-transparency states put
  this on most Greenhouse/Lever/Ashby listings) - no pay stated never drops a role.
  Below the floor is a hard drop; between the floor and the preferred rate, only a
  strong hardware/robotics fit (title/desc score >= the mid-band bar) still reaches
  Tier A / high priority, otherwise it's demoted to Tier B / low priority.

Tools: `python watch.py --dry` (per-source/per-tier table, sends nothing),
`python watch.py --explain <job-id>` (full decision trace for one role),
`python watch.py --backfill-board` (fill `roles.json` with every open match), and
`python watch.py --test-alert` (one real push in the live format). Tests:
`python test_parity.py`, `python test_dedup.py`, `python test_delivery.py`,
`node cloudflare/smoke.mjs` (runs the worker against live boards; sends nothing). Changing
any filter auto-forces a silent reseed, so widening never floods you with backfill.

## License

MIT.
