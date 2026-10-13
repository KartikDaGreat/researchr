"""FastAPI app, used both on Vercel (serverless) and locally.

Local:   python -m uvicorn api.index:app --host 0.0.0.0 --port 8000
Vercel:  /api/* is routed here; public/ is served by the CDN.

Access: the site is public. Each browser makes up a random session id (X-Session) and only ever
sees its own research. Starting research requires a reCAPTCHA v3 pass and is rate-limited.
The owner (X-Token = APP_TOKEN) is exempt from limits and gets profile/me.json as "about me".

On Vercel a run advances in chained invocations: each POST /api/runs/{id}/tick works for up to
STEP_SECONDS, saves state, then fires the next tick. The UI also re-kicks a run whose lease went
stale, so a dropped chain recovers as soon as the app is open.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from researchr import config, jobs  # noqa: E402
from researchr.schemas import Target  # noqa: E402

app = FastAPI(title="researchr")
_local_jobs = ThreadPoolExecutor(max_workers=1)  # local mode: one run at a time (shared Groq budget)
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


@dataclass
class Who:
    scope: str
    owner: bool


def who(request: Request) -> Who:
    tok = request.headers.get("x-token") or request.query_params.get("token")
    if config.APP_TOKEN and tok == config.APP_TOKEN:
        return Who("owner", True)
    sess = request.headers.get("x-session") or request.query_params.get("session") or ""
    if not SESSION_RE.match(sess):
        raise HTTPException(401, "Missing session. Reload the page.")
    return Who("s_" + hashlib.sha256(sess.encode()).hexdigest()[:24], False)


def own_run(rid: str, w: Who) -> dict:
    m = jobs.get_meta(rid)
    if not m or (not w.owner and m.get("scope") != w.scope):
        raise HTTPException(404, "Not found")
    return m


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or request.headers.get("x-real-ip") or
            (request.client.host if request.client else "unknown"))


def verify_captcha(token: str, ip: str) -> None:
    if not config.RECAPTCHA_SECRET:
        if config.ON_VERCEL:
            raise HTTPException(503, "Human verification isn't set up on this site yet, so new research is paused.")
        return  # local dev
    try:
        r = httpx.post("https://www.google.com/recaptcha/api/siteverify",
                       data={"secret": config.RECAPTCHA_SECRET, "response": token, "remoteip": ip}, timeout=10).json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Couldn't reach the verification service. Try again in a moment.")
    if not r.get("success") or r.get("action") not in (None, "research") or r.get("score", 1) < config.RECAPTCHA_MIN_SCORE:
        raise HTTPException(403, "Verification failed. Reload the page and try again.")


class NewRun(BaseModel):
    name: str = Field(max_length=120)
    hints: str = Field("", max_length=400)
    goal: str = Field("", max_length=400)
    context: str = Field("", max_length=400)
    me: str = Field("", max_length=2500)
    captcha: str = ""


class Decision(BaseModel):
    decision: str | None  # "keep" | "skip" | None (undo)


def _base_url(request: Request) -> str:
    prod = os.getenv("VERCEL_PROJECT_PRODUCTION_URL")
    return f"https://{prod}" if prod else str(request.base_url).rstrip("/")


def _kick(request: Request, rid: str) -> None:
    """Fire-and-forget the next tick (Vercel). The callee keeps running after we hang up."""
    try:
        httpx.post(f"{_base_url(request)}/api/runs/{rid}/tick", headers={"x-token": config.APP_TOKEN}, timeout=2.5)
    except httpx.HTTPError:
        pass


@app.get("/api/config")
def site_config():
    return {"recaptcha_site_key": config.RECAPTCHA_SITE_KEY, "runs_per_day": config.RUNS_PER_USER_PER_DAY,
            "verification": bool(config.RECAPTCHA_SECRET) or not config.ON_VERCEL}


@app.get("/api/health")
def health():
    return {"ok": True, "vercel": config.ON_VERCEL, "groq_keys": len(config.GROQ_KEYS),
            "firecrawl": bool(config.FIRECRAWL_API_KEY) or not config.ON_VERCEL,
            "recaptcha": bool(config.RECAPTCHA_SECRET)}


@app.get("/api/runs")
def runs(w: Who = Depends(who)):
    return jobs.list_runs(w.scope)


@app.post("/api/runs")
def new_run(body: NewRun, request: Request, w: Who = Depends(who)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Enter the name of the person you're meeting.")
    if not w.owner:
        ip = client_ip(request)
        verify_captcha(body.captcha, ip)
        err = jobs.check_limits(w.scope, ip)
        if err:
            raise HTTPException(429, err)
    t = Target(name=name, hints=body.hints, goal=body.goal, context=body.context,
               me="" if w.owner else body.me, use_owner_profile=w.owner)
    rid = jobs.create_run(t, scope=w.scope)
    if config.ON_VERCEL:
        _kick(request, rid)  # synchronous: a serverless instance may freeze right after responding
    else:
        _local_jobs.submit(jobs.run_to_completion, rid)
    return {"id": rid}


@app.post("/api/runs/{rid}/tick")
def tick(rid: str, request: Request, w: Who = Depends(who)):
    own_run(rid, w)
    if not config.ON_VERCEL:
        return {"status": (jobs.get_meta(rid) or {}).get("status", "missing")}
    status = jobs.tick(rid, budget=jobs.step_seconds())
    if status == "running":
        _kick(request, rid)
    return {"status": status}


@app.get("/api/runs/{rid}")
def run(rid: str, w: Who = Depends(who)):
    m = own_run(rid, w)
    m.pop("scope", None)
    return m


@app.delete("/api/runs/{rid}")
def delete(rid: str, w: Who = Depends(who)):
    own_run(rid, w)
    jobs.delete_run(rid)
    return {"ok": True}


@app.get("/api/runs/{rid}/cards")
def cards(rid: str, which: str = "pending", w: Who = Depends(who)):
    own_run(rid, w)
    if which not in ("pending", "kept", "skipped", "all"):
        raise HTTPException(400)
    return jobs.cards(rid, which)


@app.post("/api/runs/{rid}/cards/{cid}")
def decide(rid: str, cid: int, body: Decision, w: Who = Depends(who)):
    own_run(rid, w)
    if body.decision not in ("keep", "skip", None):
        raise HTTPException(400)
    return jobs.decide(rid, cid, body.decision)


@app.get("/api/runs/{rid}/report.html")
def report_html(rid: str, w: Who = Depends(who)):
    own_run(rid, w)
    r = jobs.report(rid)
    if not r:
        raise HTTPException(404, "The report isn't ready yet.")
    return HTMLResponse(r["html"], headers={"Cache-Control": "private, no-store"})


@app.get("/api/runs/{rid}/report.md")
def report_md(rid: str, w: Who = Depends(who)):
    m = own_run(rid, w)
    r = jobs.report(rid)
    if not r:
        raise HTTPException(404, "The report isn't ready yet.")
    name = re.sub(r"[^a-z0-9]+", "-", (m.get("title") or "report").lower()).strip("-")
    return PlainTextResponse(r["md"], media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="{name}.md"'})


if not config.ON_VERCEL:  # locally, serve the frontend too (Vercel's CDN does this when deployed)
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent.parent / "public", html=True), name="public")

    @app.on_event("startup")
    def banner():
        print(f"\n  researchr is up: http://localhost:8000   (owner mode: add ?token={config.APP_TOKEN})\n", flush=True)
