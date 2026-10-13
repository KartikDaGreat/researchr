"""Web search + page fetching, tiered to keep Firecrawl credits for when they matter.

Search tiers
  "rich"  (identity seeds, snippets matter): Firecrawl search -> Groq browser_search
  "cheap" (coverage queries):                Groq browser_search -> Firecrawl search (only if <2 hits)
Fetch tiers
  disk cache -> GitHub API (github.com profiles) -> plain HTTP -> Firecrawl scrape
  (Firecrawl only for high-value pages that plain HTTP could not read, capped per run)
Everything is cached on disk for CACHE_DAYS, so re-running on the same person is nearly free.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import config
from .store import store

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
# never worth a scrape credit: login walls, or listing pages whose search title already says it all
SNIPPET_ONLY = ("linkedin.com", "facebook.com", "tiktok.com", "instagram.com", "podcasts.apple.com",
                "open.spotify.com", "music.apple.com", "zoominfo.com", "rocketreach.co", "signalhire.com")

_FC_BIN = shutil.which("firecrawl")


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    query: str


class Usage:
    """Per-run counters so the report can show what each run cost."""

    def __init__(self, scrape_budget: int, counts: dict | None = None):
        self.lock = threading.Lock()
        self.scrape_budget = scrape_budget
        self.counts = {"fc_search": 0, "fc_scrape": 0, "groq_search": 0, "http_fetch": 0, "github_api": 0, "cache_hits": 0}
        self.counts.update(counts or {})

    def to_dict(self) -> dict:
        return {"scrape_budget": self.scrape_budget, "counts": dict(self.counts)}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Usage":
        d = d or {}
        return cls(d.get("scrape_budget", config.MAX_FIRECRAWL_SCRAPES), d.get("counts"))

    def inc(self, k: str) -> None:
        with self.lock:
            self.counts[k] += 1

    def take_scrape(self) -> bool:
        with self.lock:
            if self.scrape_budget <= 0:
                return False
            self.scrape_budget -= 1
            return True

    @property
    def firecrawl_credits(self) -> int:
        return 2 * self.counts["fc_search"] + self.counts["fc_scrape"]


def domain(url: str) -> str:
    d = urlparse(url).netloc.lower()
    return d[4:] if d.startswith("www.") else d


# ---------------------------------------------------------------- cache
def _ckey(kind: str, key: str) -> str:
    return f"cache/{kind}/{hashlib.sha1(key.encode()).hexdigest()[:24]}.json"


def _cache_get(kind: str, key: str):
    v = store.get(_ckey(kind, key))
    if isinstance(v, dict) and time.time() - v.get("t", 0) < config.CACHE_DAYS * 86400:
        return v.get("v")
    return None


def _cache_put(kind: str, key: str, value) -> None:
    try:
        store.put(_ckey(kind, key), {"t": time.time(), "v": value})
    except Exception:
        pass


# ---------------------------------------------------------------- search
def search(query: str, llm, usage: Usage, tier: str = "cheap", limit: int | None = None) -> list[SearchResult]:
    limit = limit or config.RESULTS_PER_QUERY
    cached = _cache_get("search", f"{tier}|{query}")
    if cached is not None:
        usage.inc("cache_hits")
        return [SearchResult(**r) for r in cached]

    def fc():
        if not config.USE_FIRECRAWL:
            return []
        try:
            res = _firecrawl_search(query, limit)
            usage.inc("fc_search")
            return res
        except Exception:
            return []

    def groq():
        try:
            usage.inc("groq_search")
            return [SearchResult(r["url"], r["title"], r["snippet"], query) for r in llm.web_search(query)][: limit + 4]
        except Exception:
            return []

    if tier == "rich":
        results = fc() or groq()
    else:
        results = groq()
        if len(results) < 2:
            results = fc() or results
    if results:
        _cache_put("search", f"{tier}|{query}", [r.__dict__ for r in results])
    return results


def _firecrawl_search(query: str, limit: int) -> list[SearchResult]:
    if config.FIRECRAWL_API_KEY:
        r = httpx.post(
            "https://api.firecrawl.dev/v2/search",
            headers={"Authorization": f"Bearer {config.FIRECRAWL_API_KEY}"},
            json={"query": query, "limit": limit, "sources": [{"type": "web"}]},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
    else:
        data = _fc_cli(["search", _cli_safe(query), "--limit", str(limit), "--sources", "web", "--json"])
    web = (data.get("data") or {}).get("web") or []
    return [
        SearchResult(w.get("url", ""), w.get("title", ""), w.get("description", "") or "", query)
        for w in web
        if w.get("url")
    ]


def _cli_safe(s: str) -> str:
    # the CLI is a .cmd shim on Windows; strip characters cmd.exe would interpret
    return re.sub(r'[&|<>^%!]', " ", s)


def _fc_cli(args: list[str], timeout: int = 90) -> dict:
    if not _FC_BIN:
        raise RuntimeError("firecrawl CLI not found")
    p = subprocess.run(
        [_FC_BIN, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    out = p.stdout.strip()
    start = out.find("{")
    if start < 0:
        raise RuntimeError(f"firecrawl: {p.stderr[:200] or out[:200]}")
    return json.loads(out[start:])


# ---------------------------------------------------------------- fetch
def fetch(url: str, usage: Usage, allow_firecrawl: bool) -> tuple[str, str]:
    """Return (text, method). Empty text if nothing could be fetched."""
    cached = _cache_get("page", url)
    if cached is not None:
        usage.inc("cache_hits")
        return cached["text"], "cache"
    d = domain(url)
    if any(d.endswith(u) for u in SNIPPET_ONLY):
        return "", "skipped"
    text, method = "", "http"
    if d == "github.com":
        text, method = _github(url, usage), "github_api"
    if len(text) < 300:
        usage.inc("http_fetch")
        text, method = _http_fetch(url), "http"
    if len(text) < 600 and allow_firecrawl and config.USE_FIRECRAWL and usage.take_scrape():
        try:
            fc = _firecrawl_scrape(url)
            usage.inc("fc_scrape")
            if len(fc) > len(text):
                text, method = fc, "firecrawl"
        except Exception:
            pass
    if len(text) >= 300:
        _cache_put("page", url, {"text": text})
    return text, method


def _github(url: str, usage: Usage) -> str:
    """Public GitHub API (no key, 60 req/h): profile + top repos for github.com/<user> URLs."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts or parts[0] in ("orgs", "topics", "search", "features", "about"):
        return ""
    user = parts[0]
    try:
        h = {"Accept": "application/vnd.github+json", "User-Agent": "researchr"}
        u = httpx.get(f"https://api.github.com/users/{user}", headers=h, timeout=15)
        usage.inc("github_api")
        if u.status_code != 200:
            return ""
        prof = u.json()
        repos = httpx.get(f"https://api.github.com/users/{user}/repos", params={"sort": "pushed", "per_page": 30},
                          headers=h, timeout=15).json()
        usage.inc("github_api")
    except (httpx.HTTPError, ValueError):
        return ""
    lines = [f"GitHub profile of {prof.get('name') or user} (@{user})"]
    for k in ("bio", "company", "location", "blog", "twitter_username"):
        if prof.get(k):
            lines.append(f"{k}: {prof[k]}")
    lines.append(f"followers: {prof.get('followers')}, public repos: {prof.get('public_repos')}, joined: {str(prof.get('created_at'))[:4]}")
    if isinstance(repos, list):
        own = sorted((r for r in repos if not r.get("fork")), key=lambda r: r.get("stargazers_count", 0), reverse=True)
        langs: dict[str, int] = {}
        for r in own:
            if r.get("language"):
                langs[r["language"]] = langs.get(r["language"], 0) + 1
        if langs:
            lines.append("languages: " + ", ".join(sorted(langs, key=lambda x: -langs[x])[:8]))
        for r in own[:12]:
            lines.append(f"repo {r['name']} ({r.get('stargazers_count', 0)} stars, last push {str(r.get('pushed_at'))[:10]}): "
                         f"{r.get('description') or ''}")
    return "\n".join(lines)


