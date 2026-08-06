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
  };
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

// Full instant-push decision (title checks + description hard-mismatch). Used by
// the parity test; the worker inlines the two halves so it can fetch the GH desc.
export function curatedInstant(job, f) {
  return curatedTitlePass(job, f) && !hardMismatch(job.desc);
}
