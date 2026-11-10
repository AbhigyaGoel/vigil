"""Unit tests for resume-fit surfacing of generic SWE roles (watch.py v2.12).

Covers, for aggregator rows (Simplify listings, policy=tagged):
  - a generic SWE title is judged on its fetched description's resume fit
  - no description yet -> dropped as fit-pending (retried next run), never pushed
  - weak fit -> dropped; strong fit -> Tier A with the fit bonus
  - an edge company's SWE title needs no description at all
  - a cached entry from before the fit score existed is re-fetched, not trusted

Run:  python test_fit.py    (exit 0 = all pass)
"""
import watch

FAILS = []

STRONG = ("Develop embedded C++ firmware on an RTOS and run hardware-in-the-loop tests "
          "for our flight software.")
WEAK = "Build React dashboards and REST APIs on Linux for our payments team."


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r} want {want!r}")


def row(company="Acme Corp", title="Software Engineer Intern", jid="ls:t1"):
    return {"id": jid, "company": company, "title": title, "location": "Austin, TX",
            "url": "https://example.com/job", "category": "Software", "terms": ["Summer 2027"],
            "posted": 0, "src": "listings", "policy": "tagged"}


def with_fetch(desc, fn):
    """Run fn with listing_description stubbed to return desc (counts calls)."""
    calls = []
    orig, orig_fetch = watch.listing_description, watch.ENRICH_FETCH
    watch.listing_description = lambda url: calls.append(url) or desc
    watch.ENRICH_FETCH = True
    watch.FIT_BUDGET[0] = 5
    try:
        return fn(), calls
    finally:
        watch.listing_description, watch.ENRICH_FETCH = orig, orig_fetch


def test_strong_fit_tier_a():
    (dec, score), calls = with_fetch(STRONG, lambda: watch.classify(row(), {}))
    check("strong fit tier", dec, "A")
    check("strong fit fetched once", len(calls), 1)
    check("strong fit bonus", score >= watch.CFG["resume_fit_score"], True)


def test_weak_fit_dropped():
    (dec, why), _ = with_fetch(WEAK, lambda: watch.classify(row(), {}))
    check("weak fit", (dec, why), ("drop", "weak-fit"))


def test_pending_without_description():
    watch.ENRICH_FETCH = False   # seed run: no fetching
    enrich = {}
    dec, why = watch.classify(row(), enrich)
    check("pending", (dec, why), ("drop", "fit-pending"))
    check("pending not cached", "ls:t1" in enrich, False)


def test_edge_company_needs_no_description():
    watch.ENRICH_FETCH = False
    dec, _ = watch.classify(row(company="Shield AI", jid="ls:t2"), {})
    check("edge company", dec, "A")


def test_stale_cache_refetched():
    enrich = {"ls:t3": {"hw": False, "pay": None}}   # cached before the fit score existed
    (dec, _), calls = with_fetch(STRONG, lambda: watch.classify(row(jid="ls:t3"), enrich))
    check("stale cache refetched", len(calls), 1)
    check("stale cache tier", dec, "A")
    check("fit now cached", "fit" in enrich["ls:t3"], True)


def test_non_engineering_never_judged():
    (dec, why), calls = with_fetch(STRONG, lambda: watch.classify(
        row(title="Business Operations Intern", jid="ls:t4"), {}))
    check("non-eng dropped", (dec, why), ("drop", "irrelevant"))
    check("non-eng not fetched", len(calls), 0)


def test_hardware_fetch_budget_untouched():
    before = watch.ENRICH_BUDGET[0]
    with_fetch(STRONG, lambda: watch.classify(row(jid="ls:t5"), {}))
    check("hardware budget untouched", watch.ENRICH_BUDGET[0], before)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    if FAILS:
        print("\n".join(FAILS))
        raise SystemExit(f"FAIL - {len(FAILS)} check(s)")
    print("OK - all fit tests passed")
