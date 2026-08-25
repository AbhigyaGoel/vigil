#!/usr/bin/env python3
"""
vigil - hardware internship tracker (wide-net / scoring tier).

Runs on GitHub Actions. Pulls the aggregator + Workday sources, filters with a
per-source policy, scores each survivor hardware-first, and splits into:
  Tier A -> instant ntfy push        (score >= 3, US, Summer-2027-eligible)
  Tier B -> hourly batch, low priority (everything else that survives)
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

# Curated priority: a hand-picked company's intern always DELIVERS, but only a
# hardware/robotics-relevant title earns instant Tier A. A clearly off-target
# function (IT, generic SWE, ML, biomedical) with no hardware signal routes to the
# Tier B hourly batch instead. Mirrors filters.mjs curatedRelevant (parity 'relevance').
CURATED_OFFTARGET = re.compile(
    r"\b(?:software|swe|full ?stack|front ?end|back ?end|web developer|information technology|"
    r"sys ?admin|systems? administrator|help ?desk|machine learning|ml|data scien|data analyst|"
    r"biomedical|clinical|finance|financial|business)\b", re.I)


def curated_relevant(title):
    if INCLUDE and INCLUDE.search(title or ""):   # explicit hardware/robotics signal
        return True
    return not CURATED_OFFTARGET.search(title or "")


CATS = [c.lower() for c in CFG.get("include_categories", [])]
SOFT_CATS = [c.lower() for c in CFG.get("soft_categories", [])]
DROP_SEASONS = set(CFG.get("season_drop_terms", []))
A_SEASONS = set(CFG.get("season_a_terms", []))
SCORING = [(w, re.compile(p, re.I)) for w, p in CFG.get("scoring", [])]
MAX_AGE = CFG.get("max_age_days", 21) * 86400
US_STATE = re.compile(r",\s*[A-Z]{2}(\b|$)")
US_NAME = re.compile(r"\b(united states|usa|u\.s\.a?\.)\b", re.I)
NEWGRAD = re.compile(r"new col|new ?grad|college grad|\bgraduate\b|university grad", re.I)

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

def base_score(text):
    return sum(w for w, r in SCORING if r.search(text))


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

# "2027 or later" / "and beyond" / "onwards" / "2027+" means I (2028) am eligible.
_ELIGIBLE_TAIL = re.compile(r"or later|and beyond|onwards?|or after|or above|and later|\+", re.I)
# Bachelor's, however postings actually write it: BS, B.S., BS/MS, BSc, BSEE, BSCS...
_HAS_BACH = re.compile(r"\bbachelor|\bundergrad|\bB\.?S\.?\b|\bBSc\b|\bBS[A-Z]{2,3}\b", re.I)


def extract_signals(desc):
    if not desc:
        return {}
    grad_bad = False
    m = re.search(r"(graduat|class of|degree by|complet\w+)[^.]{0,40}?\b20(25|26|27)\b([^.]{0,20})", desc, re.I)
    if m:
        eligible = _ELIGIBLE_TAIL.search(m.group(3) or "") or re.search(r"\b2028\b", desc)
        grad_bad = not eligible
    return {
        "clearance": bool(CLEARANCE and CLEARANCE.search(desc)),
        "hw": bool(DESC_HW and DESC_HW.search(desc)),
        "grad_bad": grad_bad,
        "grad_only": bool(re.search(r"\b(ph\.?d|doctoral|master)", desc, re.I)
                          and not _HAS_BACH.search(desc)),
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
    s = _HTML.sub("", s).replace("`", "")
    return s.replace("**", "").replace("__", "").strip(" *_")  # drop bold/italic markers


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


_WD_INTERN = re.compile(r"intern|co-?op|trainee|new coll|university|early career|student|graduate", re.I)


def workday(entry):
    """Discover the tenant's intern/co-op/new-grad workerSubType facet and page
    through ALL of it. searchText='intern' was fuzzy (relevance-ranked, 900 rows
    mostly senior) so a 100-cap silently hid roles ranked 100th+; the facet set is
    small and complete, so nothing is missed."""
    host, tenant, site = entry["host"], entry["tenant"], entry["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    name = entry.get("name", tenant)
    try:
        probe = post_json(api, {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    except Exception:
        return
    ids = [v.get("id") for f in probe.get("facets", []) if f.get("facetParameter") == "workerSubType"
           for v in f.get("values", []) if _WD_INTERN.search(v.get("descriptor", ""))]
    facets = {"workerSubType": ids} if ids else {}
    stext = "" if ids else "intern"   # fall back to fuzzy search only if no facet exists
    cap = 400 if ids else 100
    offset, total = 0, None
    while offset < cap:
        try:
            d = post_json(api, {"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": stext})
        except Exception:
            break
        posts = d.get("jobPostings", [])
        if total is None:
            total = d.get("total") or 0
        for p in posts:
            path = p.get("externalPath", "")
            yield {
                "id": f"wd:{tenant}:{path}", "company": name, "title": p.get("title", ""),
                "location": p.get("locationsText", ""), "url": f"https://{host}/{site}{path}",
                "category": None, "terms": None, "posted": 0, "src": "workday", "policy": "bulk",
                "detail": f"https://{host}/wday/cxs/{tenant}/{site}{path}",
            }
        offset += 20
        if not posts or offset >= total:
            break


_ATS_FN = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def discovered_boards(cfg):
    """Auto-discover NEW hardware/robotics company boards from the zshah101 registry
    and yield their jobs KEYWORD-GATED (policy='bulk'). Unlike the hand-curated instant
    tier, these companies aren't vetted, so a title must hit include_keywords to survive
    - noise costs only a fetch, not an alert. Slugs already curated are skipped (no
    double-alert with the worker); defense names are dropped by name_exclude/exclude_companies."""
    disc = cfg.get("discovery") or {}
    if not disc.get("enabled"):
        return
    inc = disc.get("name_include") or []
    exc = disc.get("name_exclude") or []
    name_inc = re.compile("|".join(inc), re.I) if inc else None
    name_exc = re.compile("|".join(exc), re.I) if exc else None
    if not name_inc or not disc.get("registry_url"):
        return
    have = (set(cfg.get("greenhouse", [])) | set(cfg.get("lever", []))
            | set(cfg.get("ashby", [])))
    try:
        reg = get_json(disc["registry_url"], timeout=30)
    except Exception as e:
        print(f"WARN discovery registry -> {repr(e)[:80]}", file=sys.stderr)
        return
    picked = []
    for d in reg:
        ats, slug, name = d.get("ats"), d.get("slug"), d.get("name", "")
        if ats not in _ATS_FN or not slug or slug in have:
            continue
        if not name_inc.search(name):
            continue
        if (name_exc and name_exc.search(name)) or (EXCLUDE_CO and EXCLUDE_CO.search(name)):
            continue
        picked.append((ats, slug, name))
    cap = disc.get("max_boards", 120)
    if len(picked) > cap:
        print(f"WARN discovery capped at {cap} (matched {len(picked)})", file=sys.stderr)
        picked = picked[:cap]
    for ats, slug, name in picked:
        try:
            for job in _ATS_FN[ats](slug):
                job["company"] = name     # real name (not slug) for the notification
                job["policy"] = "bulk"    # discovered != vetted -> keyword-gated, still Tier-A-able
                yield job
        except Exception as e:
            print(f"WARN discovery {ats}:{slug} -> {repr(e)[:60]}", file=sys.stderr)
        time.sleep(0.15)


def build_plan():
    plan = [(listings_feed, r) for r in CFG.get("listings_repos", [])]
    plan += [(markdown_source, s) for s in CFG.get("markdown_sources", [])]
    plan += [(atom_feed, s) for s in CFG.get("atom_feeds", [])]
    plan += [(workday, w) for w in CFG.get("workday", [])]  # bulk, needs enrichment -> stays here
    if not os.environ.get("SKIP_ATS"):  # curated boards; the worker owns these when SKIP_ATS=1
        plan += [(greenhouse, s) for s in CFG.get("greenhouse", [])]
        plan += [(lever, s) for s in CFG.get("lever", [])]
        plan += [(ashby, s) for s in CFG.get("ashby", [])]
    if (CFG.get("discovery") or {}).get("enabled"):  # keyword-gated auto-discovery (always on)
        plan += [(discovered_boards, CFG)]
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
        reason = "new-grad" if NEWGRAD.search(title) else "not-intern"
        note(f"DROP {reason}"); return "drop", reason
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
    bs, ds = base_score(title), desc_score(sig)
    score = bs + ds
    note(f"score={score} (title {bs}, desc {ds})")
    if policy == "curated":
        # hand-picked company: a vague US intern title is instant-worthy. But a hard
        # eligibility mismatch from the description still demotes (I graduate 2028).
        if geo != "us":
            note("TIER B (curated, non-US)"); return "B", score
        if sig.get("grad_bad") or sig.get("grad_only"):
            note("TIER B (curated, grad/degree mismatch)"); return "B", score
        if not curated_relevant(title):
            note("TIER B (curated, off-target function)"); return "B", score
        note("TIER A (curated, hardware-relevant)"); return "A", max(score, 3)
    if policy == "tagged":
        # SimplifyJobs carries explicit season terms -> trust them exactly.
        a_ok = season_a_eligible(job.get("terms"))
    else:
        # bulk/untagged (Workday, markdown, atom) have no structured terms, so a
        # strict term match would cap them at Tier B forever. A disqualifying season
        # was already dropped above; promote unless the description shows a hard
        # eligibility mismatch (I graduate 2028).
        a_ok = not (sig.get("grad_bad") or sig.get("grad_only"))
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


LOGIC_VERSION = "v2.7"  # bump when filter/scoring CODE changes -> forces a silent reseed
# v2.6: added zapplyjobs low-latency feeds + Tier-B prompt-push.
# v2.7: added registry-based auto-discovery of hardware/robotics boards; reseed so the
# ~49 discovered boards' backlog seeds silently instead of flooding on first scan.


def config_hash():
    keys = ["include_keywords", "exclude_keywords", "exclude_companies", "include_categories",
            "soft_categories", "tagged_subregex", "season_drop_terms", "season_a_terms",
            "season_title_drop", "scoring", "tier_b_countries", "drop_countries",
            "desc_hw_keywords", "desc_clearance", "ats_require", "max_age_days"]
    blob = json.dumps({"_v": LOGIC_VERSION, **{k: CFG.get(k) for k in keys}}, sort_keys=True)
    return sha256(blob.encode()).hexdigest()[:16]


# ---------------- notify ----------------

def ntfy(title, body, click=None, priority="high"):
    topic = CFG.get("ntfy_topic", "")
    if not topic or topic.startswith("CHANGE-ME"):
        return
    headers = {**UA, "Title": title[:140].encode("ascii", "ignore").decode(),
               "Priority": priority}
    if click:
        headers["Click"] = click
        headers["Actions"] = f"view, Apply, {click}"
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=20).read()


def fmt_age(posted):
    """'2d' / '5h', or '' when the source gave no real date (never fake a '0d')."""
    if not posted:
        return ""
    secs = time.time() - posted
    if secs < 0:
        return ""
    return f"{int(secs // 3600)}h" if secs < 86400 else f"{int(secs // 86400)}d"


def fmt_season(terms):
    """Prefer the term the role actually qualified on (a season_a term), so a role
    tagged [Fall 2026, Summer 2027] labels as Summer 2027, not its first tag."""
    if not terms:
        return ""
    tlist = terms if isinstance(terms, list) else [terms]
    return next((t for t in tlist if t in A_SEASONS), tlist[0])


def push_role(job, priority="high"):
    """One notification. Header = 'Company - Title' (never repeated in the body);
    body = location + posted-age / season / score, omitting whatever's unknown."""
    parts = []
    age = fmt_age(job.get("posted"))
    if age:
        parts.append(f"Posted {age} ago")
    season = fmt_season(job.get("terms"))
    if season:
        parts.append(season)
    parts.append(f"score {job.get('score', 0)}")
    body = (job.get("location") or "location n/a") + "\n" + " · ".join(parts)
    ntfy(f"{job['company']} - {job['title']}", body, click=job["url"], priority=priority)


