#!/usr/bin/env python3
"""Delivery routing + roles board. Usage: python test_delivery.py
  - deliver():     what buzzes the phone vs. lands on the board only
  - board_merge(): newest-first, deduped by id, aged out, capped
  - role_pay():    structured pay beats description pay beats cached midpoint
No network: push_role is stubbed."""
import sys
import time

import watch

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def rec(i, tier, score, bigco=False):
    return {"id": f"t:{i}", "company": f"Co{i}", "title": "Hardware Intern", "location": "Austin, TX",
            "url": f"https://x/{i}", "score": score, "tier": tier, "bigco": bigco,
            "posted": 0, "published": 0, "pay": "", "season": ""}


def test_deliver():
    sent = []
    watch.push_role = lambda r, p: sent.append((r["id"], p))
    now = time.time()
    out = watch.deliver(
        [rec(1, "A", 5), rec(2, "A", 4, bigco=True)],
        [rec(3, "B", 4), rec(4, "B", 1), rec(5, "B", 4, bigco=True)], now)
    by = {r["id"]: r["delivery"] for r in out}
    check("tierA_startup_high", by["t:1"], "high")
    check("tierA_bigco_board", by["t:2"], "board")
    check("tierB_target_low", by["t:3"], "low")
    check("tierB_weak_board", by["t:4"], "board")
    check("tierB_bigco_board", by["t:5"], "board")
    check("only_pushes_sent", sorted(sent), [("t:1", "high"), ("t:3", "low")])
    check("found_stamped", all(r["found"] == now for r in out), True)


def test_deliver_cap():
    sent = []
    watch.push_role = lambda r, p: sent.append(r["id"])
    watch.CFG["max_alerts_per_run"] = 2
    try:
        out = watch.deliver([rec(i, "A", 5) for i in range(4)], [], time.time())
    finally:
        watch.CFG.pop("max_alerts_per_run")
    check("cap_pushes", len(sent), 2)
    check("overflow_kept_on_board", [r["delivery"] for r in out].count("board"), 2)


def test_board_merge():
    now = time.time()
    old = {**rec(1, "A", 5), "found": now - 90 * 86400}
    backfill = {**rec(2, "B", 1), "found": 0}
    dup_old = {**rec(3, "A", 5), "found": now - 100, "title": "old copy"}
    new = [{**rec(3, "A", 5), "found": now, "title": "new copy"}, {**rec(4, "A", 5), "found": now}]
    out = watch.board_merge([old, backfill, dup_old], new)
    check("order_newest_first", [r["id"] for r in out], ["t:3", "t:4", "t:2"])
    check("dedup_keeps_newest", out[0]["title"], "new copy")
    big = watch.board_merge([], [{**rec(i, "B", 1), "found": now} for i in range(watch.BOARD_MAX + 5)])
    check("capped", len(big), watch.BOARD_MAX)


def test_role_pay():
    check("struct_wins", watch.role_pay({"pay_struct": (28, 34)}, {"pay_range": (40, 50)}), "$28-34/hr")
    check("desc_range", watch.role_pay({}, {"pay_range": [40, 50]}), "$40-50/hr")
    check("cached_midpoint", watch.role_pay({}, {"pay": 31.0}), "$31/hr")
    check("none", watch.role_pay({}, {}), "")


if __name__ == "__main__":
    test_deliver()
    test_deliver_cap()
    test_board_merge()
    test_role_pay()
    if fails:
        print("FAIL\n  " + "\n  ".join(fails))
        sys.exit(1)
    print("OK - all delivery tests passed")
