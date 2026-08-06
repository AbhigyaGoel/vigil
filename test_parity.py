#!/usr/bin/env python3
"""Assert watch.py (Python) and filters.mjs (JS worker) agree on the shared geo +
season rules, so the two filter implementations can't drift. Runs both sides
against cloudflare/parity_cases.json. Usage: python test_parity.py"""
import json
import subprocess
import sys
from pathlib import Path

import watch

ROOT = Path(__file__).parent
cases = json.loads((ROOT / "cloudflare" / "parity_cases.json").read_text())

fail = 0
for c in cases["geo"]:
    got = watch.geo_tier(c["loc"])
    if got != c["expect"]:
        fail += 1
        print(f"PY GEO FAIL {c['loc']!r} -> {got} (want {c['expect']})")
for c in cases["season"]:
    got = watch.season_dropped(None, c["title"])  # title-only, matches the worker
    if got != c["expect"]:
        fail += 1
        print(f"PY SEASON FAIL {c['title']} -> {got} (want {c['expect']})")
n = len(cases["geo"]) + len(cases["season"])
print(f"PY parity: {'FAIL' if fail else f'all {n} pass'}")

js = subprocess.run(["node", str(ROOT / "cloudflare" / "parity.mjs")], capture_output=True, text=True)
print(js.stdout.strip())
if js.stderr.strip():
    print(js.stderr.strip())
sys.exit(1 if (fail or js.returncode) else 0)
