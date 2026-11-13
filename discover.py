#!/usr/bin/env python3
"""
vigil - fund-portfolio discovery (monthly, report-only).

Separate from watch.py and the Worker: nothing here is on the alert path and it
never writes config.json. For each fund in discovery.json it pulls the portfolio
(Getro / Consider job-board JSON, else the fund's portfolio page), keeps
robotics/hardware/autonomy companies, resolves each one's ATS board
(Greenhouse/Lever/Ashby/Workday) and verifies it LIVE (HTTP 200 with >= 1 job).
The output is a report of new verified slugs (not already in config.json) plus
unresolved companies; the monthly Action opens a PR with it for manual approval.

  python discover.py            write discovery/report.{md,json}
  python discover.py --limit 5  only the first 5 kept companies per fund (smoke run)

Stdlib only.
"""
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
UA = {"User-Agent": "Mozilla/5.0 (vigil-discover; github.com/AbhigyaGoel/vigil)",
      "Accept": "application/json, text/html;q=0.9"}
GETRO_API = "https://api.getro.com/api/v2/collections"
PAGE_CAP = 100   # max pages walked per fund listing (Getro serves 12 companies/page)


# ---------------- http ----------------

def fetch(url, data=None, headers=None, opener=None, timeout=25):
    """(status, text). Never raises: network errors come back as (error-string, '')."""
    body = json.dumps(data).encode() if data is not None else None
    hdrs = {**UA, **({"Content-Type": "application/json"} if body else {}), **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body else "GET")
    try:
        with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return repr(e)[:80], ""


def fetch_json(url, data=None, **kw):
    status, text = fetch(url, data, **kw)
    if status != 200:
        raise RuntimeError(f"{url} -> {status}")
    return json.loads(text)


def parse_json(text, default=None):
    """json.loads that treats a non-JSON 200 (rate-limit interstitial, HTML redirect) as no data."""
    try:
        return json.loads(text)
    except ValueError:
        return default


def company(name, fund, domain="", desc="", tags=(), hints=None):
    return {"name": (name or "").strip(), "funds": [fund], "domain": clean_domain(domain),
            "desc": (desc or "").strip(), "tags": list(tags), "hints": hints}


def clean_domain(d):
    d = re.sub(r"^https?://", "", (d or "").strip().lower())
    return d.split("/")[0].removeprefix("www.")


# ---------------- fund sources ----------------

def getro_network(host):
    status, text = fetch(f"https://{host}/companies")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if status != 200 or not m:
        raise RuntimeError(f"https://{host}/companies -> {status}, no __NEXT_DATA__")
    return json.loads(m.group(1))["props"]["pageProps"]["network"]["id"]


def getro_fund(fund):
    net = getro_network(fund["host"])
    out, page, count = [], 0, None
    while page < PAGE_CAP:
        d = fetch_json(f"{GETRO_API}/{net}/search/companies",
                       {"hitsPerPage": 100, "page": page, "filters": {}, "query": ""})
        res = d.get("results") or {}
        rows = res.get("companies") or []
        count = res.get("count", count)
        for c in rows:
            tags = (c.get("industry_tags") or []) + (c.get("topics") or [])
            out.append(company(c.get("name", ""), fund["name"], c.get("domain", ""),
                               c.get("description", ""), tags, ("getro", net, c.get("id"))))
        page += 1
        if not rows or (count is not None and len(out) >= count):
            break
    return out


def consider_session(host):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, text = fetch(f"https://{host}/companies", opener=op)
    tok = re.search(r'csrfToken":"([^"]+)"', text)
    board = re.search(r'"board":(\{[^{}]*\})', text)
    if status != 200 or not tok or not board:
        raise RuntimeError(f"https://{host}/companies -> {status}, no csrfToken/board")
    return {"host": host, "opener": op, "headers": {"x-csrf-token": tok.group(1)},
            "board": json.loads(board.group(1))}


def consider_post(sess, endpoint, payload):
    return fetch_json(f"https://{sess['host']}/api-boards/{endpoint}", {**payload, "board": sess["board"]},
                      headers=sess["headers"], opener=sess["opener"])


