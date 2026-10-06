#!/usr/bin/env python3
"""Assert watch.py (Python) and filters.mjs (JS worker) agree on the shared geo,
season, and curated-instant rules, so the two filter implementations can't drift.
Both sides run EVERY case in cloudflare/parity_cases.json; the run fails loudly if
either side runs fewer cases than the fixture or the counts disagree.
Usage: python test_parity.py"""
import json
import re
import subprocess
import sys
from pathlib import Path

import display
import watch

ROOT = Path(__file__).parent
cases = json.loads((ROOT / "cloudflare" / "parity_cases.json").read_text())
fixture_total = (len(cases["geo"]) + len(cases["season"]) + len(cases["curated"])
                 + len(cases["grad"]) + len(cases["relevance"]) + len(cases["pay"])
                 + len(cases["bigco"]) + len(cases["paydisplay"]) + len(cases["structpay"])
                 + len(cases["stamp"]))

fail, ran = 0, 0
for c in cases["geo"]:
    ran += 1
    if watch.geo_tier(c["loc"]) != c["expect"]:
        fail += 1; print(f"PY GEO FAIL {c['loc']!r} -> {watch.geo_tier(c['loc'])} (want {c['expect']})")
for c in cases["season"]:
    ran += 1
    if watch.season_dropped(None, c["title"]) != c["expect"]:
        fail += 1; print(f"PY SEASON FAIL {c['title']} (want {c['expect']})")
watch.ENRICH_FETCH = False
for c in cases["curated"]:
    ran += 1
    j = dict(c["job"]); j.update(id="t:" + c["job"]["title"], category=None, terms=None,
                                 posted=0, src="greenhouse", policy="curated")
    j.setdefault("desc", "")
    dec, _ = watch.classify(j, {})
    if (dec == "A") != c["expect"]:
        fail += 1; print(f"PY CURATED FAIL {c['job']['title']} @ {c['job']['location']} -> {dec} (want push={c['expect']})")
for c in cases["grad"]:
    ran += 1
    sig = watch.extract_signals(c["desc"])
    got = bool(sig.get("grad_bad") or sig.get("grad_only"))
    if got != c["expect"]:
        fail += 1; print(f"PY GRAD FAIL {c['desc']!r} -> {got} (want {c['expect']})")
for c in cases["relevance"]:
    ran += 1
    if watch.curated_relevant(c["title"]) != c["expect"]:
        fail += 1; print(f"PY RELEVANCE FAIL {c['title']!r} -> {watch.curated_relevant(c['title'])} (want {c['expect']})")
for c in cases["pay"]:
    ran += 1
    got = watch.extract_pay(c["desc"])
    if got != c["expect"]:
        fail += 1; print(f"PY PAY FAIL {c['desc']!r} -> {got} (want {c['expect']})")

for c in cases["bigco"]:
    ran += 1
    if watch.is_bigco(c["company"]) != c["expect"]:
        fail += 1; print(f"PY BIGCO FAIL {c['company']!r} (want {c['expect']})")
for c in cases["paydisplay"]:
    ran += 1
    got = display.fmt_pay(tuple(c["range"]) if c["range"] else None)
    if got != c["expect"]:
        fail += 1; print(f"PY PAYDISPLAY FAIL {c['range']} -> {got!r} (want {c['expect']!r})")
STRUCT = {"gh": display.gh_pay, "lv": display.lever_pay, "ab": display.ashby_pay}
for c in cases["structpay"]:
    ran += 1
    got = display.fmt_pay(STRUCT[c["kind"]](c["raw"]))
    if got != c["expect"]:
        fail += 1; print(f"PY STRUCTPAY FAIL {c['kind']} -> {got!r} (want {c['expect']!r})")
for c in cases["stamp"]:
    ran += 1
    d, fo = display.fmt_date(c["epoch"]), display.fmt_found(c["epoch"])
    if (d, fo) != (c["date"], c["found"]):
        fail += 1; print(f"PY STAMP FAIL {c['epoch']} -> {d} | {fo} (want {c['date']} | {c['found']})")

if ran != fixture_total:
    fail += 1; print(f"PY DID NOT RUN ALL CASES: ran {ran} of {fixture_total}")
print(f"PY parity: ran {ran}/{fixture_total}, {'FAIL' if fail else 'all pass'}")

js = subprocess.run(["node", str(ROOT / "cloudflare" / "parity.mjs")], capture_output=True, text=True)
print(js.stdout.strip())
if js.stderr.strip():
    print(js.stderr.strip())
m = re.search(r"total=(\d+)", js.stdout)
js_total = int(m.group(1)) if m else -1
if not (ran == js_total == fixture_total):
    fail += 1; print(f"COUNT MISMATCH: py_ran={ran} js_total={js_total} fixture={fixture_total}")
else:
    print(f"counts agree: {fixture_total} cases on both sides")

sys.exit(1 if (fail or js.returncode) else 0)
