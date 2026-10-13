"""My own profile (the 'ME' side of common-ground mapping), extracted once from a PDF."""
from __future__ import annotations

import json
from pathlib import Path

from . import config, prompts
from .llm import GroqClient


def load_me(llm: GroqClient | None = None, refresh: bool = False) -> dict:
    if config.ME_JSON.exists() and not refresh:
        return json.loads(config.ME_JSON.read_text(encoding="utf-8"))
    path: Path = config.ME_PROFILE_PATH
    if not path.exists():
        return {}
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        text = "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    else:
        text = path.read_text(encoding="utf-8")
    llm = llm or GroqClient()
    me = llm.json(prompts.ME_PROFILE, text[:14000], max_tokens=2000)
    config.ME_JSON.parent.mkdir(exist_ok=True)
    config.ME_JSON.write_text(json.dumps(me, indent=2), encoding="utf-8")
    return me


def me_compact(me: dict) -> str:
    keys = ["name", "headline", "location", "summary", "orgs", "schools", "cities", "skills", "topics",
            "interests", "notable"]
    return json.dumps({k: me.get(k) for k in keys if me.get(k)}, ensure_ascii=False)[:3500]
