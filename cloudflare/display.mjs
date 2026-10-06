// Presentation helpers - the JS twin of display.py. test_parity.py asserts both
// sides format pay and timestamps identically, so a role reads the same whether
// the worker or GitHub Actions delivered it.

// Below HOURLY_MAX the figure is hourly, at or above ANNUAL_MIN it's a yearly
// salary, in between a monthly stipend. ATS "interval" labels are unreliable
// (Ashby has $32-37 tagged "1 YEAR"), so magnitude decides.
const HOURLY_MAX = 300;
const ANNUAL_MIN = 20000;
const HOURS_PER_YEAR = 2080;
const HOURS_PER_MONTH = 173;

function rng(lo, hi) {
  lo = lo == null ? null : Number(lo);
  hi = hi == null ? null : Number(hi);
  if (Number.isNaN(lo) || Number.isNaN(hi)) return null;
  lo = lo || hi;
  hi = hi || lo;
  if (!lo || lo <= 0) return null;
  return [Math.min(lo, hi), Math.max(lo, hi)];
}

export function ghPay(ranges) {
  for (const r of ranges || []) {
    if ((r.currency_type || "USD") !== "USD") continue;
    const got = rng((r.min_cents || 0) / 100, (r.max_cents || 0) / 100);
    if (got) return got;
  }
  return null;
}

export function leverPay(sr) {
  if (!sr || (sr.currency || "USD") !== "USD") return null;
  return rng(sr.min, sr.max);
}

export function ashbyPay(comp) {
  for (const c of (comp && comp.summaryComponents) || []) {
    if (c.compensationType === "Salary" && (c.currencyCode || "USD") === "USD") {
      const got = rng(c.minValue, c.maxValue);
      if (got) return got;
    }
  }
  return null;
}

// Python's round() is banker's rounding; match it so both sides print the same.
function pyRound(v) {
  const f = Math.floor(v), d = v - f;
  if (Math.abs(d - 0.5) < 1e-9) return f % 2 === 0 ? f : f + 1;
  return Math.round(v);
}
const money = (v) => (v % 1 ? v.toFixed(2).replace(/0+$/, "").replace(/\.$/, "") : String(v));
const span = (lo, hi, fmt) => (lo === hi ? fmt(lo) : `${fmt(lo)}-${fmt(hi)}`);

export function fmtPay(r) {
  if (!r) return "";
  const [lo, hi] = r;
  if (hi <= HOURLY_MAX) return "$" + span(lo, hi, money) + "/hr";
  let head, per;
  if (lo >= ANNUAL_MIN) {
    head = "$" + span(lo, hi, (v) => `${pyRound(v / 1000)}k`) + "/yr";
    per = HOURS_PER_YEAR;
  } else {
    head = "$" + span(lo, hi, (v) => pyRound(v).toLocaleString("en-US")) + "/mo";
    per = HOURS_PER_MONTH;
  }
  return `${head} (~$${span(pyRound(lo / per), pyRound(hi / per), String)}/hr)`;
}

const ET = "America/New_York";
function etParts(epochSec) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: ET, month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hour12: true,
  }).formatToParts(new Date(epochSec * 1000));
  return Object.fromEntries(parts.map((p) => [p.type, p.value]));
}

export function fmtDate(epochSec) {
  if (!epochSec) return "";
  const p = etParts(epochSec);
  return `${p.month} ${p.day}`;
}

export function fmtFound(epochSec) {
  if (!epochSec) return "";
  const p = etParts(epochSec);
  return `${p.month} ${p.day}, ${p.hour}:${p.minute} ${p.dayPeriod.toUpperCase()} ET`;
}

export function pushBody(location, pay, season, posted, found) {
  const line2 = [pay || "Pay not listed"];
  if (season) line2.push(season);
  const line3 = [];
  if (posted) line3.push(`Posted ${fmtDate(posted)}`);
  line3.push(`Found ${fmtFound(found)}`);
  return [location || "Location not listed", line2.join(" · "), line3.join(" · ")].join("\n");
}

export function displayCompany(slug, names) {
  if (!slug) return slug;
  const named = (names || {})[slug.toLowerCase()];
  if (named) return named;
  return slug.split(/[-_]+/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
}
