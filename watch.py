#!/usr/bin/env python3
"""
vigil - hardware internship tracker (wide-net / scoring tier).

Runs on GitHub Actions. Pulls the aggregator + Workday sources, filters with a
per-source policy, scores each survivor hardware-first, and splits into:
  Tier A -> instant ntfy push        (score >= 3, US, Summer-2027-eligible)
  Tier B -> one grouped digest a day (everything else that survives)
Curated company boards (Greenhouse/Lever/Ashby) get instant pushes from the
Cloudflare Worker instead, so they're skipped here when SKIP_ATS is set.

Design notes live in README + config.json. Rails (section F): dropping requires
positive evidence, ambiguity routes to Tier B, every drop is logged, and a
config-hash guard forces a silent reseed when filters change.

Modes:
  python watch.py --once           single pass (GitHub Actions)
  python watch.py --dry            per-source/per-tier table, sends nothing
  python watch.py --explain <id>   full decision trace for one job id

Stdlib only.
"""

import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).parent
UA = {"User-Agent": "vigil/2.0 (github.com/AbhigyaGoel/vigil)"}


def load_config():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    local = ROOT / "config.local.json"
    if local.exists():
        cfg.update(json.loads(local.read_text(encoding="utf-8-sig")))
    if os.environ.get("NTFY_TOPIC"):
        cfg["ntfy_topic"] = os.environ["NTFY_TOPIC"]
    return cfg


CFG = load_config()
SEEN_PATH = ROOT / "seen.json"
ENRICH_PATH = ROOT / "enrichment.json"
PENDING_PATH = ROOT / "pending_tierb.json"
STATE_PATH = ROOT / "state.json"
REJECTS_PATH = ROOT / "rejects.json"


def rx(key, default=None):
    v = CFG.get(key, default)
    if isinstance(v, list):
        v = "|".join(v)
    return re.compile(v, re.I) if v else None


INCLUDE = rx("include_keywords")
EXCLUDE = rx("exclude_keywords")
ATS_REQUIRE = rx("ats_require")
TAGGED_SUB = rx("tagged_subregex")
SEASON_TITLE = rx("season_title_drop")
CLEARANCE = rx("desc_clearance")
DESC_HW = rx("desc_hw_keywords")
TIER_B_CO = rx("tier_b_countries")
DROP_CO = rx("drop_countries")
# exclude_companies: wrap in word boundaries so 'axon' doesn't hit 'Axoni'.
_eco = CFG.get("exclude_companies") or []
EXCLUDE_CO = re.compile(r"\b(?:" + "|".join(_eco) + r")\b", re.I) if _eco else None

CATS = [c.lower() for c in CFG.get("include_categories", [])]
SOFT_CATS = [c.lower() for c in CFG.get("soft_categories", [])]
DROP_SEASONS = set(CFG.get("season_drop_terms", []))
A_SEASONS = set(CFG.get("season_a_terms", []))
SCORING = [(w, re.compile(p, re.I)) for w, p in CFG.get("scoring", [])]
MAX_AGE = CFG.get("max_age_days", 21) * 86400
US_STATE = re.compile(r",\s*[A-Z]{2}(\b|$)")
US_NAME = re.compile(r"\b(united states|usa|u\.s\.a?\.)\b", re.I)

ENRICH_FETCH = True      # network description fetch; off during seed and --dry
ENRICH_BUDGET = [60]     # max Workday/Simplify detail fetches per run (bounds runtime)


# ---------------- http ----------------

