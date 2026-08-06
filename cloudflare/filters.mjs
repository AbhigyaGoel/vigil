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
const GRAD_EARLY = /(graduat|class of|degree by|complet\w+)[^.]{0,40}\b20(25|26|27)\b/i;
const GRAD_ONLY = /\b(ph\.?d|doctoral|master)/i;
const HAS_BACH = /\bbachelor|\bundergrad|\bb\.s\./i;
export function hardMismatch(desc) {
  if (!desc) return false;
  if (GRAD_EARLY.test(desc) && !/\b2028\b/.test(desc)) return true;
  if (GRAD_ONLY.test(desc) && !HAS_BACH.test(desc)) return true;
  return false;
}

// The instant-push decision for a curated-board role. Curated bypasses the score
// gate (any surviving US intern is instant-worthy) but must be US, in-season, and
// (when a description is available) not a hard eligibility mismatch.
export function curatedInstant(job, f) {
  if (!job.url || !job.title) return false;
  const blob = `${job.title} ${job.company}`;
  if (f.exclude && f.exclude.test(blob)) return false;
  if (f.excludeCo && job.company && f.excludeCo.test(job.company)) return false;
  if (f.intern && !f.intern.test(job.title)) return false;   // intern/co-op gate
  if (f.seasonDrop(job.title)) return false;                 // title says a 2026 season
  if (f.geo(job.location) !== "us") return false;            // non-US never instant-pushes
  if (hardMismatch(job.desc)) return false;                  // 2026/27-grad or Master/PhD only
  return true;
}
