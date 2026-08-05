# Vigil

Phone notification within minutes of a hardware internship posting. Runs free on
GitHub Actions (and optionally a Cloudflare Worker).

## How it works

Two public JSON sources:

- Company job boards (Greenhouse, Lever, Ashby) for companies you pick.
- The `listings.json` behind the SimplifyJobs-style repos. It carries far more
  roles than their rendered README, which filters to one season while hardware
  recruiting runs off-cycle.

New matches are pushed to your phone with [ntfy](https://ntfy.sh) (free app, no
account) with a tappable apply link. A saved `seen` list means no repeat pings.

Two tiers, both optional to run together:

- GitHub Actions runs `watch.py` every ~5 min over the aggregator feeds.
- A Cloudflare Worker polls the company boards every minute, for ~1-3 min latency.

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

- `greenhouse`, `lever`, `ashby`: company slugs from a careers page URL. Add yours.
- `include_keywords`, `exclude_keywords`: title regex filters.
- `include_categories`: aggregator category, e.g. `hardware`.
- `listings_repos`: aggregator repos; update when the season rolls over.

Preview what currently matches without sending anything: `python watch.py --dry`.

## License

MIT.