def send_digest(roles):
    """Tier B batch (4+ roles): one line each, hardest-hitting first, cap 25."""
    roles = sorted(roles, key=lambda r: -r.get("score", 0))

    def line(r):
        bits = [r["company"], r["title"], r.get("location") or "?"]
        age = fmt_age(r.get("posted"))
        if age:
            bits.append(age)
        return " · ".join(bits)

    lines = [line(r) for r in roles[:25]]
    extra = f"\n+{len(roles) - 25} more" if len(roles) > 25 else ""
    ntfy(f"{len(roles)} new hardware roles", "\n".join(lines) + extra, priority="low")


def send_weekly_rejects(rejects, yld=None, since=None):
    import random
    by_rule = Counter(r["rule"] for r in rejects)
    season = Counter(r.get("tag", "?") for r in rejects if r["rule"] == "season")
    sample = random.sample(rejects, min(20, len(rejects))) if rejects else []
    body = []
    if yld:  # cumulative per-source scanned/A/B -> shows a source that never yields Tier A
        body.append(f"Yield since {since or 'seed'} (scanned/A/B):")
        body += [f"  {k}: {y['scanned']}/{y['A']}/{y['B']}" for k, y in sorted(yld.items())]
        body.append("")
    body.append("Drops by rule: " + ", ".join(f"{k}:{v}" for k, v in by_rule.most_common()))
    if season:
        body.append("Season drops by tag: " + ", ".join(f"{k}:{v}" for k, v in season.most_common()))
    body.append("")
    body += [f"[{r['rule']}] {r['company']}: {r['title'][:44]}" for r in sample]
    ntfy(f"vigil weekly: {len(rejects)} rejects, yield report", "\n".join(body) or "no data",
         priority="low")


