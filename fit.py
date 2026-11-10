"""Resume fit for generic software titles (vigil v2.13). Mirrors cloudflare/fit.mjs.

A posting is scored on its DUTIES + QUALIFICATIONS only. The company blurb before
the first role heading and the benefits/EEO/pay tail are dropped: robotics, AV and
defense companies put "autonomous", "robot", "computer vision" in every posting's
About-us text, which made their web/mobile/infra roles look like strong fits.

Measured on 157 live postings (83 generic-title roles, labels split between the
author and a blind labeler): full-text scoring had 20 false positives / 3 misses;
duties+quals with the title-specialty check below has 4 / 1. Qualifications alone
did worst (they're generic: "C++, Python, Linux"), so they're never used alone.

Stdlib only.
"""

import html as _html
import re

_BLOCK = re.compile(r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|section)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_STRIP = " :-–—*#•· "

ROLE_H = re.compile(
    r"what you(?:'|’)?(?:ll| will)? (?:do|work on|be doing|build|own|could work on|get to do)|responsibilit|"
    r"about the (?:role|position|job|team|opportunity|internship)|the (?:role|opportunity|position|internship)\b|"
    r"role overview|your role|in this role|day[- ]to[- ]day|job description|position (?:summary|overview)|"
    r"what (?:the|this) (?:role|job|internship)|key (?:duties|tasks)|projects? you|you will\b|"
    r"you(?:'|’)ll\b(?! bring| need| love)|overview of the role|internship (?:overview|details)|"
    r"what (?:we(?:'|’)re|we are) building|the work|^(?:the )?roles?\b|^description\b|^focus\b|"
    r"team description", re.I)
QUAL_H = re.compile(
    r"qualifications|requirements|what (?:we(?:'|’)re|we are) looking for|what you(?:'|’)?(?:ll| will)? (?:bring|need)|"
    r"who you are|about you|you (?:have|might|may|should|are|bring)|skills|bonus|nice[- ]to[- ]haves?|preferred|"
    r"minimum|basic|must[- ]haves?|ideal candidate|experience (?:we|you)|what makes you|we(?:'|’)d love|"
    r"desired|competenc|education|what we (?:require|value)|tech stack|looking for|candidates who|good fit", re.I)
END_H = re.compile(
    r"benefits|what we offer|compensation|salary|\bpay\b|pay range|equal (?:opportunity|employment)|\beeo\b|perks|"
    r"why (?:join|work|you(?:'|’)ll love)|export control|\bitar\b|u\.?s\.? person|accommodat|our (?:values|culture)|"
    r"life at|physical (?:demands|requirements)|work environment|additional information|how to apply|"
    r"what (?:we|you) (?:get|receive)|total rewards|location\b", re.I)
_MIN_SECTION = 40   # chars: shorter than this, the heading split found a heading but no duties


def _lines(text):
    t = text or ""
    if "&lt;" in t:                      # entity-escaped HTML (Greenhouse's API)
        t = _html.unescape(t)
    t = _BLOCK.sub("\n", t)
    t = _html.unescape(_TAG.sub(" ", t))
    return [re.sub(r"\s+", " ", x).strip() for x in t.split("\n") if x.strip()]


def _heading(line):
    s = line.strip(_STRIP)
    return 0 < len(s) <= 70 and not s.endswith(".") and len(s.split()) <= 10


def sections(text):
    """Split a posting (HTML or newline-separated text) into pre / role / qual / end
    by its headings. An unrecognised heading keeps the current section."""
    out = {"pre": [], "role": [], "qual": [], "end": []}
    cur = "pre"
    for ln in _lines(text):
        if _heading(ln):
            h = ln.strip(_STRIP)
            if END_H.search(h):
                cur = "end"
            elif QUAL_H.search(h):
                cur = "qual"
            elif ROLE_H.search(h):
                cur = "role"
        out[cur].append(ln)
    return {k: " ".join(v) for k, v in out.items()}


def core_text(text):
    """Duties + qualifications; the benefits/EEO tail is always dropped. A posting
    with no real duties section keeps its intro (some put the duties there, and a
    posting with no headings at all is all intro) rather than scoring nothing."""
    s = sections(text)
    role = s["role"] if len(s["role"]) >= _MIN_SECTION else f'{s["pre"]} {s["role"]}'
    return f'{role} {s["qual"]}'.strip()


class Fit:
    """Built from config so watch.py and the tests share one instance shape."""

    def __init__(self, cfg):
        self.rows = [(w, re.compile(r, re.I)) for w, r in cfg.get("resume_fit") or []]
        self.min = cfg.get("resume_fit_min") or 0
        self.min_edge = cfg.get("resume_fit_min_edge", self.min)
        self.generic_cap = cfg.get("resume_fit_generic_cap", 2)
        off, on = cfg.get("off_specialty_titles") or [], cfg.get("on_specialty_titles") or []
        self.off = re.compile(r"\b(?:" + "|".join(off) + r")\b", re.I) if off else None
        self.on = re.compile(r"\b(?:" + "|".join(on) + r")\b", re.I) if on else None

    def score(self, text):
        """Resume fit of a posting's duties + qualifications (each row counts once;
        weight-1 generic rows add at most generic_cap together)."""
        core = core_text(text)
        hits = [w for w, r in self.rows if r.search(core)]
        return sum(w for w in hits if w > 1) + min(sum(w for w in hits if w <= 1), self.generic_cap)

    def off_specialty(self, title):
        """Title names a specialization outside the resume's edge (Android, Frontend,
        Infrastructure...) and nothing on-target ("Onboard Infrastructure" is fine)."""
        return bool(self.off and self.off.search(title or "")
                    and not (self.on and self.on.search(title or "")))

    def ok(self, fit, edge=False):
        bar = self.min_edge if edge else self.min
        return bool(bar and fit is not None and fit >= bar)
