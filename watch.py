#!/usr/bin/env python3
"""
jobwatch - instant hardware internship tracker.

Polls two source tiers and pushes new matches to your phone the minute they post:

  1. ATS APIs (Greenhouse / Lever / Ashby) -> the INSTANT tier, polled every
     `poll_seconds` (default 60s). A role appears here the moment a company
     posts it, hours-to-days before any aggregator picks it up.
  2. Aggregator feeds -> the WIDE-NET tier, polled every `feed_poll_seconds`
     (default 15 min). This reads the machine-readable listings.json that the
     SimplifyJobs-style repos build their README from - it holds ~10x more
     hardware roles than the rendered README, because the README filters to a
     single season term while hardware recruiting runs off-cycle.

Notifications: ntfy.sh push (free, no signup, tappable Apply button) and
optionally Twilio SMS via TWILIO_SID/TOKEN/FROM/TO env vars.

Config: config.json ships the defaults; config.local.json (gitignored)
overrides any key wholesale. Run setup.ps1 on Windows to install everything.

Modes:
  python watch.py --watch    persistent daemon (what setup.ps1 installs)
  python watch.py --once     single pass (cron / CI / debugging)
  python watch.py --dry      print everything that currently matches your
                             filters, touching nothing - use this to tune

Stdlib only. No pip installs, no API keys, no accounts.
"""

import base64
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
UA = {"User-Agent": "jobwatch/4.0 (github.com/AbhigyaGoel/jobwatch)"}


def load_config():
    # utf-8-sig: tolerate the BOM that PowerShell 5.1 writes.
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    local = ROOT / "config.local.json"
    if local.exists():
        cfg.update(json.loads(local.read_text(encoding="utf-8-sig")))
    # Env var wins - lets CI (GitHub Actions) inject the topic as a secret.
    if os.environ.get("NTFY_TOPIC"):
        cfg["ntfy_topic"] = os.environ["NTFY_TOPIC"]
    return cfg


CFG = load_config()
SEEN_PATH = ROOT / "seen.json"
LOG_PATH = ROOT / "watch.log"

INCLUDE = re.compile("|".join(CFG["include_keywords"]), re.I)
EXCLUDE = re.compile("|".join(CFG["exclude_keywords"]), re.I) if CFG.get("exclude_keywords") else None
ATS_REQUIRE = re.compile("|".join(CFG["ats_require"]), re.I) if CFG.get("ats_require") else None
CATS = [c.lower() for c in CFG.get("include_categories", [])]
MAX_AGE = CFG.get("max_age_days", 21) * 86400


# ---------------- http ----------------

