"""Groq chat client with key rotation, a per-key token bucket and JSON-mode retries.

Free-tier gpt-oss-20b allows ~8K tokens/minute per key, so every call reserves an
estimated token budget against a sliding 60s window before it is sent.
"""
from __future__ import annotations

import json
import os
import sys
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import config

API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEBUG = os.getenv("RESEARCHR_DEBUG") == "1"


class LLMError(RuntimeError):
    pass


class QuotaExhausted(RuntimeError):
    """Every key is rate-limited for longer than a step can wait (usually the daily token cap).
    Deliberately NOT an LLMError, so stages don't swallow it: the run pauses and resumes later."""

    def __init__(self, wait: float):
        super().__init__(f"Groq quota exhausted; retry in {wait:.0f}s")
        self.wait = wait


@dataclass
class _Key:
    key: str
    window: deque = field(default_factory=deque)  # (timestamp, tokens)
    cooldown_until: float = 0.0

    def used(self, now: float) -> int:
        while self.window and now - self.window[0][0] > 60:
            self.window.popleft()
        return sum(e[1] for e in self.window)


class GroqClient:
    def __init__(self, keys: list[str] | None = None, model: str | None = None):
        keys = keys or config.GROQ_KEYS
        if not keys:
            raise LLMError("No GROQ_API_KEY / GROQ_API_KEY_2 set (local: .env; hosted: Vercel env vars)")
        self.keys = [_Key(k) for k in keys]
        self.model = model or config.GROQ_MODEL
        self.limit = int(config.GROQ_TPM * config.GROQ_TPM_SAFETY)
        self.lock = threading.Lock()
        self.http = httpx.Client(timeout=120)
        self.total_tokens = 0
        self.calls = 0

    # ---- persistence (so chained serverless steps respect the same per-minute window) ----
    def export_state(self) -> dict:
        with self.lock:
            return {"windows": [[list(e) for e in k.window] for k in self.keys],
                    "cooldowns": [k.cooldown_until for k in self.keys],
                    "total_tokens": self.total_tokens, "calls": self.calls}

    def import_state(self, d: dict | None) -> None:
        if not d:
            return
        now = time.time()
        for k, w, c in zip(self.keys, d.get("windows", []), d.get("cooldowns", [])):
            k.window = deque([list(e) for e in w if now - e[0] <= 60])
            k.cooldown_until = c
        self.total_tokens = d.get("total_tokens", 0)
        self.calls = d.get("calls", 0)

    # ---- rate limiting -------------------------------------------------
    def _reserve(self, est: int) -> tuple[_Key, list]:
        est = min(est, self.limit)
        while True:
            with self.lock:
                now = time.time()
                best = None
                for k in self.keys:
                    if k.cooldown_until > now:
                        continue
                    if k.used(now) + est <= self.limit:
                        if best is None or k.used(now) < best.used(now):
                            best = k
                if best:
                    entry = [now, est]
                    best.window.append(entry)
                    return best, entry
            time.sleep(1.0)

    def _settle(self, entry: list, actual: int) -> None:
        with self.lock:
            entry[1] = actual  # replace the reservation with the real usage
            self.total_tokens += actual
            self.calls += 1

    # ---- calls ---------------------------------------------------------
    def _post(self, body: dict, est: int, need_content: bool = True) -> dict:
        """POST with rate limiting and retries; returns the assistant message dict."""
        last_err = ""
        for attempt in range(6):
            t_wait = time.time()
            k, entry = self._reserve(est)
            t_call = time.time()
            try:
                r = self.http.post(API_URL, json=body, headers={"Authorization": f"Bearer {k.key}"})
                if DEBUG:
                    u = r.json().get("usage", {}) if r.status_code == 200 else {}
                    print(f"  [groq] key{self.keys.index(k)} attempt {attempt} waited {t_call - t_wait:.0f}s, call "
                          f"{time.time() - t_call:.0f}s -> {r.status_code} tokens={u.get('total_tokens')} "
                          f"retry-after={r.headers.get('retry-after')} {'' if r.status_code == 200 else r.text[:160]}",
                          file=sys.stderr, flush=True)
            except httpx.HTTPError as e:
                self._settle(entry, 0)
                last_err = str(e)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429:
                wait = _parse_wait(r.headers.get("retry-after") or r.headers.get("x-ratelimit-reset-tokens"))
                with self.lock:
                    k.cooldown_until = time.time() + max(wait, 2)
                    soonest = min(x.cooldown_until for x in self.keys) - time.time()
                self._settle(entry, est)
                if soonest > 90:  # all keys blocked for minutes: daily cap, not a per-minute blip
                    raise QuotaExhausted(soonest)
                last_err = "rate limited"
                continue
            if r.status_code >= 500:
                self._settle(entry, 0)
                last_err = f"{r.status_code} {r.text[:200]}"
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                self._settle(entry, 0)
                err = r.text[:400]
                # json_validate_failed: model produced invalid JSON; retry with lower temperature
                if "json_validate_failed" in err and attempt < 3:
                    body["temperature"] = 0.1
                    last_err = err
                    continue
                raise LLMError(f"Groq {r.status_code}: {err}")
            data = r.json()
            self._settle(entry, data.get("usage", {}).get("total_tokens", est))
            msg = data["choices"][0]["message"]
            if need_content and not (msg.get("content") or "").strip():
                # ran out of tokens on reasoning; give it more room
                body["max_completion_tokens"] = int(body["max_completion_tokens"] * 1.6)
                last_err = "empty content"
                continue
            return msg
        raise LLMError(f"Groq call failed after retries: {last_err}")

    def _body(self, messages, max_tokens, reasoning, temperature) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
            "include_reasoning": False,
        }
        if "gpt-oss" in self.model:
            body["reasoning_effort"] = reasoning
        return body

    def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 1500,
        reasoning: str = "low",
        json_mode: bool = False,
        temperature: float = 0.3,
    ) -> str:
        est = int(sum(len(m["content"]) for m in messages) / 3.6) + max_tokens
        body = self._body(messages, max_tokens, reasoning, temperature)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return self._post(body, est)["content"]

    def web_search(self, query: str) -> list[dict]:
        """Free web search via Groq's built-in browser_search tool. Returns [{url, title}].

        Costs ~1.5-2K Groq tokens per call (the tool output is fed back to the model)
        but no search-API credits. No snippets are returned, only titles and URLs.
        """
        messages = [{"role": "user", "content": f"Run exactly one web search for: {query}\nThen reply only with the word done."}]
        body = self._body(messages, 300, "low", 0.0)
        body["tools"] = [{"type": "browser_search"}]
        msg = self._post(body, 2200, need_content=False)
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
