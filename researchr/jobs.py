"""Runs, progress, cards and swipe preferences on top of the JSON store.

Layout:  runs/<id>/meta.json    small: status, progress, title (what the inbox shows)
         runs/<id>/state.json   the agent's resumable state
         runs/<id>/report.json  {"md", "html"} once done
         runs/<id>/cards.json   cards + swipe decisions
         prefs.json             keep/total tallies per card kind (learned from swipes)
"""
from __future__ import annotations

import time
import traceback
import uuid

from . import config
from .llm import QuotaExhausted
from .pipeline import ResearchAgent, new_state
from .report import to_cards, to_html, to_markdown
from .schemas import Target
from .store import store

LEASE_SECONDS = 300  # matches the Vercel function maxDuration


def _k(rid: str, name: str) -> str:
    return f"runs/{rid}/{name}.json"


def create_run(t: Target, scope: str = "owner") -> str:
    rid = uuid.uuid4().hex[:16]  # unguessable: run ids double as capability links
    now = time.time()
    store.put(_k(rid, "state"), new_state(t))
    store.put(_k(rid, "meta"), {"id": rid, "name": t.name, "title": t.name, "subtitle": "", "goal": t.goal,
                                "status": "running", "stage": "queued", "message": "Queued", "pct": 0,
                                "created": now, "finished": None, "error": None, "events": [], "lease_until": 0,
                                "pending": 0, "kept": 0, "scope": scope})
    remember(scope, rid)
    return rid


def get_meta(rid: str) -> dict | None:
    m = store.get(_k(rid, "meta"))
    if m:
        m["stale"] = m["status"] == "running" and m.get("lease_until", 0) < time.time() - 5
    return m


def list_runs(scope: str) -> list[dict]:
    """Runs belonging to one visitor session (or the owner). Index kept per scope so listing stays cheap."""
    rids = store.get(f"scopes/{scope}.json") or []
    metas = [m for m in (get_meta(r) for r in rids) if m]
    for m in metas:
        m.pop("events", None)
        m.pop("live", None)
    return sorted(metas, key=lambda m: m["created"], reverse=True)


def tick(rid: str, budget: float, force: bool = False) -> str:
    """Advance a run for up to `budget` seconds. Returns the run status afterwards."""
    meta = store.get(_k(rid, "meta"))
    if not meta or meta["status"] != "running":
        return meta["status"] if meta else "missing"
    now = time.time()
    if meta.get("lease_until", 0) > now and not force:
        return "busy"
    if not _claim_slot(rid, now):
        meta.update(stage="queued", message="Waiting for a free slot (other research is running)")
        store.put(_k(rid, "meta"), meta)
        return "queued"
    meta["lease_until"] = now + LEASE_SECONDS
    store.put(_k(rid, "meta"), meta)

    def emit(stage, msg, pct):
        meta.update(stage=stage, message=msg, pct=round(pct, 1))
        meta["events"] = (meta.get("events", []) + [{"ts": time.time(), "message": msg}])[-15:]

    agent = None

    def save(state):
        store.put(_k(rid, "state"), state)
        try:
            meta["live"] = agent.live()  # what the progress screen shows
        except Exception:
            pass
        store.put(_k(rid, "meta"), meta)

    try:
        agent = ResearchAgent(store.get(_k(rid, "state")), emit=emit)
        done = agent.run(deadline=now + budget, save=save)
        if done:
            finish(rid, agent, meta)
        else:
            meta["lease_until"] = 0
            store.put(_k(rid, "meta"), meta)
        _release_slot(rid, keep=not done)
    except QuotaExhausted as e:  # pause: state is saved, the run resumes when the quota frees up
        mins = max(1, round(e.wait / 60))
        meta.update(message=f"Paused: the free AI quota ran out. Resumes in about {mins} min.",
                    lease_until=time.time() + e.wait)
        meta["events"] = (meta.get("events", []) + [{"ts": time.time(), "message": meta["message"]}])[-15:]
        store.put(_k(rid, "meta"), meta)
        _release_slot(rid, keep=False)
        return "paused"
    except Exception as e:  # surface the failure in the UI instead of dying silently
        meta.update(status="error", error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}",
                    finished=time.time(), lease_until=0)
        store.put(_k(rid, "meta"), meta)
        _release_slot(rid, keep=False)
    return meta["status"]


# ------------------------------------------------------------------ concurrency + limits
def _claim_slot(rid: str, now: float) -> bool:
    """At most MAX_ACTIVE_RUNS runs hold a slot at once (they share one Groq per-minute budget).
    A run keeps its slot between chained ticks; slots of runs that went quiet expire."""
    if not config.ON_VERCEL:
        return True
    active = {k: v for k, v in (store.get("active.json") or {}).items() if v > now}
    if rid not in active and len(active) >= config.MAX_ACTIVE_RUNS:
        return False
    active[rid] = now + LEASE_SECONDS + 120
    store.put("active.json", active)
    return True


