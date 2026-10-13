# researchr: design notes

Given a person's name (plus optional details and a meeting goal), researchr collects the **public** information about them. It turns that into verified facts with confidence scores, synthesizes a portrait, draws probability-labelled reads, and maps common ground and conversation themes. The results come out as (a) a readable report and (b) a deck of cards to sort on your phone.

---

## 1. Constraints that shape the design

| Constraint | Consequence |
|---|---|
| LLM is Groq `openai/gpt-oss-20b` on the free tier: **8K tokens/min and 200K tokens/day per key**, 2 keys | Per-key token bucket with rotation, compact prompts, and a "focus window" that sends only page text near mentions of the person. A run uses ~80–110K tokens, so the free tier supports **about 4 runs a day**. Public limits are set to match. When the daily cap is hit, the run pauses and resumes later instead of failing. |
| A 20B model hallucinates more than frontier models | Every fact carries a **verbatim evidence quote**, and code checks it against the page. Probabilities are computed in code from evidence, not taken from the model. |
| Vercel Hobby functions stop after 300s; a run takes 5+ minutes | The agent is a **resumable state machine**. Each step is one bounded unit, state is JSON in a private Vercel Blob store, and each invocation works ≤200s and then chains to the next. |
| Firecrawl credits are scarce (~1,000/month) | **Tiered search.** Identity seeds use Firecrawl (snippets matter there). Coverage queries use Groq's built-in `browser_search`, falling back to Firecrawl when that comes back nearly empty. Pages are fetched via cache → GitHub API → plain HTTP → Firecrawl (only for high-value pages, max 3 per run). A 7-day cache makes re-runs nearly free. Typical cost: 0–6 credits per run. |
| Common names mean wrong-person contamination | An explicit **identity** step builds an anchor. Every source gets `p_same_person`, which multiplies into every fact it supports. A namesake guard caps any source that never mentions the anchor at 50%, and those sources are excluded from facts. |
| Public site | Each browser has an anonymous session and sees only its own research. Starting research requires reCAPTCHA v3 and is rate-limited. Public, professional information only: addresses, family, health, religion, politics and other protected traits are filtered in prompts and in code. |

---

## 2. Pipeline

```
identity → plan → search → triage → fetch → extract → snippets → merge → infer → portrait → converse → advise → summarize
```

- **identity:** seed searches → anchor `{name, role, org, location, disambiguators, ambiguity}`.
- **plan / search:** template queries (GitHub, X, podcasts, writing, news, their organization, hobbies) plus 6 queries planned by the LLM.
- **triage:** the LLM scores each result's `p_same_person` and `value`; the best ~12 pages are read.
- **extract:** per-page facts `{statement, category, evidence_quote, date}`. Code checks each quote against the page.
- **snippets:** facts from search snippets of pages that can't be opened (LinkedIn etc.), as weaker evidence.
- **merge:** the LLM groups duplicates. Code scores each merged fact:
  ```
  support_i  = p_same_person × reliability(source kind) × (0.95 if quote verified else 0.55)
  confidence = min(1 − Π(1 − support_i), 0.97)      # noisy-OR over independent sources
  ```
- **infer:** reads on the person with a probability, reasoning and a "verify by asking" question. Code calibrates them: probabilities shrink toward 50% when there's little evidence, and are capped by the strongest supporting fact.
- **portrait:** 6–9 themed insights. Each has a claim-making headline, a short synthesis paragraph, sourced supporting points and "why it matters". This replaces long fact lists.
- **converse:** common ground (code-detected literal overlaps plus LLM ones) and 5–7 broad conversation themes with loose angles.
- **advise:** open questions, specific cautions and, if there's a goal, a game plan (incentives, framing, likely pushback, next step). It never invents the user's experience.
- **summarize:** a 2–3 sentence summary plus a few short tags.

## 3. Report

Summary and tags → game plan → portrait → common ground → conversation themes, questions and cautions → reads on them → public footprint and gaps → evidence (all sourced facts, collapsed) → sources with same-person %.

## 4. Web app

- **Home:** the research form is a blank index card. Below it are "Your people", the research started from this browser.
- **While researching: live view.** Index-card divider tabs show the six phases (Identify, Search, Read, Verify, Interpret, Plan), each with its own live count. Below them:
  - "Who we think this is", which fills in as soon as the identity is resolved
  - "Notes so far": facts are inked onto ruled lines as they're found, with a check mark when the quote was confirmed on the page
  - "Sources found": namesake pages are struck through once ruled out
  - "Portrait taking shape" near the end.
- **Sort:** rich cards (portrait, themes, common ground, reads, questions, cautions, game plan). Drag **left to keep**, **right to set aside**, into two visible piles. Undo is available.
- **Brief:** the kept cards as an expandable cheat sheet. **Report:** the full page, with a Markdown download.
- **Learning:** a Laplace-smoothed keep rate per card kind ranks cards in future runs.
- **Design:** cool grey-blue desk, white card stock with a red header rule and blue ruled lines, ink navy and pine. Familjen Grotesk for the interface, Literata for reading text.

## 5. Code layout

```
researchr/
  config.py     settings, limits, Vercel detection
  llm.py        Groq client: key rotation, token bucket, JSON mode, quota pause, browser_search
  search.py     tiered search + fetch, cache, GitHub API, focus window
  pipeline.py   the resumable state machine + live snapshot
  scoring.py    reliability priors, noisy-OR, calibration, sensitive filters, quote check
  report.py     Markdown/HTML report + cards
  jobs.py       runs, ticks, leases, concurrency slots, limits, swipes
  store.py      JSON store: local files or private Vercel Blob
  profile.py    owner profile (profile/me.json)
api/index.py    FastAPI (Vercel function + local server)
public/         index.html, styles.css, app.js
cli.py          run from the terminal
```

## 6. Next
- A paid Groq tier (or `gpt-oss-120b` for the synthesis steps) to raise quality and daily capacity.
- A calendar hook: research tomorrow's attendees overnight.
- Feed swipe preferences back into query planning.
