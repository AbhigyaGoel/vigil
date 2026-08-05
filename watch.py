#!/usr/bin/env python3
"""
vigil - hardware internship tracker (wide-net tier).

Scans job sources once, pushes new matches to your phone via ntfy, and remembers
what it has seen so you're never pinged twice. This script is the WIDE-NET tier:
GitHub Actions runs it every few minutes over the aggregator feeds (the hidden
listings.json behind the SimplifyJobs-style repos, which holds ~10x more roles
than the rendered README). The INSTANT tier - polling company boards every
minute - is the Cloudflare Worker in cloudflare/; see the README.

Two sources:
  - Aggregator feeds  (listings.json)          -> hundreds of companies
  - Company ATS boards (Greenhouse/Lever/Ashby) -> instant, per-company

Set SKIP_ATS=1 to run feeds only (used on Actions once the worker owns the
board tier, so the same role never alerts from both).

Modes:
  python watch.py --once    single pass - what GitHub Actions runs
  python watch.py --dry     print current matches, notify nothing - for tuning

Stdlib only. No pip installs, no API keys, no accounts.
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
UA = {"User-Agent": "vigil/1.0 (github.com/AbhigyaGoel/vigil)"}


def load_config():
    # utf-8-sig tolerates the BOM PowerShell writes; config.local.json (gitignored)
    # overrides keys wholesale; NTFY_TOPIC env var beats both (Actions secret).
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    local = ROOT / "config.local.json"
    if local.exists():
        cfg.update(json.loads(local.read_text(encoding="utf-8-sig")))
    if os.environ.get("NTFY_TOPIC"):
        cfg["ntfy_topic"] = os.environ["NTFY_TOPIC"]
    return cfg


CFG = load_config()
SEEN_PATH = ROOT / "seen.json"

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
    """Category match OR title keyword match, minus excludes. ATS sources also
    require an intern/co-op term; aggregator feeds are internship-only already."""
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


def build_plan():
    """(fetch_fn, slug, is_ats) triples. SKIP_ATS drops the board tier when the
    Cloudflare worker owns it."""
    plan = [(listings_feed, r, False) for r in CFG.get("listings_repos", [])]
    if os.environ.get("SKIP_ATS"):
        return plan
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


def dispatch(found):
    """One ntfy push per new role."""
    for job in found:
        try:
            ntfy(f"{job['company']}: {job['title'][:60]}",
                 f"{job['company']} - {job['title']}\n{job['location'] or 'location n/a'}",
                 job["url"])
        except Exception as e:
            print("WARN ntfy", repr(e)[:80], file=sys.stderr)


# ---------------- core ----------------

def load_seen():
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8-sig")))
    return set()


def scan(seen):
    """One pass over every source. Adds new ids to `seen`, returns matches."""
    found, errors, scanned = [], [], 0
    for fn, slug, is_ats in build_plan():
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
    return found, errors, scanned


def main():
    dry = "--dry" in sys.argv

    if dry:
        seen = set()  # match everything, change nothing
        found, errors, scanned = scan(seen)
        print(f"{len(found)} current matches out of {scanned} scanned:\n")
        for j in found[:40]:
            print(f"  {j['company'][:24]:24} | {j['title'][:52]:52} | {j['location']}")
        if len(found) > 40:
            print(f"  ... and {len(found) - 40} more")
        for e in errors:
            print("WARN", e, file=sys.stderr)
        return

    seen = load_seen()
    first_run = not seen
    found, errors, scanned = scan(seen)

    cap = CFG.get("max_alerts_per_run", 25)
    if not first_run and len(found) > cap:
        errors.append(f"capped alerts at {cap} (found {len(found)})")
        found = found[:cap]

    if first_run:
        print(f"SEEDED. Scanned {scanned}, tracking {len(seen)}, "
              f"{len(found)} would have matched. No alerts on first run.")
    else:
        if found:
            try:
                dispatch(found)
            except Exception as e:
                errors.append(f"notify -> {repr(e)[:90]}")
        for j in found:
            print("PING", j["company"], "|", j["title"], "|", j["url"])
        print(f"{len(found)} new from {scanned} scanned, tracking {len(seen)}.")

    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=0), encoding="utf-8")
    for e in errors:
        print("WARN", e, file=sys.stderr)


if __name__ == "__main__":
    main()
