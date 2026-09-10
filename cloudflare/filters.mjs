// Shared filter logic for the vigil Cloudflare Worker. Kept in ONE place and
// faithfully mirrors the Python geo_tier() / season_dropped() in watch.py.
// parity.mjs + test_parity.py assert both sides agree on a fixed set of roles,
// so the two implementations can't silently drift.

export function makeFilters(cfg) {
  const rx = (a) => (a && a.length ? new RegExp(a.join("|"), "i") : null);
  const DROP = rx(cfg.drop_countries);
  const TIERB = rx(cfg.tier_b_countries);
  const US_STATE = /,\s*[A-Z]{2}(\b|$)/;               // case-sensitive, like Python
  const US_NAME = /\b(united states|usa|u\.s\.a?\.)\b/i;

  function segGeo(seg) {
    const s = seg.trim();
    if (!s) return "unknown";
    if (DROP && DROP.test(s)) return "drop";            // country name beats a 2-letter code
    if (TIERB && TIERB.test(s)) return "tierb";
    if (US_STATE.test(seg) || US_NAME.test(s)) return "us";
    const low = s.toLowerCase();
    if (low.includes("remote") && (low.includes("us") || low.includes("united states"))) return "us";
    return "unknown";
  }

  function geo(location) {
    if (!location) return "unknown";
    const segs = location.split(/\s*[/;|\n]\s*/).filter((x) => x.trim());
    const t = segs.map(segGeo);
    if (t.includes("us")) return "us";                  // any US segment wins
    if (t.includes("tierb")) return "tierb";
    if (t.length && t.every((x) => x === "drop")) return "drop";
    return "unknown";
  }

  const seasonRe = cfg.season_title_drop ? new RegExp(cfg.season_title_drop, "i") : null;
  const excludeCo = cfg.exclude_companies && cfg.exclude_companies.length
    ? new RegExp("\\b(?:" + cfg.exclude_companies.join("|") + ")\\b", "i") : null;

  return {
    geo,
    seasonDrop: (title) => !!(seasonRe && seasonRe.test(title || "")),
    exclude: rx(cfg.exclude_keywords),
    excludeCo,
    intern: rx(cfg.ats_require),
    include: rx(cfg.include_keywords),
  };
}

// Curated priority: a hand-picked company's intern still DELIVERS regardless of
// relevance, but only hardware/robotics-relevant titles fire a high-priority
// instant ping. Clearly off-target functions (IT, generic SWE, ML, biomedical)
// that carry NO hardware signal deliver at low priority instead. A generic
// "Engineering Intern" is kept high; "Software Engineer, Robotics" and "Embedded
// SWE" are saved by their hardware/robotics keyword. Mirrors watch.py
// curated_relevant() — asserted by the parity 'relevance' fixtures.
const CURATED_OFFTARGET = /\b(?:software|swe|full ?stack|front ?end|back ?end|web developer|information technology|sys ?admin|systems? administrator|help ?desk|machine learning|ml|data scien|data analyst|biomedical|clinical|finance|financial|business)\b/i;
export function curatedRelevant(job, f) {
  const title = job.title || "";
  if (f.include && f.include.test(title)) return true;   // explicit hardware/robotics signal
  return !CURATED_OFFTARGET.test(title);
}

