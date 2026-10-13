"""Deterministic confidence math and safety filters (the LLM never has the last word)."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

RELIABILITY = {
    "self": 0.90,          # their own site, GitHub, official bio
    "institutional": 0.85,  # university, employer, conference pages
    "news": 0.85,
    "aggregator": 0.60,     # directories, people-search, crunchbase-likes
    "social": 0.70,
    "snippet": 0.50,        # only a search-result snippet was available
    "about": 0.72,          # page whose title names the person (interview, profile, talk page)
    "unknown": 0.45,
}

INSTITUTIONAL_HINTS = (".edu", ".ac.", ".gov", "ieee.org", "acm.org", "arxiv.org", "scholar.google",
                       "researchgate", "springer", "sciencedirect", "patents.google", "openreview")
NEWS_HINTS = ("news", "times", "post", "journal", "techcrunch", "forbes", "bloomberg", "reuters",
              "businessinsider", "wired", "theverge", "venturebeat", "cnbc", "bbc", "guardian")
SOCIAL_HINTS = ("x.com", "twitter.com", "linkedin.com", "instagram.com", "medium.com", "substack.com",
                "youtube.com", "reddit.com", "github.com", "dev.to", "hashnode", "facebook.com")
AGGREGATOR_HINTS = ("crunchbase", "zoominfo", "rocketreach", "apollo.io", "signalhire", "contactout",
                    "peoplefinder", "spokeo", "theorg.com", "wellfound", "angel.co", "pitchbook")

# Never stored, even as sourced facts.
PERSONAL_PATTERNS = [
    r"\b(home address|lives at|resides at|phone number|cell number|date of birth|born on)\b",
    r"\b(salary|net worth|personal income|debt|mortgage)\b",
    r"\b(wife|husband|spouse|girlfriend|boyfriend|children|kids|son|daughter|divorce)\b",
]
# Never inferred (protected / special-category traits).
SENSITIVE_PATTERNS = PERSONAL_PATTERNS + [
    r"\b(religio\w*|religious|faith|christian|muslim|hindu|jewish|atheis\w*|spiritual)\b",
    r"\b(gay|lesbian|bisexual|sexual orientation|transgender|sexuality)\b",
    r"\b(illness|diagnos\w*|disabilit\w*|pregnan\w*|medical condition|mental illness)\b",
    r"\b(ethnicit\w*|racial|caste)\b",
    r"\b(democrat|republican|political affiliation|votes? for|left-leaning|right-leaning)\b",
]
_PERS = [re.compile(p, re.I) for p in PERSONAL_PATTERNS]
_SENS = [re.compile(p, re.I) for p in SENSITIVE_PATTERNS]


def is_personal(text: str) -> bool:
    return any(p.search(text) for p in _PERS)


def is_sensitive(text: str) -> bool:
    return any(p.search(text) for p in _SENS)


def classify_source(url: str, domain: str, name: str, known_urls: list[str]) -> str:
    d = domain.lower()
    if any(u and (u.lower().rstrip("/") in url.lower() or domain in u.lower()) for u in known_urls):
        return "self"
    toks = [t.lower() for t in name.split() if len(t) > 1]
    flat = d.replace("-", "").replace(".", "")
    if len(toks) >= 2 and (toks[0] + toks[-1]) in flat and not any(s in d for s in SOCIAL_HINTS):
        return "self"  # e.g. janedoe.com (needs first+last in order, not just a surname)
    if "github.com" in d:
        return "self"
    if any(h in d for h in AGGREGATOR_HINTS):
        return "aggregator"
    if any(h in d for h in INSTITUTIONAL_HINTS):
        return "institutional"
    if any(h in d for h in SOCIAL_HINTS):
        return "social"
    if any(h in d for h in NEWS_HINTS):
        return "news"
    return "unknown"


def quote_in_text(quote: str, text: str) -> bool:
    """Fuzzy check that the evidence quote really appears in the page."""
    if not quote or not text:
        return False
    norm = lambda s: re.sub(r"\W+", " ", s.lower()).strip()
    q, t = norm(quote), norm(text)
    if len(q) < 8:
        return False
    if q in t:
        return True
    # sliding fuzzy match over windows around the first rare word
    words = q.split()
    anchor = max(words, key=len)
    for m in re.finditer(re.escape(anchor), t):
        window = t[max(0, m.start() - len(q)): m.start() + len(q) * 2]
        if SequenceMatcher(None, q, window).find_longest_match(0, len(q), 0, len(window)).size >= 0.7 * len(q):
            return True
    return False


def support(p_same: float, kind: str, verified: bool) -> float:
    return p_same * RELIABILITY.get(kind, 0.45) * (0.95 if verified else 0.55)


def noisy_or(supports: list[float], cap: float = 0.97) -> float:
    prod = 1.0
    for s in supports:
        prod *= 1 - max(0.0, min(1.0, s))
    return round(min(1 - prod, cap), 3)


def calibrate_inference(p: float, basis_conf: list[float]) -> float:
    p = max(0.05, min(0.95, float(p)))
    n = len(basis_conf)
    k = 0.6 if n <= 1 else 0.8 if n == 2 else 1.0
    p = 0.5 + (p - 0.5) * k
    if basis_conf:
        p = min(p, 0.15 + max(basis_conf))
    else:
        p = min(p, 0.4)  # an inference with no cited evidence is a guess
    return round(max(0.05, min(0.95, p)), 2)


def label(p: float) -> str:
    if p >= 0.85:
        return "very likely"
    if p >= 0.65:
        return "likely"
    if p >= 0.45:
        return "uncertain"
    if p >= 0.25:
        return "unlikely"
    return "very unlikely"
