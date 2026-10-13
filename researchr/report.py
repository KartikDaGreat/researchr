"""Render a Report as Markdown and HTML, and turn it into rich swipeable cards."""
from __future__ import annotations

import html
from datetime import datetime

from .schemas import Report
from .scoring import label

CAT_TITLES = {
    "career": "Career", "education": "Education", "publication": "Publications & patents",
    "talk_media": "Talks, podcasts & media", "code": "Code & open source", "writing_social": "Writing & social",
    "award": "Awards", "organization": "Organizations & communities", "hobby_interest": "Interests & hobbies",
    "opinion": "Public views", "news": "In the news", "company_context": "Their organization", "other": "Other",
}
INSIGHT_KINDS = {"background": "Background", "work": "Work", "views": "Views", "interests": "Interests",
                 "company": "Their organization", "style": "Style"}


def pct(p: float) -> str:
    return f"{round(p * 100)}%"


# ------------------------------------------------------------------ markdown
def to_markdown(r: Report) -> str:
    i = r.identity
    L = [f"# {i.canonical_name}", ""]
    L.append(f"**{i.current_role}{' @ ' + i.org if i.org else ''}**" + (f" · {i.location}" if i.location else ""))
    L.append(f"*Researched {datetime.now():%b %d, %Y} · identity confidence {pct(i.confidence)}*")
    if i.ambiguous:
        L += ["", f"> ⚠️ **Possible mix-up:** {i.ambiguity_note}"]
    L += ["", r.summary]
    if r.glance:
        L += ["", " · ".join(f"`{g}`" for g in r.glance)]
    if r.target.goal:
        L += ["", f"**Meeting goal:** {r.target.goal}"]

    if r.strategy:
        s = r.strategy
        L += ["", "## Game plan", "", s.framing]
        if s.their_incentives:
            L += ["", "**What they likely care about:** " + "; ".join(s.their_incentives)]
        if s.objections:
            L += ["", "**Likely pushback:**"]
            L += [f"- *{o.get('objection', '')}*: {o.get('response', '')}" for o in s.objections]
        if s.ideal_outcome or s.next_step:
            L += ["", f"**Aim for:** {s.ideal_outcome}  ", f"**Next step to propose:** {s.next_step}"]

    if r.insights:
        L += ["", "## Portrait"]
        for x in r.insights:
            L += ["", f"### {x.title}", f"*{INSIGHT_KINDS.get(x.kind, x.kind.title())} · {pct(x.confidence)} evidence strength*",
                  "", x.summary]
            L += [f"- {p.text} ({pct(p.confidence)})" for p in x.points]
            if x.so_what:
                L += ["", f"**Why it matters:** {x.so_what}"]

    if r.overlaps:
        L += ["", "## Common ground"]
        for o in r.overlaps:
            L += [f"- **{o.mine} ↔ {o.theirs}** ({pct(o.strength)}). {o.description}"]

    if r.themes:
        L += ["", "## Conversation themes"]
        for t in r.themes:
            L += ["", f"**{t.theme}**: {t.why}"]
            if t.angles:
                L.append("Angles: " + " · ".join(t.angles))
            if t.their_depth:
                L.append(f"*Their depth: {t.their_depth}*")
    if r.questions:
        L += ["", "**Questions worth asking**", *[f"- {q}" for q in r.questions]]
    if r.cautions:
        L += ["", "**Handle with care**", *[f"- {c}" for c in r.cautions]]

    if r.inferences:
        L += ["", "## Reads on them", "*Reasoned guesses, not facts.*", "",
              "| Read | Probability | Why | Verify by asking |", "|---|---|---|---|"]
        for x in r.inferences:
            L.append(f"| {x.statement} | {pct(x.probability)} ({label(x.probability)}) | {x.reasoning} | {x.verify_by} |")

    L += ["", "## Public footprint"]
    if r.footprint.get("top_domains"):
        L.append("Shows up on: " + ", ".join(f"{k} ({v})" for k, v in r.footprint["top_domains"].items()))
    if r.gaps:
        L += ["", "Searched, nothing found: " + "; ".join(r.gaps)]

    L += ["", "## Evidence (all facts)"]
    for cat, title in CAT_TITLES.items():
        fs = [f for f in r.facts if f.category == cat]
        if fs:
            L += ["", f"**{title}**"]
            L += [f"- {f.statement} ({pct(f.confidence)}) {' '.join('[' + s + ']' for s in f.source_ids)}" for f in fs]

    L += ["", "## Sources", ""]
    L += [f"- [{s.id}] [{s.title or s.url}]({s.url}) · {s.kind}, same-person {pct(s.p_same_person)}" for s in r.sources]
    st = r.stats
    models = ", ".join(m.split("/")[-1] for m in (st.get("models") or {}))
    L += ["", "---", f"*{('Models: ' + models + '. ') if models else ''}{st.get('searches', 0)} searches · {st.get('pages_read', 0)} pages · {st.get('llm_calls', 0)} LLM calls · "
          f"{st.get('firecrawl_credits', 0)} Firecrawl credits · {st.get('seconds', 0)}s of work. Fact confidence = noisy-OR "
          "over sources (same-person × reliability × verified quote); reads are shrunk toward 50% on thin evidence.*"]
    return "\n".join(L)


