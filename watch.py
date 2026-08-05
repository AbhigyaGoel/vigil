#!/usr/bin/env python3
"""
vigil - hardware internship tracker (wide-net tier).

Scans job sources once, pushes new matches to your phone via ntfy, and remembers
what it matched so you're never pinged twice. GitHub Actions runs this every few
minutes. The INSTANT tier (company boards polled every minute) is the Cloudflare
Worker in cloudflare/.

Sources (all public):
  - Aggregator listings.json  (SimplifyJobs-style repos; has terms + category)
  - Markdown-table repos       (e.g. IEEE-at-Cornell; scrapes Indeed/LinkedIn)
  - Atom feeds                 (e.g. zshah101)
  - Company ATS boards         (Greenhouse / Lever / Ashby)
  - Workday                    (NVIDIA, Micron, ADI, KLA, ... where big hardware
                                intern headcount actually lives)

SKIP_ATS=1 runs the aggregator/markdown/atom feeds only (used on Actions once the
worker owns the board + Workday tiers, so nothing alerts twice).

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
import xml.etree.ElementTree as ET
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


def _rx(key):
    vals = CFG.get(key)
    return re.compile("|".join(vals), re.I) if vals else None


INCLUDE = _rx("include_keywords")
EXCLUDE = _rx("exclude_keywords")
EXCLUDE_CO = _rx("exclude_companies")
ATS_REQUIRE = _rx("ats_require")
CATS = [c.lower() for c in CFG.get("include_categories", [])]
INCLUDE_TERMS = set(CFG.get("include_terms", []))
REQUIRE_ACTIVE = CFG.get("require_active", True)
MAX_AGE = CFG.get("max_age_days", 21) * 86400


# ---------------- http ----------------

def get_json(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get_text(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def post_json(url, payload, timeout=25):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={**UA, "Content-Type": "application/json"}, method="POST")
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
    """Category match OR title keyword match, minus title and company excludes.
    ATS/Workday sources also require an intern/co-op/new-grad term in the title;
    aggregator feeds are internship-only already."""
    if EXCLUDE and EXCLUDE.search(f"{title} {company}"):
        return False
    if EXCLUDE_CO and company and EXCLUDE_CO.search(company):
        return False
    if ats and ATS_REQUIRE and not ATS_REQUIRE.search(title):
        return False
    if category and CATS and any(c in category.lower() for c in CATS):
        return True
    return bool(INCLUDE and INCLUDE.search(title))


def fresh(posted, now):
    """Keep if we don't know the age, or it's within the window."""
    return not (MAX_AGE and posted and now - posted > MAX_AGE)


def terms_ok(terms):
    """Keep listings whose season is wanted. Rows with no terms field (some
    repos omit it) are always kept - we can't season-filter what isn't tagged."""
    if not INCLUDE_TERMS or not terms:
        return True
    tset = set(terms) if isinstance(terms, list) else {terms}
    return bool(tset & INCLUDE_TERMS)


# ---------------- markdown helpers ----------------

_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML = re.compile(r"<[^>]+>")
_URL = re.compile(r'https?://[^\s)\]"\'<>]+')


def strip_md(s):
    s = _MD_IMG.sub("", s)
    s = _MD_LINK.sub(r"\1", s)
    s = _HTML.sub("", s)
    return s.replace("`", "").strip()


def last_url(s):
    m = _URL.findall(s)
    return m[-1] if m else ""


# ---------------- sources ----------------

def listings_feed(repo):
    """Structured feed behind SimplifyJobs-style repos. Append :branch to override
    the default 'dev' branch."""
    branch = "dev"
    if ":" in repo:
        repo, branch = repo.split(":", 1)
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/.github/scripts/listings.json"
    now = time.time()
    for j in get_json(url, timeout=60):
        if REQUIRE_ACTIVE and not (j.get("active") and j.get("is_visible")):
            continue
        if not terms_ok(j.get("terms")):
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


def markdown_source(src):
    """Parse a README with | Company | Role | Location | Apply | ... | rows."""
    url = src["url"] if isinstance(src, dict) else src
    for line in get_text(url).splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        company, title = strip_md(cells[0]), strip_md(cells[1])
        if not company or not title or company.lower() == "company":
            continue
        apply_url = last_url(cells[3]) or last_url(cells[-1])
        if not apply_url:
            continue
        yield {
            "id": f"md:{apply_url}", "company": company, "title": title,
            "location": strip_md(cells[2])[:70], "url": apply_url,
            "category": None, "posted": 0,
        }


def atom_feed(url):
    """Parse an Atom feed. Entry titles look like 'Company: Role'."""
    ns = "{http://www.w3.org/2005/Atom}"
    for e in ET.fromstring(get_text(url)).findall(f"{ns}entry"):
        title = (e.findtext(f"{ns}title") or "").strip()
        link = e.find(f"{ns}link")
        url_ = link.get("href") if link is not None else ""
        if not title or not url_:
            continue
        company = ""
        if ": " in title:
            company, title = (x.strip() for x in title.split(": ", 1))
        yield {
            "id": f"at:{url_}", "company": company, "title": title,
            "location": "", "url": url_, "category": None,
            "posted": to_epoch(e.findtext(f"{ns}updated")),
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


def workday(entry):
    """Workday cxs API. entry = {name, tenant, host, site}. This is where NVIDIA,
    Micron, ADI, KLA etc. post - searchText 'intern' keeps the volume sane."""
    host, tenant, site = entry["host"], entry["tenant"], entry["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    name = entry.get("name", tenant)
    for offset in (0, 20, 40):
        try:
            d = post_json(api, {"appliedFacets": {}, "limit": 20,
                                "offset": offset, "searchText": "intern"})
        except Exception:
            break
        posts = d.get("jobPostings", [])
        for p in posts:
            path = p.get("externalPath", "")
            yield {
                "id": f"wd:{tenant}:{path}", "company": name,
                "title": p.get("title", ""), "location": p.get("locationsText", ""),
                "url": f"https://{host}/{site}{path}", "category": None, "posted": 0,
            }
        if not posts or offset + 20 >= (d.get("total") or 0):
            break


def build_plan():
    """(fetch_fn, arg, is_ats). Feed tier always runs; the ATS/Workday tier is
    dropped when SKIP_ATS is set (the Cloudflare worker owns it)."""
    plan = [(listings_feed, r, False) for r in CFG.get("listings_repos", [])]
    plan += [(markdown_source, s, False) for s in CFG.get("markdown_sources", [])]
    plan += [(atom_feed, s, False) for s in CFG.get("atom_feeds", [])]
    if os.environ.get("SKIP_ATS"):
        return plan
    plan += [(greenhouse, s, True) for s in CFG.get("greenhouse", [])]
    plan += [(lever, s, True) for s in CFG.get("lever", [])]
    plan += [(ashby, s, True) for s in CFG.get("ashby", [])]
    plan += [(workday, w, True) for w in CFG.get("workday", [])]
    return plan


def label(arg):
    if isinstance(arg, str):
        return arg
    return arg.get("name") or arg.get("tenant") or arg.get("url", "?")


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
        f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, method="POST")
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
    """One pass over every source. Only matched roles are added to `seen`, so the
    state file stays small even when a source has tens of thousands of rows."""
    found, errors, scanned = [], [], 0
    for fn, arg, is_ats in build_plan():
        try:
            for job in fn(arg):
                scanned += 1
                if not job["url"] or job["id"] in seen:
                    continue
                if keep(job["title"], job["company"], job.get("category"), is_ats):
                    seen.add(job["id"])
                    found.append(job)
        except Exception as e:
            errors.append(f"{fn.__name__}:{label(arg)} -> {repr(e)[:90]}")
        time.sleep(0.3)
    found.sort(key=lambda j: -(j.get("posted") or 0))
    return found, errors, scanned


def main():
    if "--dry" in sys.argv:
        found, errors, scanned = scan(set())
        print(f"{len(found)} current matches out of {scanned} scanned:\n")
        for j in found[:40]:
            print(f"  {j['company'][:22]:22} | {j['title'][:50]:50} | {j['location']}")
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
        print(f"SEEDED. Scanned {scanned}, tracking {len(seen)} matches. No alerts on first run.")
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