def get_json(url, timeout=45):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get_text(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def post_json(url, payload, timeout=25):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={**UA, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def to_epoch(val):
    if not val:
        return 0
    if isinstance(val, (int, float)):
        return val / 1000 if val > 1e12 else val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


# ---------------- season / geography ----------------

def season_dropped(terms, title):
    if title and SEASON_TITLE and SEASON_TITLE.search(title):
        return True
    if terms:
        tset = set(terms) if isinstance(terms, list) else {terms}
        if tset and tset <= DROP_SEASONS:
            return True
    return False


def season_a_eligible(terms):
    if not terms:
        return False  # ambiguous -> Tier B, never A
    tset = set(terms) if isinstance(terms, list) else {terms}
    return bool(tset & A_SEASONS)


def _seg_geo(seg):
    s = seg.strip()
    if not s:
        return "unknown"
    if DROP_CO and DROP_CO.search(s):
        return "drop"
    if TIER_B_CO and TIER_B_CO.search(s):
        return "tierb"
    if US_STATE.search(seg) or US_NAME.search(s):
        return "us"
    low = s.lower()
    if "remote" in low and ("us" in low or "united states" in low):
        return "us"
    return "unknown"


def geo_tier(location):
    """us | tierb | drop | unknown. Any US segment wins. Country name beats a
    2-letter code so 'Toronto, ON, Canada' is Canada, not a US state."""
    if not location:
        return "unknown"
    segs = re.split(r"\s*[/;|\n]\s*", location) or [location]
    tiers = [_seg_geo(s) for s in segs if s.strip()]
    if "us" in tiers:
        return "us"
    if "tierb" in tiers:
        return "tierb"
    if tiers and all(t == "drop" for t in tiers):
        return "drop"
    return "unknown"


# ---------------- scoring ----------------

def base_score(text, policy):
    s = sum(w for w, r in SCORING if r.search(text))
    if policy == "curated":
        s += 1  # generic engineering intern at a hand-picked company is a real signal
    return s


def desc_score(sig):
    s = 0
    if sig.get("hw"):
        s += 1
    if sig.get("grad_bad"):
        s -= 3
    if sig.get("grad_only"):
        s -= 3
    return s


# ---------------- description enrichment (stage 2) ----------------

def extract_signals(desc):
    if not desc:
        return {}
    early = re.search(r"(graduat|class of|degree by|complet\w+).{0,40}\b20(25|26|27)\b", desc, re.I)
    ok2028 = re.search(r"\b2028\b", desc)
    return {
        "clearance": bool(CLEARANCE and CLEARANCE.search(desc)),
        "hw": bool(DESC_HW and DESC_HW.search(desc)),
        "grad_bad": bool(early and not ok2028),
        "grad_only": bool(re.search(r"\b(ph\.?d|doctoral|master)", desc, re.I)
                          and not re.search(r"\bbachelor|\bundergrad|\bb\.s\.", desc, re.I)),
    }


def fetch_description(job, enrich):
    """Return extracted signals for a survivor, cached by id. GH/Lever/Ashby carry
    the description inline (job['desc']); only Workday + Simplify need a network
    fetch, which is gated (off at seed/dry) and budget-capped. A skipped or failed
    fetch returns {} and is NOT cached, so a later run can try again."""
    jid = job["id"]
    if jid in enrich:
        return enrich[jid]
    desc = job.get("desc") or ""          # inline for curated boards
    inline = bool(desc)
    if not desc and ENRICH_FETCH and ENRICH_BUDGET[0] > 0:
        try:
            if job.get("src") == "workday":
                ENRICH_BUDGET[0] -= 1
                d = get_json(job["detail"], timeout=20)
                desc = (d.get("jobPostingInfo") or {}).get("jobDescription", "") or ""
                time.sleep(0.4)           # rate-limit Workday
            elif job.get("src") == "listings" and job.get("url"):
                ENRICH_BUDGET[0] -= 1
                desc = get_text(job["url"], timeout=12)
        except Exception:
            desc = ""
    sig = extract_signals(desc) if desc else {}
    if desc or inline:                    # cache real results; never cache skipped-empties
        enrich[jid] = sig
    return sig


# ---------------- sources ----------------
# Each yields dicts with: id, company, title, location, url, category, terms,
# posted, src, policy, and (optionally) desc / detail.

def listings_feed(repo):
    branch = "dev"
    if ":" in repo:
        repo, branch = repo.split(":", 1)
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/.github/scripts/listings.json"
    for j in get_json(url, timeout=60):
        if CFG.get("require_active", True) and not (j.get("active") and j.get("is_visible")):
            continue
        yield {
            "id": f"ls:{j.get('id')}", "company": j.get("company_name", "?"),
            "title": j.get("title", ""), "location": ", ".join(j.get("locations") or [])[:120],
            "url": j.get("url", ""), "category": j.get("category"),
            "terms": j.get("terms"), "posted": to_epoch(j.get("date_posted") or j.get("date_updated")),
            "src": "listings", "policy": "tagged",
        }


_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML = re.compile(r"<[^>]+>")
_URL = re.compile(r'https?://[^\s)\]"\'<>]+')


def _strip_md(s):
    s = _MD_LINK.sub(r"\1", _MD_IMG.sub("", s))
    return _HTML.sub("", s).replace("`", "").strip()


def markdown_source(src):
    url = src["url"] if isinstance(src, dict) else src
    for line in get_text(url, timeout=45).splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        company, title = _strip_md(cells[0]), _strip_md(cells[1])
        if not company or not title or company.lower() == "company":
            continue
        urls = _URL.findall(cells[3]) or _URL.findall(cells[-1])
        if not urls:
            continue
        posted = 0
        if len(cells) >= 5:  # date column, e.g. "Aug 04 2026"
            try:
                posted = datetime.strptime(_strip_md(cells[4]), "%b %d %Y").replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                posted = 0
        yield {
            "id": f"md:{urls[-1]}", "company": company, "title": title,
            "location": _strip_md(cells[2])[:120], "url": urls[-1], "category": None,
            "terms": None, "posted": posted, "src": "markdown", "policy": "untagged",
        }


def atom_feed(url):
    ns = "{http://www.w3.org/2005/Atom}"
    for e in ET.fromstring(get_text(url, timeout=30)).findall(f"{ns}entry"):
        title = (e.findtext(f"{ns}title") or "").strip()
        link = e.find(f"{ns}link")
        u = link.get("href") if link is not None else ""
        if not title or not u:
            continue
        company = ""
        if ": " in title:
            company, title = (x.strip() for x in title.split(": ", 1))
        yield {
            "id": f"at:{u}", "company": company, "title": title, "location": "",
            "url": u, "category": None, "terms": None,
            "posted": to_epoch(e.findtext(f"{ns}updated")), "src": "atom", "policy": "untagged",
        }


def greenhouse(slug):
    for j in get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true").get("jobs", []):
        yield {
            "id": f"gh:{slug}:{j['id']}", "company": slug, "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""), "url": j.get("absolute_url", ""),
            "category": None, "terms": None, "posted": to_epoch(j.get("updated_at")),
            "src": "greenhouse", "policy": "curated", "desc": _HTML.sub(" ", j.get("content", "") or ""),
        }


def lever(slug):
    for j in get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
        yield {
            "id": f"lv:{slug}:{j['id']}", "company": slug, "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""), "url": j.get("hostedUrl", ""),
            "category": None, "terms": None, "posted": to_epoch(j.get("createdAt")),
            "src": "lever", "policy": "curated", "desc": j.get("descriptionPlain", "") or "",
        }


def ashby(slug):
    for j in get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").get("jobs", []):
        yield {
            "id": f"ab:{slug}:{j.get('id')}", "company": slug, "title": j.get("title", ""),
            "location": j.get("location", ""), "url": j.get("jobUrl", ""), "category": None,
            "terms": None, "posted": to_epoch(j.get("publishedAt") or j.get("updatedAt")),
            "src": "ashby", "policy": "curated", "desc": j.get("descriptionPlain", "") or "",
        }


def workday(entry):
    host, tenant, site = entry["host"], entry["tenant"], entry["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    name, offset = entry.get("name", tenant), 0
    while offset < 100:  # Workday caps limit at 20; page to ~100 newest intern rows/tenant
        try:
            d = post_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "intern"})
        except Exception:
            break
        posts = d.get("jobPostings", [])
        for p in posts:
            path = p.get("externalPath", "")
            yield {
                "id": f"wd:{tenant}:{path}", "company": name, "title": p.get("title", ""),
                "location": p.get("locationsText", ""), "url": f"https://{host}/{site}{path}",
                "category": None, "terms": None, "posted": 0, "src": "workday", "policy": "bulk",
                "detail": f"https://{host}/wday/cxs/{tenant}/{site}{path}",
            }
        offset += 20
        if not posts or offset >= (d.get("total") or 0):
            break


def build_plan():
    plan = [(listings_feed, r) for r in CFG.get("listings_repos", [])]
    plan += [(markdown_source, s) for s in CFG.get("markdown_sources", [])]
    plan += [(atom_feed, s) for s in CFG.get("atom_feeds", [])]
    plan += [(workday, w) for w in CFG.get("workday", [])]  # bulk, needs enrichment -> stays here
    if not os.environ.get("SKIP_ATS"):  # curated boards; the worker owns these when SKIP_ATS=1
        plan += [(greenhouse, s) for s in CFG.get("greenhouse", [])]
        plan += [(lever, s) for s in CFG.get("lever", [])]
        plan += [(ashby, s) for s in CFG.get("ashby", [])]
    return plan


def label(arg):
    return arg if isinstance(arg, str) else (arg.get("name") or arg.get("tenant") or arg.get("url", "?"))


# ---------------- classification pipeline ----------------

def classify(job, enrich, trace=None):
    """Returns ('A'|'B', score, None) or ('drop', rule, None). Ambiguity -> Tier B."""
    def note(msg):
        if trace is not None:
            trace.append(msg)

    policy, title, company = job["policy"], job.get("title", ""), job.get("company", "")
    blob = f"{title} {company}"
    note(f"source={job['src']} policy={policy} title={title!r} company={company!r}")

    # --- hard filters (only these may drop; each needs positive evidence) ---
    if policy != "tagged" and ATS_REQUIRE and not ATS_REQUIRE.search(title):
        note("DROP not-intern"); return "drop", "not-intern"
    note("pass intern-gate")
    if EXCLUDE and EXCLUDE.search(blob):
        note("DROP seniority/role"); return "drop", "seniority"
    if EXCLUDE_CO and company and EXCLUDE_CO.search(company):
        note("DROP defense (company)"); return "drop", "defense"
    if season_dropped(job.get("terms"), title):
        note(f"DROP season terms={job.get('terms')}"); return "drop", "season"
    note("pass season")
    geo = geo_tier(job.get("location"))
    if geo == "drop":
        note(f"DROP geo loc={job.get('location')!r}"); return "drop", "geo"
    note(f"geo={geo}")

    # --- relevance gate (policy-specific) ---
    if policy == "tagged":
        cat = (job.get("category") or "").lower()
        if any(c in cat for c in CATS):
            note(f"relevance: hardware category ({cat})")
        elif any(c in cat for c in SOFT_CATS) and TAGGED_SUB and TAGGED_SUB.search(title):
            note(f"relevance: soft category ({cat}) + sub-regex")
        else:
            note(f"DROP irrelevant (category={cat})"); return "drop", "irrelevant"
    elif policy in ("bulk", "untagged"):
        if not (INCLUDE and INCLUDE.search(title)):
            note("DROP irrelevant (no include_keywords)"); return "drop", "irrelevant"
        note("relevance: include_keywords")
    else:
        note("relevance: curated (no keyword gate)")

    if policy == "untagged" and MAX_AGE and job.get("posted") and time.time() - job["posted"] > MAX_AGE:
        note("DROP stale"); return "drop", "stale"

    # --- stage 2: description signals ---
    sig = fetch_description(job, enrich)
    if sig.get("clearance"):
        note("DROP defense (clearance in description)"); return "drop", "defense-desc"

    # --- score & tier ---
    bs, ds = base_score(title, policy), desc_score(sig)
    score = bs + ds
    note(f"score={score} (title+curated {bs}, desc {ds})")
    a_ok = season_a_eligible(job.get("terms"))
    if score >= 3 and geo == "us" and a_ok:
        note("TIER A"); return "A", score
    note(f"TIER B (score>=3:{score>=3} us:{geo=='us'} season_a:{a_ok})")
    return "B", score


# ---------------- state ----------------

def _load(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return default
    return default


def config_hash():
    keys = ["include_keywords", "exclude_keywords", "exclude_companies", "include_categories",
            "soft_categories", "tagged_subregex", "season_drop_terms", "season_a_terms",
            "season_title_drop", "scoring", "tier_b_countries", "drop_countries",
            "desc_hw_keywords", "desc_clearance", "ats_require", "max_age_days"]
    blob = json.dumps({k: CFG.get(k) for k in keys}, sort_keys=True)
    return sha256(blob.encode()).hexdigest()[:16]


# ---------------- notify ----------------

def ntfy(title, body, click=None, tags="zap", priority="high"):
    topic = CFG.get("ntfy_topic", "")
    if not topic or topic.startswith("CHANGE-ME"):
        return
    headers = {**UA, "Title": title[:100].encode("ascii", "ignore").decode(),
               "Tags": tags, "Priority": priority}
    if click:
        headers["Click"] = click
        headers["Actions"] = f"view, Apply, {click}"
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=20).read()


def push_tier_a(job, score):
    ntfy(f"[A/{score}] {job['company']}: {job['title'][:55]}",
         f"{job['company']} - {job['title']}\n{job['location'] or 'location n/a'}", job["url"])


def send_daily_digest(pending):
    roles = sorted(pending.values(), key=lambda r: -r.get("score", 0))
    cap = CFG.get("tierb_digest_max", 60)
    lines = [f"[{r.get('score',0)}] {r['company']}: {r['title'][:48]}" for r in roles[:cap]]
    extra = f"\n+{len(roles) - cap} more" if len(roles) > cap else ""
    ntfy(f"vigil daily: {len(roles)} Tier B roles",
         "\n".join(lines) + extra, tags="clipboard", priority="default")


def send_weekly_rejects(rejects):
    import random
    by_rule = Counter(r["rule"] for r in rejects)
    season = Counter(r.get("tag", "?") for r in rejects if r["rule"] == "season")
    sample = random.sample(rejects, min(20, len(rejects))) if rejects else []
    body = ["Drops by rule: " + ", ".join(f"{k}:{v}" for k, v in by_rule.most_common())]
    if season:
        body.append("Season drops by tag: " + ", ".join(f"{k}:{v}" for k, v in season.most_common()))
    body.append("")
    body += [f"[{r['rule']}] {r['company']}: {r['title'][:44]}" for r in sample]
    ntfy(f"vigil weekly: {len(rejects)} rejects sampled", "\n".join(body),
         tags="wastebasket", priority="low")


# ---------------- run ----------------

def scan(seen, enrich, collect_rejects=False):
    """Full pass. Returns (tier_a, tier_b, rejects, stats). Adds matched ids to seen."""
    tier_a, tier_b, rejects = [], [], []
    stats = defaultdict(lambda: {"scanned": 0, "A": 0, "B": 0, "drop": Counter()})
    for fn, arg in build_plan():
        name = f"{fn.__name__}:{label(arg)}"
        src_stat = stats[fn.__name__]
        try:
            for job in fn(arg):
                src_stat["scanned"] += 1
                if not job.get("url") or job["id"] in seen:
                    continue
                decision, val = classify(job, enrich)
                if decision == "drop":
                    src_stat["drop"][val] += 1
                    if collect_rejects:
                        tag = ""
                        t = job.get("terms")
                        if val == "season":
                            tag = (t[0] if isinstance(t, list) and t else (t or "title"))
                        rejects.append({"id": job["id"], "company": job["company"],
                                        "title": job["title"], "rule": val, "tag": tag})
                    continue
                seen.add(job["id"])
                rec = {"id": job["id"], "company": job["company"], "title": job["title"],
                       "location": job["location"], "url": job["url"], "score": val}
                if decision == "A":
                    src_stat["A"] += 1; tier_a.append(rec)
                else:
                    src_stat["B"] += 1; tier_b.append(rec)
        except Exception as e:
            print(f"WARN {name} -> {repr(e)[:90]}", file=sys.stderr)
        time.sleep(0.25)
    tier_a.sort(key=lambda r: -r["score"])
    return tier_a, tier_b, rejects, stats


def print_table(stats):
    print(f"{'source':14} {'scanned':>8} {'TierA':>6} {'TierB':>6}  drops")
    tot = {"scanned": 0, "A": 0, "B": 0}
    dtot = Counter()
    for src, s in sorted(stats.items()):
        d = ", ".join(f"{k}:{v}" for k, v in s["drop"].most_common())
        print(f"{src:14} {s['scanned']:>8} {s['A']:>6} {s['B']:>6}  {d}")
        tot["scanned"] += s["scanned"]; tot["A"] += s["A"]; tot["B"] += s["B"]
        dtot.update(s["drop"])
    print("-" * 60)
    print(f"{'TOTAL':14} {tot['scanned']:>8} {tot['A']:>6} {tot['B']:>6}  "
          + ", ".join(f"{k}:{v}" for k, v in dtot.most_common()))
    season_drops = dtot.get("season", 0)
    post_cat = tot["A"] + tot["B"] + season_drops
    if post_cat and season_drops / post_cat > 0.9:
        print(f"WARN season drops are {season_drops}/{post_cat} (>90%) of post-category candidates",
              file=sys.stderr)


def do_explain(job_id):
    enrich = _load(ENRICH_PATH, {})
    for fn, arg in build_plan():
        try:
            for job in fn(arg):
                if job["id"] == job_id:
                    trace = []
                    decision, val = classify(job, enrich, trace)
                    print(f"job {job_id}:")
                    for t in trace:
                        print("  ", t)
                    print(f"  => {decision} ({val})")
                    return
        except Exception as e:
            print(f"WARN {fn.__name__}:{label(arg)} -> {repr(e)[:80]}", file=sys.stderr)
    print(f"job id {job_id} not found in any current source")


def main():
    global ENRICH_FETCH
    if "--explain" in sys.argv:
        i = sys.argv.index("--explain")
        return do_explain(sys.argv[i + 1])

    if "--dry" in sys.argv:
        ENRICH_FETCH = False  # title-only for speed; no employer-site fetching
        _, _, _, stats = scan(set(), _load(ENRICH_PATH, {}), collect_rejects=False)
        print_table(stats)
        return

    # persistent state
    state = _load(STATE_PATH, {})
    enrich = _load(ENRICH_PATH, {})
    cur_hash = config_hash()
    reseed = state.get("config_hash") != cur_hash
    seen = set() if reseed else set(_load(SEEN_PATH, []))
    pending = {} if reseed else {k: v for k, v in _load(PENDING_PATH, {}).items()}
    first_run = reseed or not seen
    ENRICH_FETCH = not first_run  # skip the expensive description fetch on the silent seed
    ENRICH_BUDGET[0] = CFG.get("enrich_budget", 60)

    tier_a, tier_b, rejects, stats = scan(seen, enrich, collect_rejects=not first_run)

    if first_run:
        for r in tier_a:
            pending.pop(r["id"], None)  # seed silently, no pushes
        print(f"SEEDED{' (config changed)' if reseed else ''}. "
              f"tracking {len(seen)}, {len(tier_a)} A + {len(tier_b)} B suppressed.")
    else:
        cap = CFG.get("max_alerts_per_run", 25)
        for r in tier_a[:cap]:
            try:
                push_tier_a(r, r["score"])
                print("PING-A", r["company"], "|", r["title"])
            except Exception as e:
                print("WARN push", repr(e)[:70], file=sys.stderr)
        if len(tier_a) > cap:
            print(f"WARN capped Tier A at {cap} (had {len(tier_a)})", file=sys.stderr)
        for r in tier_b:
            pending[r["id"]] = r
        print(f"{len(tier_a)} Tier A pushed, {len(tier_b)} added to Tier B queue ({len(pending)} pending).")

    # scheduled digests (once per day / week, guarded by state)
    now = datetime.now(timezone.utc)
    today, week = now.strftime("%Y-%m-%d"), now.strftime("%Y-W%W")
    if not first_run and now.hour == CFG.get("digest_hour_utc", 13):
        if pending and state.get("last_daily") != today:
            try:
                send_daily_digest(pending); print(f"daily digest sent ({len(pending)})")
                pending = {}; state["last_daily"] = today
            except Exception as e:
                print("WARN daily", repr(e)[:70], file=sys.stderr)
        if now.weekday() == CFG.get("weekly_day", 0) and state.get("last_weekly") != week and rejects:
            try:
                send_weekly_rejects(rejects); state["last_weekly"] = week
            except Exception as e:
                print("WARN weekly", repr(e)[:70], file=sys.stderr)

    # persist
    state["config_hash"] = cur_hash
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=0), encoding="utf-8")
    PENDING_PATH.write_text(json.dumps(pending, indent=0), encoding="utf-8")
    STATE_PATH.write_text(json.dumps(state, indent=0), encoding="utf-8")
    ENRICH_PATH.write_text(json.dumps(enrich, indent=0), encoding="utf-8")
    REJECTS_PATH.write_text(json.dumps(rejects, indent=0), encoding="utf-8")  # current-run snapshot
    print_table(stats)


if __name__ == "__main__":
    main()