# ------------------------------------------------------------------ html
def _e(s) -> str:
    return html.escape(str(s or ""))


def _bar(p: float) -> str:
    hue = 140 if p >= 0.75 else 45 if p >= 0.5 else 15
    return (f'<span class="bar"><span style="width:{round(p * 100)}%;background:hsl({hue} 60% 45%)"></span></span>'
            f'<span class="pct">{pct(p)}</span>')


def to_html(r: Report) -> str:
    i = r.identity
    src = {s.id: s for s in r.sources}

    def links(ids):
        return " ".join(f'<a class="src" href="{_e(src[x].url)}" target="_blank" title="{_e(src[x].title)}">{x}</a>'
                        for x in ids if x in src)

    H = [f'<header><h1>{_e(i.canonical_name)}</h1><p class="role">{_e(i.current_role)}'
         f'{" @ " + _e(i.org) if i.org else ""}{" · " + _e(i.location) if i.location else ""}</p>'
         f'<p class="meta">Researched {datetime.now():%b %d, %Y} · identity {_bar(i.confidence)}</p></header>']
    if i.ambiguous:
        H.append(f'<div class="warn">⚠️ <b>Possible mix-up:</b> {_e(i.ambiguity_note)}</div>')
    H.append(f'<section class="lead"><p class="summary">{_e(r.summary)}</p>'
             + "".join(f'<span class="chip">{_e(g)}</span>' for g in r.glance)
             + (f'<p class="goal"><b>Goal:</b> {_e(r.target.goal)}</p>' if r.target.goal else "") + "</section>")

    if r.strategy:
        s = r.strategy
        H.append(f"<section><h2>Game plan</h2><p>{_e(s.framing)}</p>")
        if s.their_incentives:
            H.append("<h3>What they likely care about</h3><ul>" + "".join(f"<li>{_e(x)}</li>" for x in s.their_incentives) + "</ul>")
        if s.objections:
            H.append("<h3>Likely pushback</h3><ul>" + "".join(
                f"<li><i>{_e(o.get('objection'))}</i><br>{_e(o.get('response'))}</li>" for o in s.objections) + "</ul>")
        H.append(f"<p><b>Aim for:</b> {_e(s.ideal_outcome)}<br><b>Next step:</b> {_e(s.next_step)}</p></section>")

    if r.insights:
        H.append("<section><h2>Portrait</h2>")
        for x in r.insights:
            H.append(f'<div class="ins"><div class="ins-k">{_e(INSIGHT_KINDS.get(x.kind, x.kind.title()))} {_bar(x.confidence)}</div>'
                     f'<h3 class="ins-t">{_e(x.title)}</h3><p>{_e(x.summary)}</p><ul>'
                     + "".join(f'<li>{_e(p.text)} <span class="pct">{pct(p.confidence)}</span></li>' for p in x.points)
                     + "</ul>" + (f'<p class="sowhat"><b>Why it matters:</b> {_e(x.so_what)}</p>' if x.so_what else "")
                     + f'<div>{links(x.source_ids)}</div></div>')
        H.append("</section>")

    if r.overlaps:
        H.append('<section><h2>Common ground</h2><div class="ov">')
        for o in r.overlaps:
            H.append(f'<div class="ovc"><span class="tag">{_e(o.type)}</span><div class="ov-t"><b>{_e(o.mine)}</b> ↔ '
                     f'<b>{_e(o.theirs)}</b></div><p>{_e(o.description)}</p>{_bar(o.strength)}</div>')
        H.append("</div></section>")

    if r.themes or r.questions or r.cautions:
        H.append("<section><h2>Conversation themes</h2>")
        for t in r.themes:
            H.append(f'<div class="tp"><b>{_e(t.theme)}</b><p>{_e(t.why)}</p>'
                     + "".join(f'<span class="chip">{_e(a)}</span>' for a in t.angles)
                     + (f'<p class="muted">Their depth: {_e(t.their_depth)}</p>' if t.their_depth else "") + "</div>")
        if r.questions:
            H.append("<h3>Questions worth asking</h3><ul>" + "".join(f"<li>{_e(q)}</li>" for q in r.questions) + "</ul>")
        if r.cautions:
            H.append("<h3>Handle with care</h3><ul>" + "".join(f"<li>{_e(c)}</li>" for c in r.cautions) + "</ul>")
        H.append("</section>")

    if r.inferences:
        H.append('<section><h2>Reads on them <small>reasoned guesses</small></h2><ul class="infs">')
        for x in r.inferences:
            H.append(f'<li><b>{_e(x.statement)}</b><div>{_bar(x.probability)} <span class="muted">{label(x.probability)}</span></div>'
                     f'<div class="muted">{_e(x.reasoning)}</div><div>Verify: <i>{_e(x.verify_by)}</i></div></li>')
        H.append("</ul></section>")

    H.append("<section><h2>Public footprint</h2>")
    if r.footprint.get("top_domains"):
        H.append("<p>" + "".join(f'<span class="chip">{_e(k)} · {v}</span>' for k, v in r.footprint["top_domains"].items()) + "</p>")
    if r.gaps:
        H.append('<p class="muted">Searched, nothing found: ' + "; ".join(_e(g) for g in r.gaps) + "</p>")
    H.append("</section>")

    H.append(f"<section><details><summary><h2>Evidence <small>{len(r.facts)} sourced facts</small></h2></summary>")
    for cat, title in CAT_TITLES.items():
        fs = [f for f in r.facts if f.category == cat]
        if fs:
            H.append(f"<h3>{title}</h3><ul class='facts'>" + "".join(
                f"<li>{_e(f.statement)} {_bar(f.confidence)} {links(f.source_ids)}</li>" for f in fs) + "</ul>")
    H.append("</details></section>")

    H.append("<section><details><summary><h2>Sources</h2></summary><ol class='sources'>")
    for s in r.sources:
        H.append(f'<li><b>{s.id}</b> <a href="{_e(s.url)}" target="_blank">{_e(s.title or s.url)}</a> '
                 f'<span class="muted">{_e(s.domain)} · {s.kind} · same-person {pct(s.p_same_person)}</span></li>')
    H.append("</ol></details></section>")
    st = r.stats
    models = ", ".join(m.split("/")[-1] for m in (st.get("models") or {}))
    H.append(f'<footer>{("Models: " + models + ". ") if models else ""}{st.get("searches", 0)} searches · {st.get("pages_read", 0)} pages · {st.get("llm_calls", 0)} LLM calls · '
             f'{st.get("firecrawl_credits", 0)} Firecrawl credits · {st.get("seconds", 0)}s of work</footer>')
    return REPORT_SHELL.replace("{{title}}", _e(i.canonical_name)).replace("{{body}}", "\n".join(H))