# ---------------- run ----------------

def scan(seen, enrich, collect_rejects=False):
    """Full pass. Returns (tier_a, tier_b, rejects, stats). Adds matched ids to seen."""
    tier_a, tier_b, rejects = [], [], []
    stats = defaultdict(lambda: {"scanned": 0, "A": 0, "B": 0, "drop": Counter()})
    for fn, arg in build_plan():
        name = f"{fn.__name__}:{label(arg)}"
        # Workday is broken out per tenant so thin tenants are visible in the table.
        src_stat = stats[fn.__name__ if fn.__name__ != "workday" else f"workday:{label(arg)}"]
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
                       "location": job["location"], "url": job["url"], "score": val,
                       "posted": job.get("posted", 0), "terms": job.get("terms")}
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


def test_alert():
    """Exercise the real delivery path in the real format: one Tier A push + one
    Tier B digest (8 rows). Titles carry [TEST] so it can't be mistaken for real."""
    day = 86400
    a = {"company": "Nuro", "title": "[TEST] Embedded Systems Intern",
         "location": "Mountain View, CA", "url": "https://github.com/AbhigyaGoel/vigil",
         "score": 4, "posted": time.time() - 2 * day, "terms": ["Summer 2027"]}
    push_role(a, "high")
    print("sent Tier A test ->", a["title"])
    rows = [("Zipline", "Perception Intern", 5, 1), ("Rivian", "Vehicle Controls Intern", 4, 3),
            ("Nuro", "Embedded Systems Intern", 4, 2), ("Cobot", "Robotics Hardware Intern", 4, 0),
            ("Skydio", "Firmware Intern", 3, 5), ("1X", "Mechatronics Intern", 3, 4),
            ("Physical Intelligence", "Controls Intern", 3, 7), ("Figure", "Sensor Fusion Intern", 3, 6)]
    roles = [{"company": c, "title": f"[TEST] {t}", "location": "South San Francisco, CA",
              "url": f"https://github.com/AbhigyaGoel/vigil#{i}", "score": s,
              "posted": (time.time() - d * day) if d else 0, "terms": None}
             for i, (c, t, s, d) in enumerate(rows)]
    send_digest(roles)
    print(f"sent Tier B digest -> {len(roles)} rows")