def get_json(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def to_epoch(val):
    """Best-effort parse of the assorted date formats the sources return."""
    if not val:
        return 0
    if isinstance(val, (int, float)):
        return val / 1000 if val > 1e12 else val  # Lever uses ms
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


# ---------------- filtering ----------------

def keep(title, company="", category=None, ats=False):
    """Category match OR title keyword match, minus excludes.
    ATS sources additionally require an intern/co-op term (aggregator feeds
    are internship-only by construction, so they skip that gate)."""
    if EXCLUDE and EXCLUDE.search(f"{title} {company}"):
        return False
    if ats and ATS_REQUIRE and not ATS_REQUIRE.search(title):
        return False
    if category and CATS and any(c in category.lower() for c in CATS):
        return True
    return bool(INCLUDE.search(title))


def fresh(posted, now):
    """Keep if we don't know the age, or it's within the window."""
    return not (MAX_AGE and posted and now - posted > MAX_AGE)


# ---------------- sources ----------------

def listings_feed(repo):
    """The structured feed behind SimplifyJobs-style repos. Append :branch to
    override the default 'dev' branch."""
    branch = "dev"
    if ":" in repo:
        repo, branch = repo.split(":", 1)
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/.github/scripts/listings.json"
    now = time.time()
    for j in get_json(url, timeout=60):
        if not (j.get("active") and j.get("is_visible")):
            continue
        posted = to_epoch(j.get("date_posted") or j.get("date_updated"))
        if not fresh(posted, now):
            continue
        yield {
            "id": f"ls:{j.get('id')}",
            "company": j.get("company_name", "?"),
            "title": j.get("title", ""),
            "location": ", ".join(j.get("locations") or [])[:70],
            "url": j.get("url", ""),
            "category": j.get("category"),
            "posted": posted,
        }


def greenhouse(slug):
    now = time.time()
    for j in get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").get("jobs", []):
        posted = to_epoch(j.get("updated_at"))
        if not fresh(posted, now):
            continue
        yield {
            "id": f"gh:{slug}:{j['id']}", "company": slug, "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""), "category": None, "posted": posted,
        }


def lever(slug):
    now = time.time()
    for j in get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
        posted = to_epoch(j.get("createdAt"))
        if not fresh(posted, now):
            continue
        yield {
            "id": f"lv:{slug}:{j['id']}", "company": slug, "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "url": j.get("hostedUrl", ""), "category": None, "posted": posted,
        }


def ashby(slug):
    now = time.time()
    for j in get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").get("jobs", []):
        posted = to_epoch(j.get("publishedAt") or j.get("updatedAt"))
        if not fresh(posted, now):
            continue
        yield {
            "id": f"ab:{slug}:{j.get('id')}", "company": slug, "title": j.get("title", ""),
            "location": j.get("location", ""), "url": j.get("jobUrl", ""),
            "category": None, "posted": posted,
        }


def build_plan(include_feeds):
    """(fetch_fn, slug, is_ats) triples. ATS sources are the instant tier."""
    plan = []
    if include_feeds:
        plan += [(listings_feed, r, False) for r in CFG.get("listings_repos", [])]
    if os.environ.get("SKIP_ATS"):
        return plan  # a faster runner (e.g. the Cloudflare worker) owns the ATS tier
    plan += [(greenhouse, s, True) for s in CFG.get("greenhouse", [])]
    plan += [(lever, s, True) for s in CFG.get("lever", [])]
    plan += [(ashby, s, True) for s in CFG.get("ashby", [])]
    return plan


# ---------------- notify ----------------

def ntfy(title, body, click=None):
    topic = CFG.get("ntfy_topic", "")
    if not topic or topic.startswith("CHANGE-ME"):
        return
    headers = {**UA, "Title": title[:90].encode("ascii", "ignore").decode(),
               "Tags": "zap", "Priority": "high"}
    if click:
        headers["Click"] = click
        headers["Actions"] = f"view, Apply, {click}"
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, method="POST"
    )
    urllib.request.urlopen(req, timeout=20).read()


def sms(body):
    """Optional real SMS. Set TWILIO_SID / TWILIO_TOKEN / TWILIO_FROM / TWILIO_TO."""
    sid, tok = os.environ.get("TWILIO_SID"), os.environ.get("TWILIO_TOKEN")
    frm, to = os.environ.get("TWILIO_FROM"), os.environ.get("TWILIO_TO")
    if not all([sid, tok, frm, to]):
        return
    data = urllib.parse.urlencode({"From": frm, "To": to, "Body": body[:1500]}).encode()
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=data,
        headers={**UA, "Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=25).read()


def dispatch(found):
    """One push per role; SMS batches into a digest so you don't get 40 texts."""
    for job in found:
        try:
            ntfy(f"{job['company']}: {job['title'][:60]}",
                 f"{job['company']} - {job['title']}\n{job['location'] or 'location n/a'}",
                 job["url"])
        except Exception as e:
            print("WARN ntfy", repr(e)[:80], file=sys.stderr)

    n = len(found)
    if n <= CFG.get("sms_digest_threshold", 4):
        for job in found:
            sms(f"{job['company']}: {job['title']}\n{job['location']}\n{job['url']}")
    else:
        lines = [f"{j['company']}: {j['title'][:45]}" for j in found[:10]]
        extra = f"\n+{n - 10} more" if n > 10 else ""
        sms(f"{n} new hardware roles:\n" + "\n".join(lines) + extra)


# ---------------- core ----------------

def load_seen():
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8-sig")))
    return set()


def run_once(seen, include_feeds=True):
    first_run = not seen
    found, errors, scanned = [], [], 0
    for fn, slug, is_ats in build_plan(include_feeds):
        try:
            for job in fn(slug):
                scanned += 1
                if not job["url"] or job["id"] in seen:
                    continue
                seen.add(job["id"])
                if keep(job["title"], job["company"], job.get("category"), is_ats):
                    found.append(job)
        except Exception as e:
            errors.append(f"{fn.__name__}:{slug} -> {repr(e)[:90]}")
        time.sleep(0.3)

    found.sort(key=lambda j: -(j.get("posted") or 0))

    cap = CFG.get("max_alerts_per_run", 25)
    capped = False
    if not first_run and len(found) > cap:
        errors.append(f"capped alerts at {cap} (found {len(found)})")
        found, capped = found[:cap], True

    if not first_run and found:
        try:
            dispatch(found)
        except Exception as e:
            errors.append(f"notify -> {repr(e)[:90]}")

    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=0), encoding="utf-8")
    return found, errors, scanned, first_run, capped


