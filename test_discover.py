"""Unit tests for discover.py's offline logic (no network).

Covers:
  - ats_links():    ATS slug extraction from job URLs / careers-page HTML
  - match_reason(): keyword + exact-tag company filter, incl. the false positives it must drop
  - owns_board():   a guessed slug needs evidence it is THIS company's board
  - diff():         only verified boards missing from config.json count as new

Run:  python test_discover.py    (exit 0 = all pass)
"""
import json
from pathlib import Path

import discover as d

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r} want {want!r}")


def co(name, desc="", tags=(), domain=""):
    return d.company(name, "Test Fund", domain, desc, tags)


def test_ats_links():
    html = """
      <a href="https://boards.greenhouse.io/flyzipline/jobs/123">x</a>
      <script src="https://boards.greenhouse.io/embed/job_board/js?for=skydio"></script>
      <a href="https://job-boards.greenhouse.io/anduril/jobs/9">dup host variant</a>
      <a href="https://jobs.lever.co/zoox/abc">x</a> <a href="https://jobs.eu.lever.co/cirrus/abc">eu</a>
      <a href="https://jobs.ashbyhq.com/physicalintelligence/uuid">x</a>
      <a href="https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/1">wd</a>
      <a href="https://boards.greenhouse.io/embed/job_app?for=x">not a board</a>
    """
    got = d.ats_links(html)
    check("gh_board", ("greenhouse", "flyzipline") in got, True)
    check("gh_embed", ("greenhouse", "skydio") in got, True)
    check("gh_job_boards_host", ("greenhouse", "anduril") in got, True)
    check("lever", ("lever", "zoox") in got, True)
    check("lever_eu", ("lever", "cirrus@eu") in got, True)
    check("ashby", ("ashby", "physicalintelligence") in got, True)
    check("workday", ("workday", "nvidia|nvidia.wd5.myworkdayjobs.com|NVIDIAExternalCareerSite") in got, True)
    check("no_embed_slug", any(s == "embed" for _, s in got), False)
    check("dedup", len(got), len(set(got)))
    check("other_ats", d.other_ats("https://jobs.gem.com/ntop/x https://apply.workable.com/j/1"), ["gem", "workable"])
    check("other_ats_none", d.other_ats("https://boards.greenhouse.io/x"), [])


def test_match_reason():
    rx = d.keyword_rx(json.loads(Path(d.ROOT / "discovery.json").read_text(encoding="utf-8")))
    keep = [
        co("Bedrock Robotics", "Autonomous construction equipment"),
        co("Skyline", "Autonomous trucking for mining sites"),
        co("Quiet", "We design custom AI chips"),
        co("NoDesc", tags=["Robotics"]),
        co("FirmCo", "Embedded systems for industrial IoT"),
    ]
    drop = [
        co("Lead Bank", "Embedded finance and banking-as-a-service"),
        co("Polly", "Mortgage pricing platform", tags=["Hardware", "Capital Markets"]),
        co("Agentic", "Autonomous AI agents for sales teams"),
        co("Valley Co", "Fintech from Silicon Valley"),
        co("RPA Inc", "Bots", tags=["Robotic Process Automation (RPA)"]),
        co("RPA Two", "Robotic process automation for back offices"),
        co("Finch", "Built by cutting-edge AI talent from Google"),
        co("Kyra", "Works with global blue-chip brands such as Nike"),
        co("True Link", "Greater independence and autonomy for people at financial risk"),
    ]
    for c in keep:
        check(f"keep:{c['name']}", bool(d.match_reason(c, rx)), True)
    for c in drop:
        check(f"drop:{c['name']}", d.match_reason(c, rx), None)
    check("tag_reason", d.match_reason(co("NoDesc", tags=["Robotics"]), rx), "tag:Robotics")


def test_owns_board():
    c = co("Sphere", domain="spheresemi.com")
    check("domain_in_jobs", d.owns_board(c, "", "Join us at spheresemi.com"), True)
    check("short_name_not_enough", d.owns_board(c, "", "Sphere is a payments company"), False)
    long = co("Atomic Machines", domain="atomicmachines.com")
    check("long_name_in_jobs", d.owns_board(long, "", "atomic machines builds microdevices"), True)
    check("gh_name_match", d.owns_board(co("Augury Inc."), "Augury", ""), True)
    check("gh_name_mismatch", d.owns_board(co("Abacus.AI"), "ABACUS Group", ""), False)


def test_diff_and_snippet():
    cfg = {"greenhouse": ["flyzipline"], "lever": ["cirrus@eu"], "ashby": [], "workday": [{"tenant": "nvidia"}]}
    have = d.configured(cfg)
    rows = [
        {"name": "Zipline", "verified": [{"ats": "greenhouse", "slug": "flyzipline"}]},
        {"name": "Cirrus", "verified": [{"ats": "lever", "slug": "cirrus@eu"}]},
        {"name": "NVIDIA", "verified": [{"ats": "workday", "slug": "nvidia|nvidia.wd5.myworkdayjobs.com|Site"}]},
        {"name": "NewCo", "verified": [{"ats": "ashby", "slug": "newco"}]},
        {"name": "Ghost", "verified": []},
    ]
    new, existing, unresolved = d.diff(rows, have)
    check("new", [r["name"] for r in new], ["NewCo"])
    check("existing", sorted(r["name"] for r in existing), ["Cirrus", "NVIDIA", "Zipline"])
    check("unresolved", [r["name"] for r in unresolved], ["Ghost"])
    wd = [{"name": "Acme", "verified": [{"ats": "workday", "slug": "acme|acme.wd1.myworkdayjobs.com|External"}]}]
    check("snippet_workday", d.snippet(wd),
          {"workday": [{"name": "Acme", "tenant": "acme", "host": "acme.wd1.myworkdayjobs.com", "site": "External"}]})


def test_merge():
    a = co("Bright Machines", domain="https://www.brightmachines.com/")
    b = {**co("Bright Machines", domain="brightmachines.com"), "funds": ["Lux"]}
    merged = d.merge_companies([a, b])
    check("merge_count", len(merged), 1)
    check("merge_funds", merged[0]["funds"], ["Lux", "Test Fund"])


def test_robustness():
    check("null_name", d.company(None, "F")["name"], "")
    check("bad_json", d.parse_json("<html>rate limited</html>"), None)
    check("cell_pipe", d.cell("A | B\nC"), r"A \| B C")
    # a Lever 200 that is an error object, not a postings list, must not count as a board
    orig = d.fetch
    try:
        d.fetch = lambda url, *a, **k: (200, '{"ok": false, "error": "Document not found"}')
        check("lever_error_obj", d.probe("lever", "ghost")[0], 0)
        d.fetch = lambda url, *a, **k: (200, "<html>interstitial</html>")
        check("ashby_html_200", d.probe("ashby", "ghost")[0], 0)
    finally:
        d.fetch = orig


def run():
    test_robustness()
    test_ats_links()
    test_match_reason()
    test_owns_board()
    test_diff_and_snippet()
    test_merge()
    if FAILS:
        print("FAIL")
        for f in FAILS:
            print("  -", f)
        raise SystemExit(1)
    print("OK - all discover tests passed")


if __name__ == "__main__":
    run()