def consider_fund(fund):
    sess = consider_session(fund["host"])
    out, meta = [], {"size": 100}
    for _ in range(PAGE_CAP):
        d = consider_post(sess, "search-companies", {"meta": meta, "query": {}})
        rows = d.get("companies") or []
        for c in rows:
            out.append(company(c.get("name", ""), fund["name"], c.get("domain") or c.get("website", ""),
                               c.get("description", ""), c.get("markets") or [], ("consider", sess, c.get("id"))))
        seq = (d.get("meta") or {}).get("sequence")
        if not rows or not seq or len(out) >= d.get("total", 0):
            break
        meta = {"size": 100, "sequence": seq}
    return out


def jsonld_fund(fund):
    status, text = fetch(fund["url"])
    if status != 200:
        raise RuntimeError(f"{fund['url']} -> {status}")
    out = []
    for blob in re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S):
        doc = parse_json(blob, {})
        for node in doc.get("@graph", [doc]):
            if node.get("@type") == "Organization" and (node.get("funder") or {}).get("@id") == fund["funder_id"]:
                out.append(company(node.get("name", ""), fund["name"], node.get("url", ""), node.get("description", "")))
    if not out:
        raise RuntimeError(f"{fund['url']}: no JSON-LD Organization nodes funded by {fund['funder_id']}")
    return out


_WF_ITEM = re.compile(r'class="investments-item"><a href="([^"]*)".*?<img alt="([^"]*)"[^>]*class="investments-logo"'
                      r'.*?<p[^>]*class="paragraph-0-75">(.*?)</p>', re.S)


def webflow_fund(fund):
    seen_pages, queue, out = set(), [""], {}
    while queue and len(seen_pages) < PAGE_CAP:
        q = queue.pop(0)
        seen_pages.add(q)
        status, text = fetch(fund["url"] + q)
        if status != 200:
            raise RuntimeError(f"{fund['url'] + q} -> {status}")
        for href, name, desc in _WF_ITEM.findall(text):
            out.setdefault(name, company(name, fund["name"], href, re.sub(r"<[^>]+>", " ", desc)))
        for nxt in set(re.findall(r'href="(\?[a-z0-9]+_page=\d+)"', text)):
            if nxt not in seen_pages and nxt not in queue:
                queue.append(nxt)
    if not out:
        raise RuntimeError(f"{fund['url']}: no Webflow investments-item entries (markup changed?)")
    return list(out.values())


FUND_KINDS = {"getro": getro_fund, "consider": consider_fund, "jsonld": jsonld_fund, "webflow": webflow_fund}


# ---------------- ATS links + probes ----------------

