"""Settings loaded from .env.local / environment."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ON_VERCEL = bool(os.getenv("VERCEL"))
# Vercel functions only have a writable /tmp
DATA_DIR = Path("/tmp/researchr") if ON_VERCEL else ROOT / "data"
REPORTS_DIR = DATA_DIR / "reports"
DATA_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file(ROOT / ".env.local")
_load_env_file(ROOT / ".env")

GROQ_KEYS = [k for k in (os.getenv("GROQ_API_KEY"), os.getenv("GROQ_API_KEY_2")) if k]
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
# Free tier for gpt-oss-20b is 8K tokens/min per key; keep a safety margin.
GROQ_TPM = int(os.getenv("GROQ_TPM", "8000"))
GROQ_TPM_SAFETY = float(os.getenv("GROQ_TPM_SAFETY", "0.85"))

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")  # optional locally; the `firecrawl` CLI login is used instead
USE_FIRECRAWL = os.getenv("USE_FIRECRAWL", "1") == "1"

MAX_QUERIES = int(os.getenv("MAX_QUERIES", "14"))
RESULTS_PER_QUERY = int(os.getenv("RESULTS_PER_QUERY", "6"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "12"))
MAX_FIRECRAWL_SCRAPES = int(os.getenv("MAX_FIRECRAWL_SCRAPES", "3"))  # fallback only
CACHE_DAYS = float(os.getenv("CACHE_DAYS", "7"))
PAGE_CHAR_BUDGET = int(os.getenv("PAGE_CHAR_BUDGET", "9000"))

# Each Vercel invocation works for at most this long, saves state, then chains to the next.
STEP_SECONDS = int(os.getenv("STEP_SECONDS", "200"))

# Public mode: anyone can use the site; these protect the shared (free-tier) Groq + Firecrawl budget.
RUNS_PER_USER_PER_DAY = int(os.getenv("RUNS_PER_USER_PER_DAY", "2"))
RUNS_PER_IP_PER_DAY = int(os.getenv("RUNS_PER_IP_PER_DAY", "3"))
# a run uses ~80-110K Groq tokens; the free tier allows 200K/day per key, so 2 keys ~ 4 runs/day
RUNS_PER_DAY_TOTAL = int(os.getenv("RUNS_PER_DAY_TOTAL", "4"))
MAX_ACTIVE_RUNS = int(os.getenv("MAX_ACTIVE_RUNS", "2"))
RECAPTCHA_SITE_KEY = os.getenv("RECAPTCHA_SITE_KEY", "")
RECAPTCHA_SECRET = os.getenv("RECAPTCHA_SECRET", "")
RECAPTCHA_MIN_SCORE = float(os.getenv("RECAPTCHA_MIN_SCORE", "0.5"))

ME_PROFILE_PATH = Path(os.getenv("ME_PROFILE_PATH", str(ROOT / "Profile (5).pdf")))
ME_JSON = ROOT / "profile" / "me.json"  # committed + deployed with the app

APP_TOKEN = os.getenv("APP_TOKEN") or ""
if not APP_TOKEN and not ON_VERCEL:
    tok_file = DATA_DIR / ".app_token"
    if tok_file.exists():
        APP_TOKEN = tok_file.read_text().strip()
    else:
        APP_TOKEN = secrets.token_urlsafe(12)
        tok_file.write_text(APP_TOKEN)
