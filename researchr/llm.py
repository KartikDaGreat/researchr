"""Groq chat client that keeps a run going across keys AND models.

Groq's free tier limits each (organization, model) pair separately: 8K tokens/min and 200K tokens/day,
refilling continuously. So every key x model combination is its own "lane" with its own budget.
Calls go to the best model that still has quota (GROQ_MODELS order: gpt-oss-120b, then gpt-oss-20b,
then qwen), and to the least-busy key within it. A model is only skipped when it is blocked for minutes
on every key (the daily cap), not for a per-minute blip, so quality isn't dropped needlessly.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import config

API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEBUG = os.getenv("RESEARCHR_DEBUG") == "1"
LONG_BLOCK = 90  # seconds; a lane blocked longer than this has hit its daily cap
TOOL_MODELS = ("gpt-oss",)  # only these support Groq's built-in browser_search


class LLMError(RuntimeError):
    pass


class QuotaExhausted(RuntimeError):
    """Every key on every model is blocked for longer than a step can wait (the daily caps).
    Deliberately NOT an LLMError, so stages don't swallow it: the run pauses and resumes later."""

    def __init__(self, wait: float):
        super().__init__(f"Groq quota exhausted on all models; retry in {wait:.0f}s")
        self.wait = wait


@dataclass
class _Lane:
    key_i: int
    key: str
    model: str
    window: deque = field(default_factory=deque)  # [timestamp, tokens] entries in the last 60s
    cooldown_until: float = 0.0

    @property
    def id(self) -> str:
        return f"{self.key_i}|{self.model}"

    def used(self, now: float) -> int:
        while self.window and now - self.window[0][0] > 60:
            self.window.popleft()
        return sum(e[1] for e in self.window)


