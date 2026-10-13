"""Research a person from the terminal.

    python cli.py "Jane Doe" --hints "Stripe, payments infra, SF" --goal "ask about joining her team"
    python cli.py --refresh-me      # re-read my profile PDF into profile/me.json
"""
import argparse
import sys

from researchr import config, jobs
from researchr.profile import load_me
from researchr.schemas import Target


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?")
    ap.add_argument("--hints", default="", help="company, role, city, URLs: anything that disambiguates")
    ap.add_argument("--goal", default="", help="what you want out of the meeting (optional)")
    ap.add_argument("--context", default="", help="how the meeting came about (optional)")
    ap.add_argument("--refresh-me", action="store_true")
    a = ap.parse_args()
    if a.refresh_me:
        me = load_me(refresh=True)
        print("Profile loaded:", me.get("name"), "-", me.get("headline"))
        if not a.name:
            return
    if not a.name:
        ap.error("name is required")
    rid = jobs.create_run(Target(name=a.name, hints=a.hints, goal=a.goal, context=a.context, use_owner_profile=True))
    last = [None]

    # print progress as the run advances
    import threading
    import time

    def watch():
        while True:
            m = jobs.get_meta(rid) or {}
            if m.get("message") != last[0]:
                last[0] = m.get("message")
                print(f"[{m.get('pct', 0):5.1f}%] {m.get('message')}", flush=True)
            if m.get("status") in ("done", "error"):
                return
            time.sleep(1)

    w = threading.Thread(target=watch, daemon=True)
    w.start()
    status = jobs.run_to_completion(rid)
    w.join(timeout=3)
    if status == "error":
        print((jobs.get_meta(rid) or {}).get("error"))
        sys.exit(1)
    r = jobs.report(rid)
    out = config.REPORTS_DIR / f"{rid}.md"
    out.write_text(r["md"], encoding="utf-8")
    out.with_suffix(".html").write_text(r["html"], encoding="utf-8")
    print(f"\nReport: {out}\n        {out.with_suffix('.html')}")


if __name__ == "__main__":
    main()