// Hourly pay (mirrors watch.py extract_pay): a suffix form ("$28 - $34/hour",
// "$28/hr", "$28 per hour", "$28 hourly") and a prefix form ("hourly rate: $28 -
// $34"). No match -> null, never a drop reason on its own.
const PAY_SUFFIX = /\$\s?(\d{1,3}(?:\.\d{1,2})?)(?:\s*(?:-|–|—|to)\s*\$?\s?(\d{1,3}(?:\.\d{1,2})?))?\s*(?:\/\s*(?:hr|hour)\b|per\s+hour\b|(?:an|\/)\s*hour\b|hourly\b)/i;
const PAY_PREFIX = /(?:hourly\s*(?:rate|pay|wage)|pay\s*rate)\D{0,40}\$\s?(\d{1,3}(?:\.\d{1,2})?)(?:\s*(?:-|–|—|to)\s*\$?\s?(\d{1,3}(?:\.\d{1,2})?))?/i;
export function extractPay(desc) {
  if (!desc) return null;
  const m = desc.match(PAY_SUFFIX) || desc.match(PAY_PREFIX);
  if (!m) return null;
  let lo = parseFloat(m[1]);
  let hi = m[2] ? parseFloat(m[2]) : lo;
  if (hi < lo) [lo, hi] = [hi, lo];
  if (lo < 5 || lo > 250) return null;  // sanity bounds - reject a non-hourly $ figure
  return (lo + hi) / 2;
}
// Hard drop: pay stated below the floor. Mirrors watch.py's pay_floor_hourly rule.
export function payDrop(desc, cfg) {
  const floor = cfg.pay_floor_hourly;
  if (!floor) return false;
  const pay = extractPay(desc);
  return pay !== null && pay < floor;
}
// Mid-band pay (floor <= pay < preferred) only stays high-priority on a strong
// fit. The worker has no ported scoring table, so an explicit include_keywords
// title hit is the closest available proxy for watch.py's score >= pay_midband_min_score.
export function payMidbandWeak(job, desc, cfg, f) {
  const preferred = cfg.pay_preferred_hourly;
  if (!preferred) return false;
  const pay = extractPay(desc);
  if (pay === null || pay >= preferred) return false;
  return !(f.include && f.include.test(job.title || ""));
}

// Hard eligibility mismatch from a description (mirrors watch.py extract_signals):
// an explicit 2025-27 graduation requirement without 2028, or a Master's/PhD-only
// requirement. Only applied when a description is available for free (Lever/Ashby).
const GRAD_EARLY = /(graduat|class of|degree by|complet\w+)[^.]{0,40}?\b20(25|26|27)\b([^.]{0,20})/i;
const ELIGIBLE_TAIL = /or later|and beyond|onwards?|or after|or above|and later|\+/i;
const GRAD_ONLY = /\b(ph\.?d|doctoral|master)/i;
const HAS_BACH = /\bbachelor|\bundergrad|\bB\.?S\.?\b|\bBSc\b|\bBS[A-Z]{2,3}\b/i;
export function hardMismatch(desc) {
  if (!desc) return false;
  const m = desc.match(GRAD_EARLY);   // 2027-or-later / and-beyond / 2027+ is eligible
  if (m && !ELIGIBLE_TAIL.test(m[3] || "") && !/\b2028\b/.test(desc)) return true;
  if (GRAD_ONLY.test(desc) && !HAS_BACH.test(desc)) return true;
  return false;
}

// Cheap title/geo checks (no description needed). The worker resolves the
// description separately (inline for Lever/Ashby, per-job fetch for Greenhouse)
// and applies hardMismatch after this passes, so a description is fetched only
// for roles about to be pushed.
export function curatedTitlePass(job, f) {
  if (!job.url || !job.title) return false;
  const blob = `${job.title} ${job.company}`;
  if (f.exclude && f.exclude.test(blob)) return false;
  if (f.excludeCo && job.company && f.excludeCo.test(job.company)) return false;
  if (f.intern && !f.intern.test(job.title)) return false;   // intern/co-op gate
  if (f.seasonDrop(job.title)) return false;                 // title says a 2026 season
  if (f.geo(job.location) !== "us") return false;            // non-US never instant-pushes
  return true;
}

// Full instant-push decision (title checks + description hard-mismatch + pay
// floor). Used by the parity test; the worker inlines the pieces so it can fetch
// the GH desc and apply the pay mid-band priority split separately.
export function curatedInstant(job, f, cfg) {
  return curatedTitlePass(job, f) && !hardMismatch(job.desc) && !payDrop(job.desc, cfg || {});
}