class GroqClient:
    def __init__(self, keys: list[str] | None = None, models: list[str] | None = None):
        keys = keys or config.GROQ_KEYS
        if not keys:
            raise LLMError("No GROQ_API_KEY / GROQ_API_KEY_2 set (local: .env; hosted: Vercel env vars)")
        self.models = models or config.GROQ_MODELS
        self.lanes = [_Lane(i, k, m) for m in self.models for i, k in enumerate(keys)]
        self.limit = int(config.GROQ_TPM * config.GROQ_TPM_SAFETY)
        self.lock = threading.Lock()
        self.http = httpx.Client(timeout=120)
        self.total_tokens = 0
        self.calls = 0
        self.by_model: dict[str, int] = {}

    # ---- persistence (chained serverless steps share the same windows and cooldowns) ----
    def export_state(self) -> dict:
        with self.lock:
            return {"lanes": {ln.id: {"w": [list(e) for e in ln.window], "c": ln.cooldown_until} for ln in self.lanes},
                    "total_tokens": self.total_tokens, "calls": self.calls, "by_model": dict(self.by_model)}

    def import_state(self, d: dict | None) -> None:
        if not d:
            return
        now = time.time()
        saved = d.get("lanes") or {}
        for ln in self.lanes:
            s = saved.get(ln.id)
            if s:
                ln.window = deque([list(e) for e in s.get("w", []) if now - e[0] <= 60])
                ln.cooldown_until = s.get("c", 0.0)
        self.total_tokens = d.get("total_tokens", 0)
        self.calls = d.get("calls", 0)
        self.by_model = dict(d.get("by_model") or {})

    # ---- choosing a lane -------------------------------------------------
    def _reserve(self, est: int, tools: bool) -> tuple[_Lane, list]:
        est = min(est, self.limit)
        while True:
            with self.lock:
                now = time.time()
                usable_models = [m for m in self.models if not tools or any(t in m for t in TOOL_MODELS)]
                if not usable_models:
                    raise LLMError("no model supports this request")
                for model in usable_models:
                    lanes = [ln for ln in self.lanes if ln.model == model]
                    if all(ln.cooldown_until - now > LONG_BLOCK for ln in lanes):
                        continue  # daily cap on every key for this model: step down to the next model
                    free = [ln for ln in lanes if ln.cooldown_until <= now and ln.used(now) + est <= self.limit]
                    if free:
                        lane = min(free, key=lambda ln: ln.used(now))
                        entry = [now, est]
                        lane.window.append(entry)
                        return lane, entry
                    break  # this model is only briefly busy (per-minute limit): wait for it, keep quality
                else:
                    blocked = [ln for ln in self.lanes if ln.model in usable_models]
                    raise QuotaExhausted(min(ln.cooldown_until for ln in blocked) - now)
            time.sleep(1.0)

    def _settle(self, lane: _Lane, entry: list, actual: int) -> None:
        with self.lock:
            entry[1] = actual  # replace the reservation with the real usage
            if actual:
                self.total_tokens += actual
                self.calls += 1
                self.by_model[lane.model] = self.by_model.get(lane.model, 0) + actual

    # ---- calls ---------------------------------------------------------
    @staticmethod
    def _body(model: str, spec: dict) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": spec["messages"],
                                "max_completion_tokens": spec["max_tokens"], "temperature": spec["temperature"]}
        if "gpt-oss" in model:
            body["reasoning_effort"] = spec["reasoning"]
            body["include_reasoning"] = False
        else:
            body["reasoning_format"] = "hidden"  # e.g. qwen: think, but don't put the thinking in content
        if spec.get("json"):
            body["response_format"] = {"type": "json_object"}
        if spec.get("tools"):
            body["tools"] = spec["tools"]
        return body

    def _post(self, spec: dict, est: int, need_content: bool = True) -> dict:
        """Send with rate limiting, key/model failover and retries; returns the assistant message."""
        last_err = ""
        for attempt in range(8):
            t_wait = time.time()
            lane, entry = self._reserve(est, tools=bool(spec.get("tools")))
            t_call = time.time()
            try:
                r = self.http.post(API_URL, json=self._body(lane.model, spec), headers={"Authorization": f"Bearer {lane.key}"})
            except httpx.HTTPError as e:
                self._settle(lane, entry, 0)
                last_err = str(e)
                time.sleep(2 * (attempt + 1))
                continue
            if DEBUG:
                u = r.json().get("usage", {}) if r.status_code == 200 else {}
                print(f"  [groq] {lane.model} key{lane.key_i} attempt {attempt} waited {t_call - t_wait:.0f}s, call "
                      f"{time.time() - t_call:.0f}s -> {r.status_code} tokens={u.get('total_tokens')} "
                      f"retry-after={r.headers.get('retry-after')} {'' if r.status_code == 200 else r.text[:140]}",
                      file=sys.stderr, flush=True)
            if r.status_code == 429:
                wait = _parse_wait(r.headers.get("retry-after") or r.headers.get("x-ratelimit-reset-tokens"))
                if "per day" in r.text:
                    wait = max(wait, LONG_BLOCK + 1)  # daily cap: move to another key/model right away
                with self.lock:
                    lane.cooldown_until = time.time() + max(wait, 2)
                self._settle(lane, entry, 0)
                last_err = f"rate limited on {lane.model}"
                continue  # _reserve picks the next best lane, or raises QuotaExhausted if none is left
            if r.status_code >= 500:
                self._settle(lane, entry, 0)
                last_err = f"{r.status_code} {r.text[:200]}"
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                self._settle(lane, entry, 0)
                err = r.text[:400]
                if "json_validate_failed" in err and attempt < 4:  # bad JSON from the model: retry cooler
                    spec["temperature"] = 0.1
                    last_err = err
                    continue
                if r.status_code in (400, 404) and ("model" in err.lower() or "tools" in err.lower()):
                    with self.lock:  # this model can't take this request: stop using it for this run
                        for ln in self.lanes:
                            if ln.model == lane.model:
                                ln.cooldown_until = time.time() + 3600
                    last_err = err
                    continue
                raise LLMError(f"Groq {r.status_code}: {err}")
            data = r.json()
            self._settle(lane, entry, data.get("usage", {}).get("total_tokens", est))
            msg = data["choices"][0]["message"]
            if need_content and not (msg.get("content") or "").strip():
                spec["max_tokens"] = int(spec["max_tokens"] * 1.6)  # spent its budget thinking; give more room
                last_err = "empty content"
                continue
            return msg
        raise LLMError(f"Groq call failed after retries: {last_err}")

    def chat(self, messages: list[dict], *, max_tokens: int = 1500, reasoning: str = "low",
             json_mode: bool = False, temperature: float = 0.3) -> str:
        est = int(sum(len(m["content"]) for m in messages) / 3.6) + max_tokens
        spec = {"messages": messages, "max_tokens": max_tokens, "reasoning": reasoning,
                "temperature": temperature, "json": json_mode}
        return self._post(spec, est)["content"]

    def web_search(self, query: str) -> list[dict]:
        """Web search via Groq's built-in browser_search (gpt-oss models only). Returns [{url, title}].
        ~1.5-2K tokens per call; no snippets. Raises if no gpt-oss lane is left (caller falls back to Firecrawl)."""
        spec = {"messages": [{"role": "user", "content": f"Run exactly one web search for: {query}\nThen reply only with the word done."}],
                "max_tokens": 300, "reasoning": "low", "temperature": 0.0, "tools": [{"type": "browser_search"}]}
        msg = self._post(spec, 2200, need_content=False)
        out = []
        for t in msg.get("executed_tools") or []:
            sr = t.get("search_results") or {}
            for r in sr.get("results", []) if isinstance(sr, dict) else []:
                if r.get("url"):
                    out.append({"url": r["url"], "title": r.get("title") or "", "snippet": r.get("content") or ""})
        return out

    def json(self, system: str, user: str, *, max_tokens: int = 1500, reasoning: str = "low") -> dict:
        messages = [
            {"role": "system", "content": system + "\nRespond with a single valid JSON object only."},
            {"role": "user", "content": user},
        ]
        for attempt in range(2):
            text = self.chat(messages, max_tokens=max_tokens, reasoning=reasoning, json_mode=True)
            try:
                data = parse_json(text)
                if isinstance(data, dict):
                    return data
                if isinstance(data, list):  # model returned a bare array; wrap it so callers can .get()
                    return {"items": data}
                raise ValueError("not an object")
            except ValueError:
                messages.append({"role": "assistant", "content": text[:2000]})
                messages.append({"role": "user", "content": "That was not valid JSON. Return only the JSON object."})
        raise LLMError("Model did not return valid JSON")


def _parse_wait(v: str | None) -> float:
    if not v:
        return 5.0
    try:
        return float(v)
    except ValueError:
        pass
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|s|m|h)", v):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total or 5.0


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise ValueError("invalid json")
