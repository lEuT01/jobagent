"""jobagent command line.

  jobagent profile   build/refresh the candidate profile from CV + profile.yaml
  jobagent run       fetch -> prescreen -> score new listings -> write report
  jobagent report    re-render the report from the store (no API calls)
  jobagent status    mark a listing applied / ignored / saved
  jobagent stats     counts
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings, load_yaml
from .cv import read_cv
from .llm import LLM
from .profile import CandidateProfile, build_profile
from .report import render
from .store import Store

log = logging.getLogger("jobagent")


def _paths(args, settings: Settings) -> dict[str, Path]:
    d = settings.data_dir
    return {"profile": d / "candidate_profile.json", "db": d / "jobs.sqlite", "report": d / "report.md",
            "config": Path(args.config), "companies": Path(args.companies)}


# ---- commands ---------------------------------------------------------------------------------
def cmd_profile(args, settings: Settings) -> int:
    p = _paths(args, settings)
    cfg = load_yaml(p["config"])
    cv_text = read_cv(cfg["cv"])
    extra = "\n\n".join(read_cv(x) for x in cfg.get("extra_documents", []) if Path(x).exists())
    llm = LLM(settings.anthropic_api_key, settings.profile_model)
    prof = build_profile(llm, cv_text, cfg.get("aspirations", {}), extra)
    p["profile"].parent.mkdir(parents=True, exist_ok=True)
    prof.save(p["profile"])
    print(prof.to_json())
    print(f"\nSaved to {p['profile']}  ({llm.usage_summary()})", file=sys.stderr)
    print("Review target_roles / search_queries / reject_keywords and edit the JSON by hand if needed.", file=sys.stderr)
    return 0


def cmd_run(args, settings: Settings) -> int:
    from .pipeline import run_pipeline
    p = _paths(args, settings)
    if not p["profile"].exists():
        print("No candidate profile yet. Run `jobagent profile` first.", file=sys.stderr)
        return 2
    cfg = load_yaml(p["config"])
    prof = CandidateProfile.load(p["profile"])
    store = Store(p["db"])
    st = run_pipeline(cfg=cfg, settings=settings, profile=prof, store=store, companies_path=p["companies"],
                      score=not args.no_score, limit=args.limit, log_fn=log.info)
    for e in st.errors:
        log.error(e)
    if args.verbose:
        for t, c, why in st.rejects[: args.show_rejects]:
            log.debug("reject [%s] %s @ %s", why, t, c)
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    rows = store.top(min_fit=args.min_fit, limit=args.top, since=None if args.all else since)
    md = render(rows, title="Job shortlist" + ("" if args.all else " (new this run)"), min_fit=args.min_fit,
                screen_stats={"fetched": st.fetched, "sources": len(st.per_source), "new": st.new,
                              "kept": st.kept, "scored": st.scored, "usage": st.usage})
    p["report"].write_text(md, encoding="utf-8")
    print(md)
    print(f"\nReport written to {p['report']}", file=sys.stderr)
    return 0


def cmd_report(args, settings: Settings) -> int:
    p = _paths(args, settings)
    store = Store(p["db"])
    rows = store.top(min_fit=args.min_fit, limit=args.top)
    md = render(rows, title="Job shortlist (all time)", min_fit=args.min_fit)
    p["report"].write_text(md, encoding="utf-8")
    print(md)
    return 0


def cmd_status(args, settings: Settings) -> int:
    p = _paths(args, settings)
    store = Store(p["db"])
    uid = store.find_uid(args.id)
    if not uid:
        print(f"No unique job matching id fragment {args.id!r}", file=sys.stderr)
        return 1
    store.set_status(uid, args.status, args.note or "")
    print(f"{uid} -> {args.status}")
    return 0


def cmd_probe(args, settings: Settings) -> int:
    """Hit every career-page endpoint once and report which slugs work. No LLM calls."""
    import httpx

    cos = load_yaml(Path(args.companies)).get("companies", [])
    bad = 0
    with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "jobagent/0.1"}) as c:
        for co in cos:
            try:
                if co["ats"] == "greenhouse":
                    r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{co['slug']}/jobs")
                    n = len(r.json().get("jobs", [])) if r.status_code == 200 else None
                elif co["ats"] == "lever":
                    r = c.get(f"https://api.lever.co/v0/postings/{co['slug']}", params={"mode": "json"})
                    n = len(r.json()) if r.status_code == 200 else None
                else:
                    r = c.post(f"https://{co['host']}/wday/cxs/{co['tenant']}/{co['site']}/jobs",
                               json={"appliedFacets": {}, "limit": 5, "offset": 0, "searchText": "quant"},
                               headers={"Accept": "application/json", "Content-Type": "application/json"})
                    n = r.json().get("total") if r.status_code == 200 else None
                ok = r.status_code == 200
            except Exception as e:  # noqa: BLE001
                ok, n, r = False, None, None
                err = f"{type(e).__name__}"
            bad += not ok
            print(f"{'OK ' if ok else 'BAD'} {co['name']:32} {co['ats']:10} "
                  f"{(r.status_code if r is not None else err)!s:>4} {'' if n is None else f'{n} listings'}")
    print(f"\n{len(cos) - bad}/{len(cos)} endpoints working. Fix or delete the BAD ones in {args.companies}.")
    return 0


def cmd_serve(args, settings: Settings) -> int:
    from .server import serve
    print(f"jobagent web app: http://127.0.0.1:{args.port}", file=sys.stderr)
    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_stats(args, settings: Settings) -> int:
    p = _paths(args, settings)
    print(Store(p["db"]).counts())
    return 0


# ---- entry ------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jobagent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="profile.yaml")
    ap.add_argument("--companies", default="companies.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("profile", help="build candidate profile from CV + aspirations")

    r = sub.add_parser("run", help="fetch, screen, score, report")
    r.add_argument("--min-fit", type=int, default=40)
    r.add_argument("--top", type=int, default=40)
    r.add_argument("--limit", type=int, default=0, help="score at most N listings (cost cap)")
    r.add_argument("--no-score", action="store_true", help="fetch and prescreen only")
    r.add_argument("--all", action="store_true", help="report everything scored, not just today's")
    r.add_argument("--show-rejects", type=int, default=50, help="log up to N prescreen rejects (with -v)")

    rp = sub.add_parser("report", help="re-render report from stored scores")
    rp.add_argument("--min-fit", type=int, default=40)
    rp.add_argument("--top", type=int, default=60)

    st = sub.add_parser("status", help="mark a listing")
    st.add_argument("id", help="uid fragment shown in the report")
    st.add_argument("status", choices=["applied", "ignored", "saved"])
    st.add_argument("--note")

    sub.add_parser("stats")
    sub.add_parser("probe", help="check which career-page endpoints in companies.yaml work")
    sv = sub.add_parser("serve", help="start the local web app")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--no-browser", action="store_true")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    settings = Settings.from_env()
    return {"profile": cmd_profile, "run": cmd_run, "report": cmd_report, "status": cmd_status, "stats": cmd_stats, "probe": cmd_probe, "serve": cmd_serve}[args.cmd](args, settings)


if __name__ == "__main__":
    sys.exit(main())