def _http_fetch(url: str) -> str:
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=20,
                      follow_redirects=True)
    except httpx.HTTPError:
        return ""
    if r.status_code != 200:
        return ""
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype:
        return _pdf_text(r.content)
    if "html" not in ctype and "text" not in ctype:
        return ""
    return html_to_text(r.text)


def _pdf_text(blob: bytes) -> str:
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(blob))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:15])
    except Exception:
        return ""


def _firecrawl_scrape(url: str) -> str:
    if config.FIRECRAWL_API_KEY:
        r = httpx.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": f"Bearer {config.FIRECRAWL_API_KEY}"},
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            timeout=90,
        )
        r.raise_for_status()
        return (r.json().get("data") or {}).get("markdown", "")
    data = _fc_cli(["scrape", url, "--only-main-content", "--json"], timeout=120)
    d = data.get("data", data)
    return d.get("markdown", "") if isinstance(d, dict) else ""


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    meta = []
    for sel in ('meta[property="og:title"]', 'meta[name="description"]', 'meta[property="og:description"]'):
        m = soup.select_one(sel)
        if m and m.get("content"):
            meta.append(m["content"].strip())
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "form", "iframe", "header"]):
        tag.decompose()
    lines = list(dict.fromkeys(meta))
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "blockquote", "span", "div"]):
        if el.find(["p", "li", "div", "h1", "h2", "h3"]):
            continue  # only leaf-ish blocks, avoids duplicates
        t = el.get_text(" ", strip=True)
        if len(t) > 25:
            lines.append(t)
    return "\n".join(dict.fromkeys(lines))


def focus_window(text: str, name: str, budget: int | None = None, radius: int = 3) -> str:
    """Keep only paragraphs near mentions of the person (by full name or surname)."""
    budget = budget or config.PAGE_CHAR_BUDGET
    if len(text) <= budget:
        return text
    parts = [p for p in re.split(r"\n+", text) if p.strip()]
    tokens = [t.lower() for t in name.split() if len(t) > 1]
    surname = tokens[-1] if tokens else ""
    hits = [i for i, p in enumerate(parts) if surname and surname in p.lower()]
    if not hits:
        return text[:budget]
    keep = sorted({j for i in hits for j in range(max(0, i - radius), min(len(parts), i + radius + 1))})
    # always keep the page head for context (titles, bios)
    keep = sorted(set(range(min(4, len(parts)))) | set(keep))
    out, size, prev = [], 0, -2
    for i in keep:
        chunk = ("…\n" if i != prev + 1 else "") + parts[i]
        if size + len(chunk) > budget:
            break
        out.append(chunk)
        size += len(chunk)
        prev = i
    return "\n".join(out)