REPORT_SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}} · researchr</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Familjen+Grotesk:wght@400;600;700&family=Literata:opsz,wght@7..72,400;7..72,500&display=swap" rel="stylesheet">
<style>
:root{--desk:#DCE3E9;--card:#FDFDFA;--fg:#1D2B3A;--muted:#4D5B6A;--line:#D5DFEA;--red:#C8424A;--pine:#2E6A4E;--warn:#FBEDEA}
@media (prefers-color-scheme:dark){:root{--desk:#12171D;--card:#1C232B;--fg:#E3E8EE;--muted:#A3AFBC;--line:#2C3947;--red:#E0646B;--pine:#62B28C;--warn:#3A2224}}
*{box-sizing:border-box}body{margin:0;background:var(--desk);color:var(--fg);font:16px/1.65 "Literata",Georgia,serif}
main{max-width:760px;margin:0 auto;padding:18px 14px 60px}
h1,h2,h3,.role,.meta,.chip,.tag,.ins-k,.pct,footer,summary{font-family:"Familjen Grotesk",system-ui,sans-serif}
h1{margin:0;font-size:32px;letter-spacing:-.02em;line-height:1.15}h2{font-size:19px;margin:0 0 10px;display:inline}
h2 small{font-weight:400;color:var(--muted);font-size:14px}h3{font-size:14px;margin:18px 0 6px;color:var(--muted);font-weight:600}
section,header{background:var(--card);border-radius:6px;padding:16px 18px;margin:14px 0;box-shadow:0 1px 0 rgba(29,43,58,.06),0 10px 24px -14px rgba(29,43,58,.35)}
header{border-bottom:2px solid var(--red);border-radius:6px 6px 0 0}section>h2{display:block;border-bottom:2px solid var(--red);padding-bottom:6px;margin:-2px 0 12px}
.role{margin:4px 0;font-weight:600}.meta,.muted{color:var(--muted);font-size:14px}.warn{background:var(--warn);padding:10px 12px;border-radius:6px}
.summary{font-size:18px;line-height:1.6;margin:0 0 10px}.goal{margin:10px 0 0}
.bar{display:inline-block;width:54px;height:5px;background:var(--line);border-radius:3px;vertical-align:middle;overflow:hidden}.bar span{display:block;height:100%}
.pct{font-size:12px;color:var(--muted);margin-left:5px;font-variant-numeric:tabular-nums}
.chip,.tag{display:inline-block;font-size:13px;padding:1px 10px;border-radius:99px;border:1px solid var(--line);margin:0 5px 6px 0}
.ins{border-top:1px solid var(--line);padding:14px 0 6px}.ins:first-of-type{border-top:0;padding-top:0}.ins-k{font-size:13px;color:var(--muted)}
.ins-t{font-size:19px;color:var(--fg);margin:2px 0 4px;font-weight:700}.ins ul{margin:6px 0;padding-left:18px}
.sowhat{border-left:2px solid var(--red);padding:2px 0 2px 12px;font-size:15px}
.ov{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}.ovc{border-top:2px solid var(--pine);padding:8px 0;font-size:15px}.ovc p{margin:6px 0}
.tp{border-bottom:1px solid var(--line);padding:10px 0}.tp p{margin:4px 0 6px}
ul.facts,ul.infs{list-style:none;padding:0;margin:0}ul.facts li,ul.infs li{padding:8px 0;border-bottom:1px solid var(--line);font-size:15px}
.src{font:12px "Familjen Grotesk",sans-serif;text-decoration:none;border:1px solid var(--line);color:var(--fg);padding:0 5px;border-radius:4px}a{color:var(--fg);text-decoration-color:var(--red)}
details summary{cursor:pointer;list-style:none}details summary::-webkit-details-marker{display:none}details summary h2::after{content:" +";color:var(--red)}details[open] summary h2::after{content:" –"}
ol.sources li{margin:4px 0;word-break:break-word;font-size:14px}footer{color:var(--muted);font-size:13px;text-align:center;margin-top:20px}
</style></head><body><main>{{body}}</main></body></html>"""


# ------------------------------------------------------------------ cards
def to_cards(r: Report) -> list[dict]:
    """Rich notification cards. Each has a headline, a paragraph, supporting points and labelled sections."""
    src = {s.id: s for s in r.sources}
    fmap = {f.id: f for f in r.facts}

    def srcs(ids):
        out, seen = [], set()
        for x in ids:
            for sid in ([x] if x in src else fmap[x].source_ids if x in fmap else []):
                if sid in src and sid not in seen:
                    seen.add(sid)
                    out.append({"id": sid, "url": src[sid].url, "title": src[sid].title or src[sid].domain})
        return out[:4]

    def card(kind, category, title, summary="", points=None, sections=None, confidence=None, base=0.6, sources=None):
        return {"kind": kind, "category": category, "title": title, "summary": summary, "points": points or [],
                "sections": [s for s in (sections or []) if s.get("text")], "confidence": confidence, "base": base,
                "sources": sources or []}

    cards = []
    if r.strategy:
        s = r.strategy
        secs = [{"label": "What they likely care about", "text": " · ".join(s.their_incentives)}]
        secs += [{"label": f"If they say: {o.get('objection', '')}", "text": o.get("response", "")} for o in s.objections]
        secs += [{"label": "Aim for", "text": s.ideal_outcome}, {"label": "Next step", "text": s.next_step}]
        cards.append(card("strategy", "goal", "Your game plan", s.framing, sections=secs, base=0.99))
    for x in r.insights:
        cards.append(card("insight", x.kind, x.title, x.summary,
                          points=[{"text": p.text, "confidence": p.confidence} for p in x.points],
                          sections=[{"label": "Why it matters", "text": x.so_what}], confidence=x.confidence,
                          base=0.55 + 0.4 * x.confidence, sources=srcs(x.source_ids)))
    for t in r.themes:
        cards.append(card("theme", "conversation", t.theme, t.why, points=[{"text": a} for a in t.angles],
                          sections=[{"label": "How much they've said about it", "text": t.their_depth}],
                          base={1: 0.92, 2: 0.78, 3: 0.62}.get(t.priority, 0.7)))
    for o in r.overlaps:
        cards.append(card("overlap", o.type, f"{o.mine} ↔ {o.theirs}", o.description,
                          sections=[{"label": "Your side", "text": o.mine}, {"label": "Their side", "text": o.theirs}],
                          confidence=o.strength, base=0.5 + 0.45 * o.strength))
    for x in r.inferences:
        basis = [{"text": fmap[b].statement, "confidence": fmap[b].confidence} for b in x.based_on if b in fmap][:4]
        cards.append(card("inference", x.category, x.statement, x.reasoning, points=basis,
                          sections=[{"label": "Verify by asking", "text": x.verify_by}], confidence=x.probability,
                          base=0.35 + 0.5 * x.probability, sources=srcs(x.based_on)))
    if r.questions:
        cards.append(card("questions", "conversation", "Questions worth asking",
                          "Open-ended questions that also test the less certain reads on them.",
                          points=[{"text": q} for q in r.questions], base=0.8))
    if r.cautions:
        cards.append(card("caution", "caution", "Handle with care", "", points=[{"text": c} for c in r.cautions], base=0.75))
    return cards