def main():
    global ENRICH_FETCH
    if "--test-alert" in sys.argv:
        return test_alert()
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

    now = datetime.now(timezone.utc)
    today, week = now.strftime("%Y-%m-%d"), now.strftime("%Y-W%W")
    # yield PERSISTS across reseeds - the probation metric must survive LOGIC_VERSION bumps
    yld = state.get("yield", {})
    state.setdefault("yield_since", today)
    for k, s in stats.items():
        y = yld.setdefault(k, {"scanned": 0, "A": 0, "B": 0})
        y["scanned"] += s["scanned"]; y["A"] += s["A"]; y["B"] += s["B"]
    state["yield"] = yld

    if first_run:
        for r in tier_a:
            pending.pop(r["id"], None)  # seed silently, no pushes
        print(f"SEEDED{' (config changed)' if reseed else ''}. "
              f"tracking {len(seen)}, {len(tier_a)} A + {len(tier_b)} B suppressed.")
    else:
        cap = CFG.get("max_alerts_per_run", 25)
        for r in tier_a[:cap]:
            try:
                push_role(r, "high")
                print("PING-A", r["company"], "|", r["title"])
            except Exception as e:
                print("WARN push", repr(e)[:70], file=sys.stderr)
        if len(tier_a) > cap:
            print(f"WARN capped Tier A at {cap} (had {len(tier_a)})", file=sys.stderr)
        # Tier B split: a target-grade role (score>=prompt_bar) is only in Tier B
        # because geo/season is AMBIGUOUS (e.g. a scraped row with no location, like
        # Warp's robotics intern) - it shouldn't sit behind the hourly batch. Push it
        # NOW at low priority; the genuinely low-value remainder still batches.
        prompt_bar = CFG.get("tierb_prompt_score", 3)
        prompt_b = [r for r in tier_b if r.get("score", 0) >= prompt_bar]
        rest_b = [r for r in tier_b if r.get("score", 0) < prompt_bar]
        for r in sorted(prompt_b, key=lambda x: -x.get("score", 0))[:cap]:
            try:
                push_role(r, "low")
                print("PING-B*", r["company"], "|", r["title"], f"(score {r.get('score')})")
            except Exception as e:
                print("WARN push", repr(e)[:70], file=sys.stderr)
        for r in rest_b:
            pending[r["id"]] = r
        print(f"{len(tier_a)} Tier A pushed, {len(prompt_b)} high-score Tier B pushed now, "
              f"{len(rest_b)} batched ({len(pending)} pending).")

    # Tier B: at most once per hour, and only when there's something new (never an
    # empty digest). 1-3 roles go out as individual low-priority notifications so each
    # keeps its own Apply button; 4+ batch into a single low-priority list.
    if not first_run:
        if pending and time.time() - state.get("last_digest", 0) >= 3600:
            roles = list(pending.values())
            try:
                if len(roles) <= 3:
                    for r in sorted(roles, key=lambda x: -x.get("score", 0)):
                        push_role(r, "low")
                    print(f"Tier B: {len(roles)} sent individually")
                else:
                    send_digest(roles); print(f"Tier B digest: {len(roles)}")
                pending = {}; state["last_digest"] = time.time()
            except Exception as e:
                print("WARN digest", repr(e)[:70], file=sys.stderr)
        h = CFG.get("digest_hour_utc", 13)
        if (state.get("last_weekly") != week and now.weekday() >= CFG.get("weekly_day", 0)
                and now.hour >= h):
            try:
                send_weekly_rejects(rejects, yld, state.get("yield_since")); state["last_weekly"] = week
            except Exception as e:
                print("WARN weekly", repr(e)[:70], file=sys.stderr)

    # season-rate anomaly WARN: fire only on a sharp jump vs the trailing average,
    # so a steady 98% August doesn't cry wolf but an upstream tagging shift does.
    dtot = Counter()
    for s in stats.values():
        dtot.update(s["drop"])
    season_drops = dtot.get("season", 0)
    post_cat = sum(s["A"] + s["B"] for s in stats.values()) + season_drops
    if not first_run and post_cat:
        rate = season_drops / post_cat
        hist = state.get("season_rates", [])
        if len(hist) >= 12:
            avg = sum(hist) / len(hist)
            if rate > avg + 0.05:
                print(f"WARN season-drop rate {rate:.0%} vs {avg:.0%} trailing-avg "
                      f"(+{(rate - avg) * 100:.0f}pt) - upstream tagging shift?", file=sys.stderr)
        state["season_rates"] = (hist + [round(rate, 4)])[-48:]

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