def stamp():
    return datetime.now(timezone.utc).strftime("%m-%d %H:%M:%S")


def acquire_lock():
    """Cross-platform single-instance guard: bind a localhost port and hold it."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", CFG.get("lock_port", 47113)))
        s.listen(1)
        return s  # keep a reference so it isn't GC'd
    except OSError:
        print("jobwatch is already running (lock port busy). Exiting.")
        sys.exit(0)


def dry_run():
    """Print everything that currently matches, touching nothing. For tuning."""
    matches, scanned = [], 0
    for fn, slug, is_ats in build_plan(include_feeds=True):
        try:
            for job in fn(slug):
                scanned += 1
                if job["url"] and keep(job["title"], job["company"], job.get("category"), is_ats):
                    matches.append(job)
        except Exception as e:
            print("WARN", fn.__name__, slug, "->", repr(e)[:90], file=sys.stderr)
    matches.sort(key=lambda j: -(j.get("posted") or 0))
    print(f"{len(matches)} current matches out of {scanned} scanned:\n")
    for j in matches[:40]:
        print(f"  {j['company'][:24]:24} | {j['title'][:52]:52} | {j['location']}")
    if len(matches) > 40:
        print(f"  ... and {len(matches) - 40} more")


def daemon():
    lock = acquire_lock()  # noqa: F841 - held for process lifetime
    seen = load_seen()
    interval = CFG.get("poll_seconds", 60)
    feed_every = CFG.get("feed_poll_seconds", 900)
    last_feed = 0.0
    print(f"[{stamp()}] jobwatch daemon up. ATS tier every {interval}s, "
          f"aggregator feeds every {feed_every}s.")
    while True:
        include_feeds = last_feed == 0.0 or time.monotonic() - last_feed >= feed_every
        try:
            found, errors, scanned, first_run, capped = run_once(seen, include_feeds)
            if include_feeds:
                last_feed = time.monotonic()
            if first_run:
                print(f"[{stamp()}] SEEDED {len(seen)} existing postings, "
                      f"{len(found)} matched. No alerts on the seed pass.")
            elif found or errors or include_feeds:
                # feed cycles double as a ~15-min heartbeat so the log shows life
                note = ", capped" if capped else ""
                print(f"[{stamp()}] {len(found)} new / {scanned} scanned{note}")
                for j in found:
                    print(f"    PING {j['company']} | {j['title'][:55]}")
                for e in errors:
                    print(f"    WARN {e}")
        except Exception as e:
            print(f"[{stamp()}] loop error: {repr(e)[:120]}", file=sys.stderr)
        time.sleep(interval)


def main():
    if "--dry" in sys.argv:
        return dry_run()

    if "--watch" in sys.argv:
        # Daemon mode always logs to a file: under pythonw.exe there is no
        # console at all (sys.stdout is None), and a hidden console can't be
        # tailed. Rotate at 5 MB so it never grows unbounded.
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 5_000_000:
            LOG_PATH.replace(ROOT / "watch.log.1")
        logf = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = logf
        try:
            daemon()
        except KeyboardInterrupt:
            print(f"[{stamp()}] stopped.")
        return

    # --once: single pass for cron/CI/debugging
    seen = load_seen()
    found, errors, scanned, first_run, _ = run_once(seen)
    if first_run:
        print(f"SEEDED. Scanned {scanned}, tracking {len(seen)}, "
              f"{len(found)} would have matched. No alerts on first run.")
        for j in found[:25]:
            print("   ", j["company"], "|", j["title"], "|", j["location"])
    else:
        for j in found:
            print("PING", j["company"], "|", j["title"], "|", j["url"])
        print(f"{len(found)} new from {scanned} scanned, tracking {len(seen)}.")
    for e in errors:
        print("WARN", e, file=sys.stderr)


if __name__ == "__main__":
    main()
