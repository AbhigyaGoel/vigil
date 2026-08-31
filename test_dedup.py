"""Unit tests for the repeat-notification dedup logic added in watch.py v2.8.

Covers:
  - canon_key():  cross-feed identity (same role, different feed/URL -> one key)
  - worker_owned(): cross-system dedup (curated companies the CF worker owns)
  - scan():       end-to-end, the two fixes actually suppress a repeat

Run:  python test_dedup.py    (exit 0 = all pass)
"""
import os
import watch

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r} want {want!r}")


# ---------------- canon_key ----------------

def test_canon_key():
    ck = watch.canon_key
    # a greenhouse role, however it is surfaced, collapses to the same job-id key
    a = ck("https://zipline.com/open-roles?gh_jid=7978843003&utm_source=Simplify")
    b = ck("https://boards.greenhouse.io/flyzipline/jobs/7978843003")
    c = ck("https://job-boards.greenhouse.io/flyzipline/jobs/7978843003?gh_src=abc")
    check("gh_jid==board", a, b)
    check("gh_board_variant", b, c)
    check("gh_prefix", a, "k:gh:7978843003")

    # lever / ashby collapse on their UUID regardless of host or query junk
    u = "6f1a2b3c-4d5e-6f7a-8b9c-0d1e2f3a4b5c"
    la = ck(f"https://jobs.lever.co/diversified-automation/{u}")
    lb = ck(f"https://jobs.lever.co/diversified-automation/{u}/apply?ref=x")
    check("lever_uuid", la, lb)
    check("lever_prefix", la, "k:uuid:" + u)
    ab = ck(f"https://jobs.ashbyhq.com/skydio/{u.upper()}")
    check("ashby_uuid_lc", ab, "k:uuid:" + u)

    # plain URLs: tracking params stripped, real query kept, trailing slash/host norm
    p1 = ck("https://www.acme.com/careers/eng-intern/?utm_source=x&utm_medium=y")
    p2 = ck("http://acme.com/careers/eng-intern")
    check("url_track_strip", p1, p2)
    p3 = ck("https://acme.com/jobs?id=42&utm_campaign=z")
    check("url_keep_real_q", p3, "k:u:acme.com/jobs?id=42")

    check("empty", ck(""), "")
    check("none", ck(None), "")


# ---------------- worker_owned ----------------

def test_worker_owned():
    wo = watch.worker_owned
    # URL carries a curated ATS slug -> owned
    check("gh_slug_owned",
          wo({"url": "https://boards.greenhouse.io/flyzipline/jobs/1", "company": "Zipline"}),
          True)
    check("lever_slug_owned",
          wo({"url": "https://jobs.lever.co/diversified-automation/x", "company": "Diversified Automation"}),
          True)
    # company-careers URL (no ATS host) but curated NAME via alias -> owned
    check("alias_name_owned",
          wo({"url": "https://zipline.com/open-roles?gh_jid=7", "company": "Zipline"}),
          True)
    # curated name that normalizes straight from its slug, no alias needed
    check("name_norm_owned",
          wo({"url": "https://x.com/j", "company": "Diversified Automation"}),
          True)
    # a non-curated company is NOT owned
    check("not_owned",
          wo({"url": "https://jobs.lever.co/randomco/x", "company": "Random Co"}),
          False)
    check("not_owned_name",
          wo({"url": "https://acme.com/j", "company": "Acme Robotics"}),
          False)


# ---------------- scan() integration ----------------

def _job(jid, url, company="Acme", title="Robotics Hardware Intern", policy="bulk"):
    return {"id": jid, "url": url, "company": company, "title": title,
            "location": "Austin, TX", "category": "", "terms": None, "posted": 0,
            "src": "test", "policy": policy}


def test_scan_dedups(monkeyplan):
    # two feeds emit the SAME greenhouse role under different ids + tracking; Fix A
    # must let only one through. A curated (worker-owned) role must be dropped entirely.
    dup_a = _job("md:feed1", "https://boards.greenhouse.io/acmerobotics/jobs/555")
    dup_b = _job("ls:feed2", "https://acme.com/careers?gh_jid=555&utm_source=Simplify")
    owned = _job("md:z", "https://zipline.com/open-roles?gh_jid=999", company="Zipline")

    monkeyplan([dup_a, dup_b, owned])
    os.environ["SKIP_ATS"] = "1"
    try:
        seen = set()
        a, b, rej, stats = watch.scan(seen, {}, collect_rejects=True)
    finally:
        os.environ.pop("SKIP_ATS", None)

    delivered = a + b
    check("one_copy_delivered", len([r for r in delivered if "555" in r["url"]]), 1)
    check("worker_owned_dropped", any(r["company"] == "Zipline" for r in delivered), False)
    check("worker_owned_in_rejects",
          any(r["rule"] == "curated_worker" for r in rej), True)


def run():
    # monkeypatch build_plan to emit a fixed job list from a single fake source
    def make_plan(jobs):
        def fake_source(_arg):
            return list(jobs)
        watch.build_plan = lambda: [(fake_source, "test")]

    test_canon_key()
    test_worker_owned()
    test_scan_dedups(make_plan)

    if FAILS:
        print("FAIL")
        for f in FAILS:
            print("  -", f)
        raise SystemExit(1)
    print("OK - all dedup tests passed")


if __name__ == "__main__":
    run()
