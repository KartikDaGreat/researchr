"""The research agent as a resumable state machine.

identity -> plan -> search -> triage -> fetch -> extract -> snippets -> merge -> infer
         -> portrait -> converse -> summarize -> done

Each `step()` does one bounded unit of work (usually one LLM call) and the whole state is plain
JSON, so a serverless function can run a few steps, save, and let the next invocation continue.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import config, prompts, scoring
from .llm import GroqClient, LLMError
from .profile import load_me, me_compact
from .schemas import (FACT_CATEGORIES, Fact, GoalStrategy, Identity, Inference, Insight, InsightPoint, Overlap,
                      RawFact, Report, Source, Target, Theme)
from .search import SNIPPET_ONLY, SearchResult, Usage, domain, fetch, focus_window, search

Emit = Callable[[str, str, float], None]

STAGES = ["identity", "plan", "search", "triage", "fetch", "extract", "snippets", "merge", "infer",
          "portrait", "converse", "advise", "summarize", "done"]
STAGE_PCT = {"identity": 2, "plan": 8, "search": 10, "triage": 24, "fetch": 28, "extract": 32, "snippets": 70,
             "merge": 73, "infer": 78, "portrait": 82, "converse": 88, "advise": 92, "summarize": 96, "done": 100}
# seconds a unit of each stage may take; a step only starts if it can finish before the deadline
STAGE_BUDGET = {"fetch": 150, "search": 60, "extract": 90, "portrait": 90, "converse": 90, "advise": 80, "infer": 80}


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _f(x, default=0.5) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return default


def new_state(target: Target) -> dict:
    return {"target": target.model_dump(), "stage": "identity", "sources": {}, "texts": {}, "identity": None,
            "queries": [], "qi": 0, "cands": [], "ti": 0, "ranked": [], "pages": [], "ei": 0, "raw_facts": [],
            "chunks": [], "mi": 0, "groups": [], "facts": [], "inferences": [], "insights": [], "cg": {},
            "summary": {}, "usage": None, "llm": None, "searches": 0, "started": time.time(), "work_seconds": 0}


class ResearchAgent:
    def __init__(self, state: dict, emit: Emit | None = None, llm: GroqClient | None = None):
        self.st = state
        self.t = Target(**state["target"])
        self.emit = emit or (lambda stage, msg, pct: print(f"[{pct:5.1f}%] {stage}: {msg}", flush=True))
        self.llm = llm or GroqClient()
        self.llm.import_state(state.get("llm"))
        self.usage = Usage.from_dict(state.get("usage"))
        self.sources = {k: Source(**v) for k, v in state["sources"].items()}
        for sid, txt in state.get("texts", {}).items():
            if sid in self.sources:
                self.sources[sid].text = txt
        self.identity = Identity(**state["identity"]) if state.get("identity") else None

    # ------------------------------------------------------------------ state
    def to_state(self) -> dict:
        st = self.st
        st["sources"] = {k: s.model_dump() for k, s in self.sources.items()}
        st["texts"] = {k: s.text for k, s in self.sources.items() if s.text}
        st["identity"] = self.identity.model_dump() if self.identity else None
        st["usage"] = self.usage.to_dict()
        st["llm"] = self.llm.export_state()
        return st

    @property
    def stage(self) -> str:
        return self.st["stage"]

    def _go(self, stage: str, msg: str = "") -> None:
        self.st["stage"] = stage
        if msg:
            self.emit(stage, msg, STAGE_PCT[stage])

    def run(self, deadline: float = float("inf"), save: Callable[[dict], None] | None = None) -> bool:
        """Run steps until done or until the next step might overrun `deadline`. Returns True when done."""
        first = True
        while self.stage != "done":
            # always do at least one unit per call so a run can't stall on a stage bigger than the budget
            if not first and time.time() + STAGE_BUDGET.get(self.stage, 60) > deadline:
                break
            first = False
            t0 = time.time()
            getattr(self, f"_s_{self.stage}")()
            self.st["work_seconds"] = self.st.get("work_seconds", 0) + time.time() - t0
            if save:
                save(self.to_state())
        return self.stage == "done"

    # ------------------------------------------------------------------ helpers
    def _add_results(self, results: list[SearchResult]) -> None:
        url_to_id = {s.url: sid for sid, s in self.sources.items()}
        for r in results:
            url = r.url.split("#")[0]
            if not url.startswith("http") or url in url_to_id:
                continue
            sid = f"S{len(self.sources) + 1}"
            self.sources[sid] = Source(id=sid, url=url, title=_clip(r.title, 140), snippet=_clip(r.snippet, 320),
                                       domain=domain(url))
            url_to_id[url] = sid

    def _run_queries(self, queries: list[str], tier: str = "cheap") -> None:
        with ThreadPoolExecutor(max_workers=2) as ex:
            for res in ex.map(lambda q: search(q, self.llm, self.usage, tier=tier), queries):
                self.st["searches"] += 1
                self._add_results(res)

    def _anchor(self) -> str:
        i = self.identity
        if not i:
            return self.t.name
        bits = [i.canonical_name, i.current_role, i.org, i.location] + i.disambiguators[:5]
        return "; ".join(b for b in bits if b)

    @staticmethod
    def _rows(sources: list[Source]) -> str:
        return "\n".join(f"{s.id} | {s.url} | {s.title} | {s.snippet}" for s in sources)

    def _facts(self) -> list[Fact]:
        return [Fact(**f) for f in self.st["facts"]]

    def _infs(self) -> list[Inference]:
        return [Inference(**i) for i in self.st["inferences"]]

    def _insights(self) -> list[Insight]:
        return [Insight(**i) for i in self.st["insights"]]

    # ------------------------------------------------------------------ stages
    def _s_identity(self) -> None:
        self.emit("identity", f"Finding who '{self.t.name}' is", 2)
        name, hints = self.t.name, self.t.hints.strip()
        seeds = [f'"{name}" {hints}'.strip(), f'"{name}" linkedin {hints}'.strip(), f'"{name}" {hints} profile'.strip()]
        for url in re.findall(r"https?://\S+", hints):
            self._add_results([SearchResult(url, "user-provided", "", "hint")])
        self._run_queries(seeds[:2], tier="rich")  # snippets matter most for identity
        self._run_queries(seeds[2:])
        data = self.llm.json(
            prompts.IDENTITY.format(ethics=prompts.ETHICS, name=name, hints=hints or "(none)",
                                    results=self._rows(list(self.sources.values())[:18])),
            "Resolve the identity.", max_tokens=1200,
        )
        data.setdefault("canonical_name", name)
        self.identity = Identity(**{k: v for k, v in data.items() if k in Identity.model_fields})
        self.identity.confidence = min(_f(self.identity.confidence), 0.97)
        self.identity.known_urls += re.findall(r"https?://\S+", hints)
        self._go("plan", f"Anchor: {self._anchor()}" + (" (AMBIGUOUS)" if self.identity.ambiguous else ""))

    def _s_plan(self) -> None:
        i = self.identity
        n, org = f'"{i.canonical_name}"', i.org or ""
        templates = [
            f"{n} {org}", f"{n} github", f"{n} twitter OR x.com", f"{n} podcast OR interview OR talk",
            f"{n} blog OR medium OR substack", f"{n} {org} news", f"{n} publications OR paper OR patent",
            f"{n} hobbies OR marathon OR music OR photography OR volunteer",
        ]
        if org:
            templates.append(f"{org} news {time.strftime('%Y')}")
        templates = [re.sub(r"\s+", " ", q).strip() for q in templates]
        goal_hint = f", and angles relevant to the goal '{self.t.goal}'" if self.t.goal else ""
        try:
            data = self.llm.json(
                prompts.QUERY_PLAN.format(ethics=prompts.ETHICS, anchor=self._anchor(), goal=self.t.goal or "(none)",
                                          existing="; ".join(templates), n=6, goal_hint=goal_hint),
                "Plan the queries.", max_tokens=900,
            )
            extra = [q for q in data.get("queries", []) if isinstance(q, str)]
        except LLMError:
            extra = []
        self.st["queries"] = list(dict.fromkeys(templates + extra))[: config.MAX_QUERIES]
        self._go("search", f"Running {len(self.st['queries'])} searches")

    def _s_search(self) -> None:
        q, i = self.st["queries"], self.st["qi"]
        self._run_queries(q[i: i + 4])
        self.st["qi"] = i + 4
        pct = STAGE_PCT["search"] + 14 * min(1, self.st["qi"] / max(1, len(q)))
        self.emit("search", f"{min(self.st['qi'], len(q))}/{len(q)} searches · {len(self.sources)} results", pct)
        if self.st["qi"] >= len(q):
            surname = self.identity.canonical_name.split()[-1].lower()
            org = (self.identity.org or "").lower()
            cands = []
            for s in self.sources.values():
                blob = f"{s.title} {s.snippet} {s.url}".lower()
                if surname in blob or (org and org in blob) or s.title == "user-provided":
                    cands.append(s.id)
                else:
                    s.p_same_person, s.value = 0.05, 0.0
            self.st["cands"] = cands
            self._go("triage", f"Scoring {len(cands)} candidate pages")

    def _s_triage(self) -> None:
        cands, i = self.st["cands"], self.st["ti"]
        batch = [self.sources[c] for c in cands[i: i + 22]]
        if batch:
            try:
                data = self.llm.json(prompts.TRIAGE.format(anchor=self._anchor(), results=self._rows(batch)),
                                     "Score every id.", max_tokens=1400)
                for row in data.get("scores", data.get("items", [])):
                    if isinstance(row, dict) and str(row.get("id")) in self.sources:
                        s = self.sources[str(row["id"])]
                        s.p_same_person, s.value = _f(row.get("p_same")), _f(row.get("value"))
            except LLMError:
                pass
        self.st["ti"] = i + 22
        if self.st["ti"] < len(cands):
            return
        known = self.identity.known_urls
        full = self.identity.canonical_name.lower()
        for s in self.sources.values():
            s.kind = scoring.classify_source(s.url, s.domain, self.identity.canonical_name, known)
            if s.kind == "unknown" and full in s.title.lower():
                s.kind = "about"
            if s.kind == "self" and s.p_same_person < 0.7 and any(s.url.startswith(u.rstrip("/")) for u in known):
                s.p_same_person = 0.9
        ranked = sorted((self.sources[c] for c in cands if self.sources[c].p_same_person >= 0.45),
                        key=lambda s: s.p_same_person * (0.4 + s.value), reverse=True)
        self.st["ranked"] = [s.id for s in ranked]
        self._go("fetch", f"Opening up to {config.MAX_PAGES} pages")

    def _s_fetch(self) -> None:
        ranked = [self.sources[r] for r in self.st["ranked"]]
        # spend the page budget on sites we can actually open; walled ones contribute their snippets later
        to_read = [s for s in ranked if not any(s.domain.endswith(d) for d in SNIPPET_ONLY)][: config.MAX_PAGES + 4]

        def get(s: Source):
            # Firecrawl scrape is a fallback reserved for pages that are clearly about the person and valuable
            worth_credit = s.p_same_person >= 0.7 and s.value >= 0.6
            return (s, *fetch(s.url, self.usage, allow_firecrawl=worth_credit))

        with ThreadPoolExecutor(max_workers=4) as ex:
            fetched = list(ex.map(get, to_read))
        pages = []
        for s, text, method in fetched:
            if len(text) >= 300 and len(pages) < config.MAX_PAGES:
                s.fetched, s.fetch_method, s.text = True, method, text
                pages.append(s.id)
        self.st["pages"] = pages
        self._go("extract", f"Reading {len(pages)} pages")

    def _s_extract(self) -> None:
        pages, i = self.st["pages"], self.st["ei"]
        batch = [self.sources[p] for p in pages[i: i + 2]]

        def extract(s: Source):
            try:
                return s, self.llm.json(
                    prompts.EXTRACT.format(ethics=prompts.ETHICS, anchor=self._anchor(), url=s.url,
                                           text=focus_window(s.text, self.identity.canonical_name),
                                           categories=", ".join(FACT_CATEGORIES)),
                    "Extract the facts.", max_tokens=1500,
                )
            except LLMError:
                return s, {}

        with ThreadPoolExecutor(max_workers=2) as ex:
            for s, data in ex.map(extract, batch):
                if "page_about_target" in data:
                    # blend triage guess with the reader's judgement after seeing the full page
                    s.p_same_person = round(0.4 * s.p_same_person + 0.6 * _f(data["page_about_target"]), 3)
                for f in (data.get("facts") or [])[:15]:
                    self._keep_fact(f, s, s.text)
        self.st["ei"] = i + 2
        done = min(self.st["ei"], len(pages))
        self.emit("extract", f"Read {done}/{len(pages)} pages · {len(self.st['raw_facts'])} facts so far",
                  32 + 38 * done / max(1, len(pages)))
        if self.st["ei"] >= len(pages):
            self._go("snippets")

    def _s_snippets(self) -> None:
        ranked = [self.sources[r] for r in self.st["ranked"]]
        unread = [s for s in ranked if not s.fetched and (s.snippet or s.title)][:24]
        if unread:
            try:
                data = self.llm.json(
                    prompts.SNIPPET_EXTRACT.format(ethics=prompts.ETHICS, anchor=self._anchor(),
                                                   results=self._rows(unread), categories=", ".join(FACT_CATEGORIES)),
                    "Extract the facts.", max_tokens=1800,
                )
                for f in data.get("facts", []):
                    if isinstance(f, dict) and str(f.get("source_id")) in self.sources:
                        s = self.sources[str(f["source_id"])]
                        if s.kind == "unknown":
                            s.kind = "snippet"
                        self._keep_fact(f, s, f"{s.title} {s.snippet}")
            except LLMError:
                pass
        self._anchor_check()
        for s in self.sources.values():  # page texts are no longer needed; keep saved state small
            s.text = ""
        self._plan_merge()
        self._go("merge", f"{len(self.st['raw_facts'])} raw facts · merging duplicates")

    def _keep_fact(self, f, s: Source, text: str) -> None:
        if not isinstance(f, dict):
            return
        st = _clip(str(f.get("statement", "")), 300)
        if len(st) < 8 or scoring.is_personal(st):
            return
        cat = f.get("category") if f.get("category") in FACT_CATEGORIES else "other"
        q = _clip(str(f.get("evidence_quote", "")), 300)
        self.st["raw_facts"].append(RawFact(
            statement=st, category=cat, evidence_quote=q, date=str(f.get("date", "") or ""), source_id=s.id,
            quote_verified=scoring.quote_in_text(q, text)).model_dump())

    def _anchor_terms(self) -> set[str]:
        """Distinctive proper nouns from the identity anchor (org, disambiguators), minus the name itself."""
        i = self.identity
        name_toks = {t.lower() for t in i.canonical_name.split()}
        generic = {"co-founder", "cofounder", "founder", "ceo", "cto", "the", "and", "linkedin", "profile", "active",
                   "speaker", "university", "inc", "inc.", "llc", "company", "github", "twitter", "launched",
                   "known", "former", "senior", "engineer", "director", "head", "lead", "followers", "member"}
        terms = set()
        for phrase in [i.org] + i.disambiguators:
            for w in re.findall(r"[A-Z][A-Za-z0-9&.\-]{2,}", phrase or ""):
                w = w.lower().strip(".")
                if w not in name_toks and w not in generic and not w.isdigit():
                    terms.add(w)
        return terms

    def _anchor_check(self) -> None:
        """Namesake guard: a source that never mentions anything from the anchor can't be >50% same-person."""
        terms = self._anchor_terms()
        if not terms:
            return
        known = [u.rstrip("/").lower() for u in self.identity.known_urls]
        for s in self.sources.values():
            if any(s.url.lower().startswith(u) for u in known):
                continue
            blob = f"{s.title} {s.snippet} {s.text[:30000]}".lower()
            if not any(t in blob for t in terms):
                s.p_same_person = min(s.p_same_person, 0.5)

    def _plan_merge(self) -> None:
        raws = self.st["raw_facts"]
        keep = [i for i, r in enumerate(raws) if self.sources[r["source_id"]].p_same_person > 0.5]
        by_cat: dict[str, list[int]] = {}
        for i in keep:
            by_cat.setdefault(raws[i]["category"], []).append(i)
        chunks, cur = [], []
        for idxs in by_cat.values():
            for i in idxs:
                cur.append(i)
                if len(cur) >= 45:
                    chunks.append(cur)
                    cur = []
        if cur:
            chunks.append(cur)
        self.st["chunks"], self.st["mi"], self.st["groups"] = chunks, 0, []

    def _s_merge(self) -> None:
        raws = [RawFact(**r) for r in self.st["raw_facts"]]
        chunks, mi = self.st["chunks"], self.st["mi"]
        if mi < len(chunks):
            chunk = chunks[mi]
            rows = "\n".join(f"r{i} | {raws[i].category} | {raws[i].statement}" for i in chunk)
            seen: set[int] = set()
            try:
                data = self.llm.json(prompts.MERGE.format(facts=rows), "Group them.", max_tokens=2200)
                for g in data.get("groups", data.get("items", [])):
                    if not isinstance(g, dict):
                        continue
                    ids = [int(str(x).lstrip("r")) for x in g.get("ids", []) if str(x).lstrip("r").isdigit()]
                    ids = [i for i in ids if i in chunk and i not in seen]
                    if ids:
                        seen.update(ids)
                        cat = g.get("category") if g.get("category") in FACT_CATEGORIES else raws[ids[0]].category
                        self.st["groups"].append([ids, _clip(g.get("statement") or raws[ids[0]].statement, 300), cat])
            except LLMError:
                pass
            for i in chunk:  # anything the model dropped stays as its own fact
                if i not in seen:
                    self.st["groups"].append([[i], raws[i].statement, raws[i].category])
            self.st["mi"] = mi + 1
            if self.st["mi"] < len(chunks):
                return

        facts = []
        for ids, statement, cat in self.st["groups"]:
            per_source: dict[str, float] = {}
            evidence, dates = [], []
            for i in ids:
                r = raws[i]
                s = self.sources[r.source_id]
                sup = scoring.support(s.p_same_person, s.kind, r.quote_verified)
                per_source[s.id] = max(per_source.get(s.id, 0), sup)  # one vote per source
                if r.evidence_quote and r.quote_verified:
                    evidence.append(r.evidence_quote)
                if r.date:
                    dates.append(r.date)
            facts.append(Fact(id="", statement=statement, category=cat, date=dates[0] if dates else "",
                              source_ids=list(per_source), evidence=evidence[:2],
                              confidence=scoring.noisy_or(list(per_source.values()))))
        facts.sort(key=lambda f: f.confidence, reverse=True)
        facts = [f for f in facts if f.confidence >= 0.2]
        for n, f in enumerate(facts, 1):
            f.id = f"F{n}"
        self.st["facts"] = [f.model_dump() for f in facts]
        self._refresh_identity(facts)
        self._go("infer", f"{len(facts)} facts · drawing inferences")

    def _refresh_identity(self, facts: list[Fact]) -> None:
        """Snippet-based guesses can be stale or wrong; prefer strong, page-verified career/location facts."""
        i = self.identity
        surname = i.canonical_name.split()[-1].lower()
        strong = [f for f in facts if f.confidence >= 0.8 and surname in f.statement.lower()]
        for f in strong:  # "X is the Head of Engineering at Revin." -> role, org (present tense = current)
            m = re.search(r"\b(?:is|serves as|works as)\s+(?:the |a |an )?(.{3,60}?)\s+at\s+([A-Z][\w&.\- ]{1,40}?)[.,]?$",
                          f.statement)
            if f.category == "career" and m:
                i.current_role, i.org = m.group(1).strip(), m.group(2).strip()
                break
        for f in strong:
            m = re.search(r"\b(?:is based|is located|lives) in ([A-Z][\w.\- ]+(?:, [A-Z][\w.\- ]+){0,2})", f.statement)
            if m:
                i.location = m.group(1).strip().rstrip(".")
                break

    def _s_infer(self) -> None:
        facts = self._facts()
        rows = "\n".join(f"{f.id} | {f.confidence:.2f} | {f.statement}" for f in facts[:55])
        out = []
        try:
            data = self.llm.json(
                prompts.INFER.format(ethics=prompts.ETHICS, anchor=self._anchor(), goal=self.t.goal or "(none)", facts=rows),
                "Draw the inferences.", max_tokens=2600, reasoning="medium",
            )
            conf = {f.id: f.confidence for f in facts}
            for d in data.get("inferences", data.get("items", [])):
                if not isinstance(d, dict):
                    continue
                st = _clip(str(d.get("statement", "")), 300)
                if not st or scoring.is_sensitive(st):
                    continue
                # the model often cites ids only inside its reasoning ("... (F3, F12)"), so harvest both
                reasoning = str(d.get("reasoning", ""))
                cited = re.findall(r"F\d+", f"{d.get('based_on', '')} {reasoning}")
                basis = [b for b in dict.fromkeys(cited) if b in conf]
                reasoning = re.sub(r"\s*\((?:F\d+(?:[-–]\d+)?[,;\s]*)+\)", "", reasoning).strip()
                out.append(Inference(id="", statement=st, category=str(d.get("category", "other")),
                                     probability=scoring.calibrate_inference(_f(d.get("probability")), [conf[b] for b in basis]),
                                     reasoning=_clip(reasoning, 400), based_on=basis,
                                     verify_by=_clip(str(d.get("verify_by", "")), 220)))
        except LLMError as e:
            self._fail("infer", e)
        out.sort(key=lambda x: x.probability, reverse=True)
        for n, i in enumerate(out, 1):
            i.id = f"I{n}"
        self.st["inferences"] = [i.model_dump() for i in out]
        self._go("portrait", "Synthesizing a portrait")

    def _s_portrait(self) -> None:
        facts = self._facts()
        fmap = {f.id: f for f in facts}
        rows = "\n".join(f"{f.id} | {f.confidence:.2f} | {f.statement}" for f in facts[:60])
        insights = []
        try:
            data = self.llm.json(
                prompts.PORTRAIT.format(ethics=prompts.ETHICS, anchor=self._anchor(), goal=self.t.goal or "(none)",
                                        facts=rows),
                "Write the portrait.", max_tokens=3200, reasoning="medium",
            )
            for d in data.get("insights", data.get("items", [])):
                if not isinstance(d, dict) or not d.get("title"):
                    continue
                if scoring.is_sensitive(str(d.get("title", "")) + " " + str(d.get("summary", ""))):
                    continue
                points, all_ids = [], []
                for p in d.get("points", []) or []:
                    if isinstance(p, str):
                        p = {"text": p, "facts": []}
                    if not isinstance(p, dict) or not p.get("text"):
                        continue
                    ids = [x for x in p.get("facts", []) or [] if x in fmap]
                    all_ids += ids
                    points.append(InsightPoint(text=_clip(str(p["text"]), 260), facts=ids,
                                               confidence=max((fmap[x].confidence for x in ids), default=0.35)))
                confs = sorted((fmap[x].confidence for x in set(all_ids)), reverse=True)[:4]
                srcs = list(dict.fromkeys(s for x in all_ids for s in fmap[x].source_ids))[:5]
                insights.append(Insight(
                    kind=str(d.get("kind", "work")), title=_clip(str(d["title"]), 120),
                    summary=_clip(str(d.get("summary", "")), 700), points=points[:5],
                    so_what=_clip(str(d.get("so_what", "")), 300),
                    confidence=round(sum(confs) / len(confs), 3) if confs else 0.35, source_ids=srcs))
        except LLMError as e:
            self._fail("portrait", e)
        self.st["insights"] = [i.model_dump() for i in insights]
        self._go("converse", "Mapping common ground and conversation themes")

    def _me_text(self) -> tuple[dict, str]:
        if self.t.use_owner_profile:
            me = load_me(self.llm)
            return me, (me_compact(me) if me else "")
        return {}, _clip(self.t.me, 2500)  # public visitors: whatever they told us about themselves

    def _fail(self, stage: str, e: Exception) -> None:
        self.st.setdefault("errors", []).append(f"{stage}: {_clip(str(e), 200)}")

    def _s_converse(self) -> None:
        me, me_text = self._me_text()
        literal = literal_overlaps(me, self._facts())
        insights = self._insights()
        data = {}
        try:
            data = self.llm.json(
                prompts.COMMON_GROUND.format(
                    ethics=prompts.ETHICS, anchor=self._anchor(),
                    me=me_text or "(not provided: return no overlaps, keep themes general)",
                    insights="\n".join(f"- {i.title}: {i.summary}" for i in insights) or "(none)",
                    overlaps=json.dumps(literal) if literal else "none",
                    goal=self.t.goal or "(none: general relationship building)",
                ),
                "Map it.", max_tokens=2600, reasoning="medium",
            )
        except LLMError as e:
            self._fail("converse", e)
        overlaps = []
        for o in data.get("overlaps", []) or []:
            if isinstance(o, dict) and o.get("mine") and o.get("theirs"):
                overlaps.append(Overlap(type=str(o.get("type", "topic")), mine=_clip(str(o["mine"]), 90),
                                        theirs=_clip(str(o["theirs"]), 90), strength=_f(o.get("strength")),
                                        description=_clip(str(o.get("description", "")), 400)))
        have = " ".join(f"{o.mine} {o.theirs}" for o in overlaps).lower()
        for lo in literal:  # keep literal overlaps the model didn't already cover
            if lo["term"].lower() not in have:
                overlaps.append(Overlap(type=lo["type"], mine=lo["term"], theirs=_clip(lo["evidence"], 90),
                                        strength=0.5, description=f"You both have a connection to {lo['term']}."))
        overlaps.sort(key=lambda o: o.strength, reverse=True)
        themes = []
        for t in data.get("themes", []) or []:
            if isinstance(t, dict) and t.get("theme"):
                try:
                    themes.append(Theme(theme=_clip(str(t["theme"]), 120), why=_clip(str(t.get("why", "")), 500),
                                        angles=[_clip(str(a), 120) for a in t.get("angles", []) or []][:4],
                                        their_depth=_clip(str(t.get("their_depth", "")), 200),
                                        priority=int(t.get("priority", 2) or 2)))
                except (TypeError, ValueError):
                    continue
        themes.sort(key=lambda t: t.priority)
        self.st["cg"] = {"overlaps": [o.model_dump() for o in overlaps], "themes": [t.model_dump() for t in themes],
                         "questions": [], "cautions": [], "strategy": None}
        self._go("advise", "Preparing questions and a game plan")

    def _s_advise(self) -> None:
        _, me_text = self._me_text()
        cg = self.st["cg"]
        data = {}
        try:
            data = self.llm.json(
                prompts.ADVISE.format(
                    ethics=prompts.ETHICS, anchor=self._anchor(), me=me_text or "(not provided)",
                    insights="\n".join(f"- {i.title}" for i in self._insights()) or "(none)",
                    inferences="\n".join(f"{i.id} | {i.probability:.2f} | {i.statement}" for i in self._infs()[:10]),
                    themes="; ".join(t["theme"] for t in cg.get("themes", [])) or "(none)",
                    goal=self.t.goal or "(none: general relationship building)", context=self.t.context or "(none)",
                    strategy_shape=prompts.STRATEGY_SHAPE if self.t.goal else "null",
                ),
                "Prepare me.", max_tokens=2200, reasoning="medium",
            )
        except LLMError as e:
            self._fail("advise", e)
        strat = None
        if self.t.goal and isinstance(data.get("strategy"), dict):
            s = data["strategy"]
            strat = GoalStrategy(
                their_incentives=[str(x) for x in s.get("their_incentives", []) or []],
                framing=str(s.get("framing", "")),
                objections=[o for o in s.get("objections", []) or [] if isinstance(o, dict)],
                ideal_outcome=str(s.get("ideal_outcome", "")), next_step=str(s.get("next_step", "")),
            )
        cg.update(questions=[_clip(str(q), 240) for q in data.get("questions", []) or []][:8],
                  # drop boilerplate etiquette the model adds despite instructions
                  cautions=[_clip(str(c), 240) for c in data.get("cautions", []) or []
                            if not scoring.is_sensitive(str(c)) and not re.search(
                                r"politic|personal life|confidential|proprietary", str(c), re.I)][:5],
                  strategy=strat.model_dump() if strat else None)
        self._go("summarize", "Writing the summary")

    def _s_summarize(self) -> None:
        cg = self.st["cg"]
        try:
            data = self.llm.json(
                prompts.SUMMARY.format(
                    anchor=self._anchor(), goal=self.t.goal or "(none)",
                    insights="; ".join(i.title for i in self._insights()),
                    overlaps="; ".join(f"{o['mine']} / {o['theirs']}" for o in cg.get("overlaps", [])[:3]) or "none",
                ),
                "Write it.", max_tokens=700,
            )
        except LLMError:
            data = {}
        self.st["summary"] = {"summary": _clip(str(data.get("summary", "")), 450),
                              "glance": [_clip(str(g), 40) for g in data.get("glance", []) or []][:6]}
        self._go("done", "Report ready")

    # ------------------------------------------------------------------ live view
    def live(self) -> dict:
        """Small snapshot of what has been collected so far, for the progress screen (kept under ~10KB)."""
        st = self.st
        stage_i = STAGES.index(self.stage)
        triaged = stage_i > STAGES.index("triage")
        ident = None
        if self.identity:
            i = self.identity
            ident = {"name": i.canonical_name, "role": i.current_role, "org": i.org, "location": i.location,
                     "confidence": i.confidence, "ambiguous": i.ambiguous, "note": _clip(i.ambiguity_note, 160),
                     "clues": [_clip(d, 48) for d in i.disambiguators
                               if not re.search(r"https?://|www\.|linkedin\.com/in", d)][:4]}
        srcs = list(self.sources.values())
        recent = srcs[-18:][::-1] if not triaged else sorted(
            srcs, key=lambda s: (s.fetched, s.p_same_person), reverse=True)[:18]
        sources = [{"d": s.domain, "t": _clip(s.title, 70), "url": s.url, "read": s.fetched,
                    "p": round(s.p_same_person, 2) if triaged else None,
                    "out": triaged and s.p_same_person <= 0.5} for s in recent]
        if st["facts"]:  # after merging: the strongest facts with real confidence
            notes = [{"text": _clip(f["statement"], 160), "cat": f["category"], "conf": f["confidence"],
                      "d": self.sources[f["source_ids"][0]].domain if f["source_ids"] else "", "ok": True}
                     for f in st["facts"][:10]]
        else:  # while reading: the latest raw facts, with whether their quote was found on the page
            notes = [{"text": _clip(r["statement"], 160), "cat": r["category"], "conf": None,
                      "d": self.sources[r["source_id"]].domain, "ok": r["quote_verified"]}
                     for r in st["raw_facts"][-10:][::-1]]
        pages_total = len(st["pages"]) if stage_i > STAGES.index("fetch") else 0
        return {
            "stage": self.stage, "identity": ident,
            "queries": {"done": min(st["qi"], len(st["queries"])), "total": len(st["queries"]),
                        "recent": st["queries"][max(0, st["qi"] - 4): st["qi"]][::-1]},
            "counts": {"sources": len(srcs), "candidates": len(st["cands"]),
                       "ruled_out": sum(1 for s in srcs if triaged and s.p_same_person <= 0.5),
                       "pages_read": min(st["ei"], pages_total), "pages_total": pages_total,
                       "raw_facts": len(st["raw_facts"]), "facts": len(st["facts"]),
                       "verified": sum(1 for r in st["raw_facts"] if r["quote_verified"]),
                       "inferences": len(st["inferences"]), "insights": len(st["insights"]),
                       "themes": len(st["cg"].get("themes", [])) if st["cg"] else 0},
            "sources": sources, "notes": notes,
            "insights": [{"title": i["title"], "kind": i["kind"]} for i in st["insights"][:9]],
            "reads": [{"text": _clip(i["statement"], 140), "p": i["probability"]} for i in st["inferences"][:3]],
        }

    # ------------------------------------------------------------------ result
    def report(self) -> Report:
        facts = self._facts()
        used = {sid for f in facts for sid in f.source_ids}
        sources = sorted((s for s in self.sources.values() if s.id in used or s.fetched),
                         key=lambda s: s.p_same_person, reverse=True)
        cg, st = self.st["cg"], self.st
        return Report(
            target=self.t, identity=self.identity, summary=st["summary"].get("summary", ""),
            glance=st["summary"].get("glance", []), insights=self._insights(), facts=facts,
            inferences=self._infs(), overlaps=[Overlap(**o) for o in cg.get("overlaps", [])],
            themes=[Theme(**t) for t in cg.get("themes", [])], questions=cg.get("questions", []),
            cautions=cg.get("cautions", []),
            strategy=GoalStrategy(**cg["strategy"]) if cg.get("strategy") else None,
            footprint=footprint(facts, sources), gaps=gaps(facts), sources=sources,
            stats={"searches": st["searches"], "results": len(self.sources),
                   "pages_read": sum(s.fetched for s in self.sources.values()), "raw_facts": len(st["raw_facts"]),
                   "llm_calls": self.llm.calls, "llm_tokens": self.llm.total_tokens,
                   "seconds": round(st.get("work_seconds", 0)), "firecrawl_credits": self.usage.firecrawl_credits,
                   **self.usage.counts},
        )