def _release_slot(rid: str, keep: bool) -> None:
    if not config.ON_VERCEL:
        return
    active = store.get("active.json") or {}
    if keep:
        active[rid] = time.time() + 180  # chained tick should arrive within seconds
    else:
        active.pop(rid, None)
    store.put("active.json", active)


def check_limits(scope: str, ip: str) -> str | None:
    """Count a new public run against the daily limits. Returns an error message if over."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    key = f"limits/{day}.json"
    lim = store.get(key) or {"total": 0, "s": {}, "ip": {}}
    if lim["total"] >= config.RUNS_PER_DAY_TOTAL:
        return "The site has reached its research limit for today. Try again after midnight UTC."
    if lim["s"].get(scope, 0) >= config.RUNS_PER_USER_PER_DAY or lim["ip"].get(ip, 0) >= config.RUNS_PER_IP_PER_DAY:
        return f"You can start {config.RUNS_PER_USER_PER_DAY} researches a day. Try again after midnight UTC."
    lim["total"] += 1
    lim["s"][scope] = lim["s"].get(scope, 0) + 1
    lim["ip"][ip] = lim["ip"].get(ip, 0) + 1
    store.put(key, lim)
    return None


def remember(scope: str, rid: str) -> None:
    ids = store.get(f"scopes/{scope}.json") or []
    store.put(f"scopes/{scope}.json", [rid] + [i for i in ids if i != rid][:199])


def forget(scope: str, rid: str) -> None:
    ids = store.get(f"scopes/{scope}.json") or []
    store.put(f"scopes/{scope}.json", [i for i in ids if i != rid])


def finish(rid: str, agent: ResearchAgent, meta: dict) -> None:
    report = agent.report()
    cards = to_cards(report)
    for n, c in enumerate(cards):
        c["id"], c["decision"] = n, None
    store.put(_k(rid, "report"), {"md": to_markdown(report), "html": to_html(report)})
    store.put(_k(rid, "cards"), cards)
    ident = report.identity
    meta.update(status="done", finished=time.time(), pct=100, lease_until=0, title=ident.canonical_name,
                subtitle=" @ ".join(x for x in (ident.current_role, ident.org) if x),
                pending=len(cards), kept=0, credits=report.stats.get("firecrawl_credits", 0))
    store.put(_k(rid, "meta"), meta)


def run_to_completion(rid: str) -> str:
    """Local mode: no time limit, no chaining."""
    status = "running"
    while status in ("running", "busy", "queued", "paused"):
        if status == "paused":
            time.sleep(max(5, (store.get(_k(rid, "meta")) or {}).get("lease_until", 0) - time.time()))
        status = tick(rid, budget=float("inf"), force=True)
    return status


# ------------------------------------------------------------------ cards + swipes
def cards(rid: str, which: str = "pending") -> list[dict]:
    allc = store.get(_k(rid, "cards")) or []
    want = {"pending": lambda c: c["decision"] is None, "kept": lambda c: c["decision"] == "keep",
            "skipped": lambda c: c["decision"] == "skip", "all": lambda c: True}[which]
    prefs = store.get("prefs.json") or {}
    rate = lambda key: (prefs.get(key, [0, 0])[0] + 1) / (prefs.get(key, [0, 0])[1] + 2)
    out = [c for c in allc if want(c)]
    for c in out:
        r = rate(f"{c['kind']}:{c['category']}") if f"{c['kind']}:{c['category']}" in prefs else rate(c["kind"])
        c["score"] = round((c.get("base") or 0.5) * (0.5 + r), 4)
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def decide(rid: str, cid: int, decision: str | None) -> dict:
    allc = store.get(_k(rid, "cards")) or []
    c = next((x for x in allc if x["id"] == cid), None)
    if not c:
        return {"ok": False}
    old, c["decision"] = c["decision"], decision
    store.put(_k(rid, "cards"), allc)
    prefs = store.get("prefs.json") or {}
    for key in (c["kind"], f"{c['kind']}:{c['category']}"):
        keep, n = prefs.get(key, [0, 0])
        if old:
            keep, n = keep - (old == "keep"), n - 1
        if decision:
            keep, n = keep + (decision == "keep"), n + 1
        prefs[key] = [max(0, keep), max(0, n)]
    store.put("prefs.json", prefs)
    meta = store.get(_k(rid, "meta"))
    if meta:
        meta["pending"] = sum(x["decision"] is None for x in allc)
        meta["kept"] = sum(x["decision"] == "keep" for x in allc)
        store.put(_k(rid, "meta"), meta)
    return {"ok": True}


def report(rid: str) -> dict | None:
    return store.get(_k(rid, "report"))


def delete_run(rid: str) -> None:
    meta = store.get(_k(rid, "meta")) or {}
    forget(meta.get("scope", "owner"), rid)
    store.delete_prefix(f"runs/{rid}/")


def prefs() -> dict:
    p = store.get("prefs.json") or {}
    return {k: round((v[0] + 1) / (v[1] + 2), 3) for k, v in p.items()}


def step_seconds() -> float:
    return config.STEP_SECONDS if config.ON_VERCEL else float("inf")
