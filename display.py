"""Presentation helpers shared by watch.py's pushes and the roles board.

Pure functions only (no network, no config). Each one has a twin in
cloudflare/filters.mjs and the pair is asserted by test_parity.py, so a role
reads identically whether the worker or Actions delivered it.
"""

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:          # Windows without the tzdata package
    _ET = None

# Below this the figure is hourly, at or above ANNUAL_MIN it's a yearly salary,
# and in between it's a monthly stipend. ATS "interval" labels are unreliable
# (Ashby has $32-37 tagged "1 YEAR"), so magnitude decides.
HOURLY_MAX = 300
ANNUAL_MIN = 20000
HOURS_PER_YEAR = 2080
HOURS_PER_MONTH = 173


def _rng(lo, hi):
    """Normalize a (lo, hi) dollar pair; None when unusable."""
    try:
        lo = float(lo) if lo is not None else None
        hi = float(hi) if hi is not None else None
    except (TypeError, ValueError):
        return None
    lo = lo if lo else hi
    hi = hi if hi else lo
    if not lo or lo <= 0:
        return None
    return (min(lo, hi), max(lo, hi))


def gh_pay(ranges):
    """Greenhouse pay_input_ranges (pay_transparency=true) -> (lo, hi) dollars."""
    for r in ranges or []:
        if (r.get("currency_type") or "USD") != "USD":
            continue
        got = _rng((r.get("min_cents") or 0) / 100, (r.get("max_cents") or 0) / 100)
        if got:
            return got
    return None


def lever_pay(sr):
    """Lever salaryRange -> (lo, hi) dollars."""
    if not sr or (sr.get("currency") or "USD") != "USD":
        return None
    return _rng(sr.get("min"), sr.get("max"))


def ashby_pay(comp):
    """Ashby compensation (includeCompensation=true) -> (lo, hi) dollars."""
    for c in (comp or {}).get("summaryComponents") or []:
        if c.get("compensationType") == "Salary" and (c.get("currencyCode") or "USD") == "USD":
            got = _rng(c.get("minValue"), c.get("maxValue"))
            if got:
                return got
    return None


def _money(v):
    return f"{v:.2f}".rstrip("0").rstrip(".") if v % 1 else f"{int(v)}"


def _span(lo, hi, fmt):
    return fmt(lo) if lo == hi else f"{fmt(lo)}-{fmt(hi)}"


def fmt_pay(rng):
    """'$28-34/hr', '$110k-160k/yr (~$53-77/hr)', '$7,000/mo (~$40/hr)', or ''."""
    if not rng:
        return ""
    lo, hi = rng
    if hi <= HOURLY_MAX:
        return "$" + _span(lo, hi, _money) + "/hr"
    if lo >= ANNUAL_MIN:
        yr = "$" + _span(lo, hi, lambda v: f"{round(v / 1000)}k") + "/yr"
        per = HOURS_PER_YEAR
    else:
        yr = "$" + _span(lo, hi, lambda v: f"{round(v):,}") + "/mo"
        per = HOURS_PER_MONTH
    return f"{yr} (~${_span(round(lo / per), round(hi / per), str)}/hr)"


def _to_et(epoch):
    utc = datetime.fromtimestamp(epoch, tz=timezone.utc)
    if _ET is not None:
        return utc.astimezone(_ET)
    # Fallback US DST rule: 2nd Sunday of March 07:00 UTC -> 1st Sunday of Nov 06:00 UTC.
    y = utc.year
    mar = datetime(y, 3, 8, 7, tzinfo=timezone.utc)
    nov = datetime(y, 11, 1, 6, tzinfo=timezone.utc)
    start = mar + timedelta(days=(6 - mar.weekday()) % 7)
    end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return utc + timedelta(hours=-4 if start <= utc < end else -5)


def fmt_date(epoch):
    """'Oct 3' in US Eastern, or '' when unknown."""
    if not epoch:
        return ""
    d = _to_et(epoch)
    return f"{d:%b} {d.day}"


def fmt_found(epoch):
    """'Oct 5, 8:42 PM ET' - an absolute stamp that stays true after the first minute."""
    if not epoch:
        return ""
    d = _to_et(epoch)
    hour = d.hour % 12 or 12
    return f"{d:%b} {d.day}, {hour}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'} ET"


def push_body(location, pay, season, posted, found):
    """Notification body. The title line (Company - Title) is never repeated here.
      line 1: location
      line 2: pay (or 'Pay not listed') + season
      line 3: Posted <date> . Found <date, time ET>
    """
    line2 = [pay or "Pay not listed"]
    if season:
        line2.append(season)
    line3 = []
    if posted:
        line3.append(f"Posted {fmt_date(posted)}")
    line3.append(f"Found {fmt_found(found)}")
    return "\n".join([location or "Location not listed", " · ".join(line2), " · ".join(line3)])