# ---------------------------------------------------------------------- helpers
def literal_overlaps(me: dict, facts: list[Fact]) -> list[dict]:
    corpus = " \n".join(f.statement for f in facts).lower()
    out, seen = [], set()
    mapping = {"schools": "school", "orgs": "company", "cities": "city", "skills": "technology",
               "topics": "topic", "interests": "interest"}
    for key, typ in mapping.items():
        for term in me.get(key, []) or []:
            if not isinstance(term, str):
                continue
            t = term.strip()
            core = re.sub(r"\b(university|institute|of|the|inc|ltd|llc|private limited|labs?)\b", " ", t.lower())
            core = re.sub(r"\s+", " ", core).strip()
            if len(core) < 3 or core in seen:
                continue
            if re.search(r"\b" + re.escape(core) + r"\b", corpus):
                seen.add(core)
                ev = next((f.statement for f in facts if core in f.statement.lower()), "")
                out.append({"type": typ, "term": t, "evidence": _clip(ev, 160)})
    return out[:15]


def footprint(facts: list[Fact], sources: list[Source]) -> dict:
    kinds: dict[str, int] = {}
    domains: dict[str, int] = {}
    for s in sources:
        kinds[s.kind] = kinds.get(s.kind, 0) + 1
        domains[s.domain] = domains.get(s.domain, 0) + 1
    cats: dict[str, int] = {}
    for f in facts:
        cats[f.category] = cats.get(f.category, 0) + 1
    return {"source_kinds": kinds, "fact_categories": cats,
            "top_domains": dict(sorted(domains.items(), key=lambda x: -x[1])[:12])}


GAP_LABELS = {
    "code": "No public code / GitHub activity found",
    "talk_media": "No talks, podcasts or interviews found",
    "writing_social": "No blog posts or active social presence found",
    "hobby_interest": "No public hobbies or personal interests found",
    "opinion": "No publicly stated opinions found",
    "publication": "No publications or patents found",
    "news": "No press coverage found",
}


def gaps(facts: list[Fact]) -> list[str]:
    have = {f.category for f in facts if f.confidence >= 0.35}
    return [label for cat, label in GAP_LABELS.items() if cat not in have]