_ATS_LINK = [
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(eu\.)?lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.%-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("workday", re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
]
_NOT_SLUGS = {"embed", "v1", "api", "jobs", "wday", "job_app", "careers", "en-us"}
# ATSes vigil has no adapter for. Seen in fund-board job URLs / careers pages, they explain an
# unresolved company ("uses Gem") instead of a bare "not found".
_OTHER_ATS = {"gem": r"jobs\.gem\.com", "workable": r"apply\.workable\.com", "rippling": r"ats\.rippling\.com",
              "bamboohr": r"\.bamboohr\.com", "smartrecruiters": r"smartrecruiters\.com", "jobvite": r"jobvite\.com",
              "icims": r"\.icims\.com", "breezy": r"\.breezy\.hr", "recruitee": r"\.recruitee\.com",
              "teamtailor": r"\.teamtailor\.com", "pinpoint": r"\.pinpointhq\.com", "dover": r"app\.dover\.(?:io|com)",
              "jazzhr": r"applytojob\.com", "paylocity": r"recruiting\.paylocity\.com", "adp": r"workforcenow\.adp\.com",
              "successfactors": r"successfactors\.com", "oracle": r"oraclecloud\.com/hcmUI", "wellfound": r"wellfound\.com/"}
_OTHER_ATS_RX = {k: re.compile(v, re.I) for k, v in _OTHER_ATS.items()}


def other_ats(text):
    return sorted(k for k, rx in _OTHER_ATS_RX.items() if rx.search(text or ""))


def ats_links(text):
    """Every (ats, slug) referenced in a blob of HTML/URLs, first-seen order. Lever EU -> 'slug@eu';
    Workday -> 'tenant|host|site'."""
    found = []
    for ats, rx in _ATS_LINK:
        for m in rx.finditer(text or ""):
            if ats == "lever":
                slug = m.group(2).lower() + ("@eu" if m.group(1) else "")
            elif ats == "workday":
                tenant, wd, site = m.group(1).lower(), m.group(2).lower(), m.group(3)
                slug = f"{tenant}|{tenant}.{wd}.myworkdayjobs.com|{site}"
            else:
                slug = m.group(1).lower()
            if slug.split("@")[0].split("|")[0] in _NOT_SLUGS or (ats, slug) in found:
                continue
            found.append((ats, slug))
    return found


def name_guesses(c):
    base = re.sub(r"\b(inc|llc|corp|co|ltd|technologies|technology|labs?|ai)\b\.?", " ", c["name"].lower())
    words = re.findall(r"[a-z0-9]+", base) or re.findall(r"[a-z0-9]+", c["name"].lower())
    full = re.findall(r"[a-z0-9]+", c["name"].lower())
    stem = c["domain"].split(".")[0] if c["domain"] else ""
    cands = ["".join(full), "-".join(full), "".join(words), "-".join(words), stem]
    return [s for i, s in enumerate(cands) if s and s not in cands[:i]]


def probe(ats, slug):
    """(job_count, board_name, job_text) when the board answers 200 with >= 1 job, else (0, reason, '').
    job_text (titles + descriptions) is what an unconfirmed name-guess slug is checked against."""
    if ats == "workday":
        tenant, host, site = slug.split("|")
        status, text = fetch(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                             {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        n = (parse_json(text, {}) or {}).get("total") or 0 if status == 200 else 0
        return (n, "", "") if n else (0, f"HTTP {status}" if status != 200 else "0 jobs", "")
    s, _, region = slug.partition("@")
    url = {"greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs?content=true",
           "lever": f"https://api.{'eu.' if region else ''}lever.co/v0/postings/{s}?mode=json",
           "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{s}"}.get(ats)
    if not url:
        return 0, "unsupported", ""
    status, text = fetch(url)
    if status != 200:
        return 0, f"HTTP {status}", ""
    data = parse_json(text)
    if data is None:
        return 0, "200 but not JSON", ""
    # Lever answers a bare list; anything else (an error object) is not a board.
    if ats == "lever":
        jobs = data if isinstance(data, list) else []
    else:
        jobs = (data.get("jobs") or []) if isinstance(data, dict) else []
    if not jobs:
        return 0, "0 jobs", ""
    name = ""
    if ats == "greenhouse":
        meta_status, meta = fetch(f"https://boards-api.greenhouse.io/v1/boards/{s}")
        name = (parse_json(meta, {}) or {}).get("name", "") if meta_status == 200 else ""
    return len(jobs), name, text


_SUFFIX = re.compile(r"\b(inc|llc|corp|corporation|co|company|ltd|technologies|technology|labs?|ai|robotics|systems)\b\.?", re.I)


def owns_board(c, board_name, job_text):
    """Positive evidence a GUESSED slug is this company's board, not a namesake: the board's
    Greenhouse name matches, the company's domain appears in its postings, or a distinctive
    (8+ char) company name does."""
    core = _norm(_SUFFIX.sub(" ", c["name"])) or _norm(c["name"])
    if board_name and _norm(_SUFFIX.sub(" ", board_name)) == core:
        return True
    low = job_text.lower()
    if c["domain"] and c["domain"] in low:
        return True
    return len(_norm(c["name"])) >= 8 and c["name"].lower() in low


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def board_job_urls(c):
    """The fund board's own job URLs for this company (strongest evidence of its ATS)."""
    h = c.get("hints")
    if not h:
        return ""
    try:
        if h[0] == "getro":
            d = fetch_json(f"{GETRO_API}/{h[1]}/search/jobs",
                           {"hitsPerPage": 20, "page": 0, "filters": {"organization.id": [h[2]]}, "query": ""})
            urls = [j.get("url", "") for j in (d.get("results") or {}).get("jobs") or []]
        else:
            d = consider_post(h[1], "search-jobs", {"meta": {"size": 20}, "query": {"companies": [h[2]]}})
            urls = [j.get("url", "") for j in d.get("jobs") or []]
    except Exception:
        return ""
    return " ".join(urls)


def careers_html(c):
    if not c["domain"]:
        return ""
    pages = [fetch(f"https://{c['domain']}{path}", timeout=15) for path in ("/careers", "/jobs", "/")]
    return "\n".join(text for status, text in pages if status == 200)


def resolve(c):
    """Try board-hint, careers-page and name-guess candidates. Returns (verified boards, failed
    candidates, unsupported ATSes seen)."""
    hint_text, careers_text = board_job_urls(c), careers_html(c)
    cands = [(a, s, "fund-board job link") for a, s in ats_links(hint_text)]
    cands += [(a, s, "careers-page link") for a, s in ats_links(careers_text)]
    cands += [(a, s, "name guess") for s in name_guesses(c) for a in ("greenhouse", "lever", "ashby")]
    verified, tried, seen = [], [], set()
    for ats, slug, how in cands:
        if (ats, slug) in seen or any(v["ats"] == ats for v in verified):
            continue
        seen.add((ats, slug))
        n, info, job_text = probe(ats, slug)
        if not n:
            if how != "name guess":
                tried.append(f"{ats}:{slug} ({how}) -> {info}")
            continue
        if how == "name guess" and not owns_board(c, info, job_text):
            tried.append(f"{ats}:{slug} (name guess) -> live, but nothing ties it to this company"
                         + (f" (board is '{info}')" if info else ""))
            continue
        verified.append({"ats": ats, "slug": slug, "jobs": n, "evidence": how,
                         **({"board_name": info} if info else {})})
        if how != "name guess":
            break   # a link the company itself publishes is authoritative; stop probing guesses
    return verified, tried, other_ats(hint_text + " " + careers_text)


# ---------------- config diff ----------------

def configured(cfg):
    have = {(a, s.split("@")[0].lower()) for a in ("greenhouse", "lever", "ashby") for s in cfg.get(a, [])}
    have |= {("workday", w.get("tenant", "").lower()) for w in cfg.get("workday", [])}
    return have


def config_key(ats, slug):
    return (ats, slug.split("|")[0].split("@")[0].lower())


def keyword_rx(disc):
    return re.compile("|".join(disc["keywords"]), re.I), {t.lower() for t in disc.get("tags", [])}


def match_reason(c, rx):
    """Why a company is kept: the matched keyword or industry tag, or None (dropped)."""
    words, tags = rx
    m = words.search(f"{c['name']} {c['desc']}")
    if m:
        return m.group(0)
    return next((f"tag:{t}" for t in c["tags"] if t.lower() in tags), None)


def merge_companies(rows):
    """One record per company across funds (same domain, else same normalized name)."""
    merged = {}
    for c in rows:
        key = c["domain"] or _norm(c["name"])
        if key in merged:
            m = merged[key]
            m["funds"] = sorted(set(m["funds"]) | set(c["funds"]))
            m["hints"] = m["hints"] or c["hints"]
        else:
            merged[key] = dict(c)
    return list(merged.values())


def diff(results, have):
    new, existing, unresolved = [], [], []
    for r in results:
        if not r["verified"]:
            unresolved.append(r)
            continue
        fresh = [v for v in r["verified"] if config_key(v["ats"], v["slug"]) not in have]
        (new if fresh else existing).append({**r, "verified": fresh or r["verified"]})
    return new, existing, unresolved


# ---------------- report ----------------

def snippet(new):
    out = {"greenhouse": [], "lever": [], "ashby": [], "workday": []}
    for r in new:
        for v in r["verified"]:
            if v["ats"] == "workday":
                tenant, host, site = v["slug"].split("|")
                out["workday"].append({"name": r["name"], "tenant": tenant, "host": host, "site": site})
            else:
                out[v["ats"]].append(v["slug"])
    return {k: v for k, v in out.items() if v}


def cell(v):
    """Markdown table cell: a '|' in a name or error string would shift the columns."""
    return re.sub(r"\s*\n\s*", " ", str(v)).replace("|", r"\|")


def render_md(funds, new, existing, unresolved, when):
    L = [f"# Portfolio discovery - {when}", "",
         "Report only: nothing was written to `config.json`. Copy the slugs you approve by hand.", "",
         "## Funds", "", "| Fund | Source | Status | Companies | Kept |", "|---|---|---|---|---|"]
    for f in funds:
        L.append(f"| {cell(f['name'])} | {f['kind']} | {'ok' if f['ok'] else 'FAILED: ' + cell(f['error'])} "
                 f"| {f.get('total', 0)} | {f.get('kept', 0)} |")
    L += ["", f"## New verified boards ({len(new)})", "",
          "Each board answered HTTP 200 with at least 1 open job. `name guess` = slug derived from the company "
          "name; kept only when the board's postings name the company's domain (or a distinctive full name), "
          "or its Greenhouse board name matches. Still worth a glance before adding.", "",
          "| Company | Funds | Matched | ATS:slug | Jobs | Evidence |", "|---|---|---|---|---|---|"]
    for r in new:
        for v in r["verified"]:
            slug = v["slug"].replace("|", " / ")
            note = f" (board: {v['board_name']})" if v.get("board_name") else ""
            L.append(f"| {cell(r['name'])} | {cell(', '.join(r['funds']))} | {cell(r['matched'])} "
                     f"| `{v['ats']}:{slug}` | {v['jobs']} | {v['evidence']}{cell(note)} |")
    if new:
        L += ["", "Config snippet (merge into the matching lists):", "", "```json",
              json.dumps(snippet(new), indent=2), "```"]
    L += ["", f"## Unresolved ({len(unresolved)})", "",
          "Kept by the keyword filter, but no Greenhouse/Lever/Ashby/Workday board verified.", "",
          "| Company | Funds | Matched | Domain | Why unresolved |", "|---|---|---|---|---|"]
    for r in unresolved:
        why = "; ".join(r["tried"]) or "no ATS link on the fund board or careers page; name guesses all failed"
        L.append(f"| {cell(r['name'])} | {cell(', '.join(r['funds']))} | {cell(r['matched'])} "
                 f"| {r['domain'] or '-'} | {cell(why)} |")
    L += ["", f"## Already in config ({len(existing)})", "",
          ", ".join(f"{r['name']} (`{r['verified'][0]['ats']}:{r['verified'][0]['slug']}`)" for r in existing) or "-", ""]
    return "\n".join(L)


# ---------------- main ----------------

def run_fund(fund):
    try:
        return FUND_KINDS[fund["kind"]](fund), None
    except Exception as e:
        return [], repr(e)[:200]


def main(argv):
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    disc = json.loads((ROOT / "discovery.json").read_text(encoding="utf-8-sig"))
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    rx = keyword_rx(disc)
    workers = disc.get("fetch_workers", 12)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        fund_rows = list(ex.map(run_fund, disc["funds"]))
    funds, kept = [], []
    for fund, (rows, err) in zip(disc["funds"], fund_rows):
        rows = [c for c in rows if _norm(c["name"]) != _norm(fund["name"])]   # a fund lists itself on its board
        k = [{**c, "why": why} for c in rows for why in [match_reason(c, rx)] if why]
        k = k[:limit] if limit else k
        funds.append({"name": fund["name"], "kind": fund["kind"], "ok": err is None and bool(rows),
                      "error": err or ("0 companies returned" if not rows else ""), "total": len(rows), "kept": len(k)})
        kept += k
        print(f"{fund['name']:<20} {'ok' if err is None else 'FAILED'} {len(rows):>4} companies, {len(k):>3} kept"
              + (f"  ({err})" if err else ""), file=sys.stderr)

    companies = merge_companies(kept)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        resolved = list(ex.map(resolve, companies))
    results = [{"name": c["name"], "funds": c["funds"], "domain": c["domain"], "matched": c["why"],
                "verified": verified, "tried": tried + [f"uses {x} (no vigil adapter)" for x in others]}
               for c, (verified, tried, others) in zip(companies, resolved)]
    results.sort(key=lambda r: r["name"].lower())
    new, existing, unresolved = diff(results, configured(cfg))

    when = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = ROOT / disc.get("report_dir", "discovery")
    out.mkdir(exist_ok=True)
    (out / "report.md").write_text(render_md(funds, new, existing, unresolved, when), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(
        {"date": when, "funds": funds, "new": new, "unresolved": unresolved,
         "already_configured": existing, "config_snippet": snippet(new)}, indent=1), encoding="utf-8")
    print(f"{len(companies)} companies: {len(new)} new verified, {len(existing)} already configured, "
          f"{len(unresolved)} unresolved -> {out / 'report.md'}", file=sys.stderr)
    return 0 if any(f["ok"] for f in funds) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
