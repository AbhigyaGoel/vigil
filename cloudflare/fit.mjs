// Resume fit for generic software titles. Faithful port of watch.py's fit.py -
// parity.mjs + test_parity.py assert both sides score the same postings alike.
//
// A posting is scored on its DUTIES + QUALIFICATIONS only: the company blurb before
// the first role heading and the benefits/EEO/pay tail are dropped, because robotics,
// AV and defense companies put "autonomous / robot / computer vision" in every
// posting's About-us text. See fit.py for the measurement behind this.

const BLOCK = /<\/?(?:p|div|br|li|ul|ol|h[1-6]|tr|section)\b[^>]*>/gi;
const TAG = /<[^>]+>/g;
const STRIP = /^[\s:\-–—*#•·\u00a0]+|[\s:\-–—*#•·\u00a0]+$/g;

const ROLE_H = /what you(?:'|’)?(?:ll| will)? (?:do|work on|be doing|build|own|could work on|get to do)|responsibilit|about the (?:role|position|job|team|opportunity|internship)|the (?:role|opportunity|position|internship)\b|role overview|your role|in this role|day[- ]to[- ]day|job description|position (?:summary|overview)|what (?:the|this) (?:role|job|internship)|key (?:duties|tasks)|projects? you|you will\b|you(?:'|’)ll\b(?! bring| need| love)|overview of the role|internship (?:overview|details)|what (?:we(?:'|’)re|we are) building|the work|^(?:the )?roles?\b|^description\b|^focus\b|team description/i;
const QUAL_H = /qualifications|requirements|what (?:we(?:'|’)re|we are) looking for|what you(?:'|’)?(?:ll| will)? (?:bring|need)|who you are|about you|you (?:have|might|may|should|are|bring)|skills|bonus|nice[- ]to[- ]haves?|preferred|minimum|basic|must[- ]haves?|ideal candidate|experience (?:we|you)|what makes you|we(?:'|’)d love|desired|competenc|education|what we (?:require|value)|tech stack|looking for|candidates who|good fit/i;
const END_H = /benefits|what we offer|compensation|salary|\bpay\b|pay range|equal (?:opportunity|employment)|\beeo\b|perks|why (?:join|work|you(?:'|’)ll love)|export control|\bitar\b|u\.?s\.? person|accommodat|our (?:values|culture)|life at|physical (?:demands|requirements)|work environment|additional information|how to apply|what (?:we|you) (?:get|receive)|total rewards|location\b/i;
const MIN_SECTION = 40;

const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: "\u00a0", "#39": "'" };
export function unescape(s) {
  return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+\d*);/gi, (m, e) => {
    if (e[0] === "#") {
      const n = e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return Number.isFinite(n) ? String.fromCodePoint(n) : m;
    }
    return ENTITIES[e.toLowerCase()] ?? m;
  });
}

function lines(text) {
  let t = text || "";
  if (t.includes("&lt;")) t = unescape(t);   // entity-escaped HTML (Greenhouse's API)
  t = unescape(t.replace(BLOCK, "\n").replace(TAG, " "));
  return t.split("\n").map((x) => x.replace(/\s+/g, " ").trim()).filter(Boolean);
}

function isHeading(line) {
  const s = line.replace(STRIP, "");
  return s.length > 0 && s.length <= 70 && !s.endsWith(".") && s.split(/\s+/).length <= 10;
}

export function sections(text) {
  const out = { pre: [], role: [], qual: [], end: [] };
  let cur = "pre";
  for (const ln of lines(text)) {
    if (isHeading(ln)) {
      const h = ln.replace(STRIP, "");
      if (END_H.test(h)) cur = "end";
      else if (QUAL_H.test(h)) cur = "qual";
      else if (ROLE_H.test(h)) cur = "role";
    }
    out[cur].push(ln);
  }
  return Object.fromEntries(Object.entries(out).map(([k, v]) => [k, v.join(" ")]));
}

export function coreText(text) {
  const s = sections(text);
  const role = s.role.length >= MIN_SECTION ? s.role : `${s.pre} ${s.role}`;
  return `${role} ${s.qual}`.trim();
}

export function makeFit(cfg) {
  const rows = (cfg.resume_fit || []).map(([w, r]) => [w, new RegExp(r, "i")]);
  const min = cfg.resume_fit_min || 0;
  const minEdge = cfg.resume_fit_min_edge ?? min;
  const cap = cfg.resume_fit_generic_cap ?? 2;
  const wrap = (a) => (a && a.length ? new RegExp("\\b(?:" + a.join("|") + ")\\b", "i") : null);
  const off = wrap(cfg.off_specialty_titles), on = wrap(cfg.on_specialty_titles);
  return {
    min, minEdge,
    score(text) {
      const core = coreText(text);
      const hits = rows.filter(([, r]) => r.test(core)).map(([w]) => w);
      const sum = (a) => a.reduce((s, w) => s + w, 0);
      return sum(hits.filter((w) => w > 1)) + Math.min(sum(hits.filter((w) => w <= 1)), cap);
    },
    offSpecialty: (title) => !!(off && off.test(title || "") && !(on && on.test(title || ""))),
    ok(fit, edge = false) {
      const bar = edge ? minEdge : min;
      return !!(bar && fit != null && fit >= bar);
    },
  };
}
