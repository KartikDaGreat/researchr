# researchr

**Know who you're meeting.** Type a name, a detail that pins the person down, and optionally what you want from the meeting. researchr searches the public web, keeps only what's actually about that person, and turns it into a short brief you sort on your phone like a deck of index cards.

**Try it:** https://researchr.kartikgounder.com

## What you get

- **A live view while it works.** You watch it go from working out *which* person you mean (and how sure it is) to searching, reading pages, verifying facts and interpreting them. Notes appear as they're found, each with its source and a check when the quote was confirmed on the page. Pages about a different person with the same name are visibly crossed out.
- **A portrait, not a fact dump.** 6–9 themed insights, each with a headline, a short synthesis, sourced supporting points and why it matters for your meeting.
- **Reads on the person.** Reasoned guesses with calibrated probabilities and a question you can ask to check each one.
- **Common ground and things to talk about.** Overlaps with you (if you add a few lines about yourself) and broad conversation themes with angles to explore. Plus questions to ask, things to handle carefully, and a game plan if you set a goal.
- **Sort, then brief.** Drag a card left to keep it or right to set it aside. Your kept cards become a one-screen brief. The full report is one tap away and downloads as Markdown.

Every fact carries a confidence score, computed in code from how many independent sources back it, how likely each source is about the right person, and whether the quoted evidence really appears on the page.

## How it works

A resumable agent pipeline on Groq. It uses `openai/gpt-oss-120b` first and steps down to `gpt-oss-20b`, then `qwen3.8-27b`, when a model's free quota runs out, so runs don't stall:

```
identity → plan → search → triage → fetch → extract → snippets → merge → infer → portrait → converse → advise → summarize
```

- **Identity first:** seed searches build an "anchor" for the person, so namesakes get filtered out.
- **Search:** Groq's built-in web search, with [Firecrawl](https://firecrawl.dev) as a fallback for searches that come back empty and for pages a plain fetch can't read. GitHub profiles come from its public API.
- **Facts:** extracted per page with verbatim quotes, checked against the page, merged, then scored with a noisy-OR over sources.
- **Synthesis:** reads (shrunk toward 50% when the evidence is thin), the portrait, conversation themes and the game plan.
- **Serverless:** each step is small and its state is plain JSON. On Vercel, each function call works up to 200s, saves to a private Blob store, and hands off to the next. If the free Groq quota runs out, the run pauses and resumes on its own.

More detail in [PLAN.md](PLAN.md).

## Responsible use

Public, professional information only. It never collects home addresses, phone numbers or family details, and never infers health, religion, sexuality, ethnicity, politics or finances. Those are filtered in the prompts *and* in code. It's for preparing for a conversation, not for looking up private individuals.

## Run it yourself

```bash
pip install -r requirements.txt
```

Create a `.env`:
```
GROQ_API_KEY=...          # https://console.groq.com
GROQ_API_KEY_2=...        # optional second key, doubles throughput
FIRECRAWL_API_KEY=...     # optional; locally a logged-in `firecrawl` CLI also works
```

Optionally copy `profile/me.example.json` to `profile/me.json` and fill it in. That becomes "you" for common ground in owner mode. `python cli.py --refresh-me` builds it from a resume PDF set by `ME_PROFILE_PATH`.

- **Terminal:** `python cli.py "Jane Doe" --hints "Stripe, payments, SF" --goal "ask about joining her team"`
- **Web app:** `python -m uvicorn api.index:app --port 8000`, then open http://localhost:8000. Locally there's no captcha.

## Deploy your own (Vercel)

1. `vercel link`, then create a private Blob store: `vercel blob create-store <name> --access private`. Back up `.env.local` first, because the Vercel CLI rewrites it.
2. Add env vars: `GROQ_API_KEY`, `GROQ_API_KEY_2`, `APP_TOKEN` (your owner key), `RECAPTCHA_SITE_KEY` and `RECAPTCHA_SECRET` (v3), and optionally `FIRECRAWL_API_KEY`.
3. `vercel deploy --prod`.

**Access model:**
- Each browser gets an anonymous session and only ever sees its own research.
- Starting research needs a reCAPTCHA v3 pass and is rate-limited.
- Opening `/?token=<APP_TOKEN>` once turns on owner mode: no captcha or limits, and your own profile is used.

**Limits** (env vars): `RUNS_PER_DAY_TOTAL` (12), `RUNS_PER_USER_PER_DAY` (3), `RUNS_PER_IP_PER_DAY` (4), `MAX_ACTIVE_RUNS` (2).

**Groq quota:** the free tier gives each *account* 8K tokens/min and 200K tokens/day *per model*. It refills continuously, at about 8.3K tokens an hour. Keys on the same account share a quota, so use keys from different accounts. A run uses about 80–110K tokens. With two accounts and three models (`GROQ_MODELS`, tried in order) that's about 1.2M tokens, or roughly 12 runs, a day. Only the gpt-oss models can use Groq's web search, so on qwen, searches go through Firecrawl.

**Tuning:** `MAX_QUERIES` (14), `MAX_PAGES` (12), `MAX_FIRECRAWL_SCRAPES` (3), `USE_FIRECRAWL` (1), `CACHE_DAYS` (7), `STEP_SECONDS` (200), `GROQ_MODELS`, `RESEARCHR_DEBUG=1` to log every model call.

## Project layout

```
researchr/      the agent: pipeline, search, scoring, report, jobs, storage
api/index.py    FastAPI app (Vercel function + local server)
public/         the web app (index.html, styles.css, app.js)
cli.py          run research from the terminal
```
