"""Local web app: `jobagent serve` then open http://127.0.0.1:8765

Single-user, binds to localhost only. All state lives in data/.
"""

import json
import logging
import uuid
import threading
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .config import Settings, load_yaml
from .cv import read_cv
from .llm import LLM
from . import mail as M
from .pipeline import run_pipeline
from .profile import CandidateProfile, build_profile
from .scorer import score_jobs
from .sources.base import Job
from .store import Store

log = logging.getLogger("jobagent.web")
WEB_DIR = Path(__file__).parent / "web"


class RunState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.log: list[str] = []
        self.progress = (0, 0)
        self.stats: dict | None = None
        self.started: float = 0

    def add(self, msg: str):
        with self.lock:
            self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "log": self.log[-200:], "progress": self.progress,
                    "stats": self.stats, "elapsed": round(time.time() - self.started, 1) if self.started else 0}


def _normalise_aspirations(a: dict) -> dict:
    """Map the profile.yaml keys onto the form fields."""
    out = dict(a)
    if "constraints" in out and "avoid" not in out:
        c = out.pop("constraints")
        out["avoid"] = [x for x in c if isinstance(x, str) and x.lower().startswith("no ")]
        out["seniority"] = next((x for x in c if "senior" in x.lower()), "")
        out["notes"] = (out.get("notes", "") + " " + " ".join(x for x in c if x not in out["avoid"] and x != out["seniority"])).strip()
    return out


def create_app(config_path: Path = Path("profile.yaml"), companies_path: Path = Path("companies.yaml")) -> FastAPI:
    app = FastAPI(title="jobagent")
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    paths = {"profile": settings.data_dir / "candidate_profile.json", "db": settings.data_dir / "jobs.sqlite",
             "aspirations": settings.data_dir / "aspirations.txt", "cv": settings.data_dir / "cv.txt"}
    state = RunState()

    def store() -> Store:
        return Store(paths["db"])

    def cfg() -> dict:
        return load_yaml(config_path) if config_path.exists() else {}

    # ---- static ----
    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    # ---- settings ----
    class SettingsIn(BaseModel):
        anthropic_api_key: str | None = None
        adzuna_app_id: str | None = None
        adzuna_app_key: str | None = None
        reed_api_key: str | None = None
        profile_model: str | None = None
        score_model: str | None = None

    @app.get("/api/settings")
    def get_settings():
        c = cfg()
        return {"settings": settings.public(), "sources": c.get("sources", {}),
                "companies": (load_yaml(companies_path).get("companies", []) if companies_path.exists() else []),
                "has_profile": paths["profile"].exists()}

    @app.post("/api/settings")
    def post_settings(s: SettingsIn):
        settings.save({k: v for k, v in s.model_dump().items() if v is not None})
        return settings.public()

    class SourcesIn(BaseModel):
        sources: dict

    @app.post("/api/sources")
    def post_sources(s: SourcesIn):
        c = cfg()
        c["sources"] = {k: bool(v) for k, v in s.sources.items()}
        config_path.write_text(yaml.safe_dump(c, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return c["sources"]

    class CompaniesIn(BaseModel):
        companies: list[dict]

    @app.post("/api/companies")
    def post_companies(c: CompaniesIn):
        companies_path.write_text(yaml.safe_dump({"companies": c.companies}, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return {"count": len(c.companies)}

    # ---- profile ----
    @app.get("/api/profile")
    def get_profile():
        out = {"profile": None, "aspirations": {}, "cv_text": ""}
        if paths["profile"].exists():
            out["profile"] = json.loads(paths["profile"].read_text(encoding="utf-8"))
            out["profile"].pop("cv_text", None)
        raw = paths["aspirations"].read_text(encoding="utf-8") if paths["aspirations"].exists() else ""
        if raw.strip():
            try:
                out["aspirations"] = json.loads(raw)
            except json.JSONDecodeError:
                try:
                    out["aspirations"] = yaml.safe_load(raw) or {}
                except yaml.YAMLError:
                    out["aspirations"] = {"notes": raw}
        elif config_path.exists():
            out["aspirations"] = _normalise_aspirations(cfg().get("aspirations", {}))
        if not isinstance(out["aspirations"], dict):
            out["aspirations"] = {"notes": str(out["aspirations"])}
        if paths["cv"].exists():
            out["cv_text"] = paths["cv"].read_text(encoding="utf-8")
        return out

    @app.post("/api/profile/build")
    async def build(cv: UploadFile | None = File(None), cv_text: str = Form(""), aspirations: str = Form("")):
        try:
            asp = json.loads(aspirations) if aspirations.strip() else {}
        except json.JSONDecodeError:
            try:
                asp = yaml.safe_load(aspirations) or {}
            except yaml.YAMLError:
                asp = {"notes": aspirations}
        if not isinstance(asp, dict):
            asp = {"notes": str(asp)}
        paths["aspirations"].write_text(json.dumps(asp, indent=2), encoding="utf-8")
        if not settings.anthropic_api_key:
            raise HTTPException(400, "Add your Anthropic API key in Settings first.")
        text = cv_text
        if cv is not None and cv.filename:
            tmp = settings.data_dir / f"upload_{cv.filename}"
            tmp.write_bytes(await cv.read())
            text = read_cv(tmp)
            tmp.unlink(missing_ok=True)
        if not text.strip():
            raise HTTPException(400, "Upload a CV or paste its text.")
        paths["cv"].write_text(text, encoding="utf-8")
        llm = LLM(settings.anthropic_api_key, settings.profile_model)
        try:
            prof = build_profile(llm, text, asp)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Claude call failed: {e}") from e
        prof.save(paths["profile"])
        d = json.loads(prof.to_json())
        d.pop("cv_text", None)
        return {"profile": d, "usage": llm.usage_summary()}

    @app.put("/api/profile")
    def put_profile(p: dict):
        if not paths["profile"].exists():
            raise HTTPException(404, "No profile yet")
        cur = json.loads(paths["profile"].read_text(encoding="utf-8"))
        for k, v in p.items():
            if k in cur and k != "cv_text":
                cur[k] = v
        CandidateProfile.from_json(json.dumps(cur)).save(paths["profile"])
        return {"ok": True}

    # ---- run ----
    class RunIn(BaseModel):
        score: bool = True
        limit: int = 0

    @app.post("/api/run")
    def start_run(r: RunIn):
        if state.running:
            raise HTTPException(409, "A run is already in progress")
        if not paths["profile"].exists():
            raise HTTPException(400, "Build your profile first")
        prof = CandidateProfile.load(paths["profile"])

        def work():
            state.running, state.log, state.stats, state.progress, state.started = True, [], None, (0, 0), time.time()
            try:
                st = run_pipeline(cfg=cfg(), settings=settings, profile=prof, store=store(), companies_path=companies_path,
                                  score=r.score, limit=r.limit, log_fn=state.add,
                                  progress=lambda d, n: setattr(state, "progress", (d, n)))
                state.stats = {"fetched": st.fetched, "kept": st.kept, "new": st.new, "scored": st.scored,
                               "per_source": st.per_source, "rejects": st.rejects[:300], "usage": st.usage, "errors": st.errors}
                state.add("Done.")
            except Exception as e:  # noqa: BLE001
                log.exception("run failed")
                state.add(f"Run failed: {e}")
                state.stats = {"errors": [str(e)]}
            finally:
                state.running = False

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    @app.get("/api/run/status")
    def run_status():
        return state.snapshot()

    # ---- jobs ----
    @app.get("/api/jobs")
    def jobs(min_fit: int = 0, limit: int = 200, include_done: bool = False):
        st = store()
        rows = st.top(min_fit=min_fit, limit=limit, exclude_status=not include_done)
        for r in rows:
            r["description"] = (r.get("description") or "")[:4000]
            r["replies"] = st.replies_for(r["uid"]) if r.get("status") == "applied" else []
        return {"jobs": rows, "counts": st.counts()}

    class StatusIn(BaseModel):
        status: str
        note: str = ""

    @app.post("/api/jobs/{uid:path}/status")
    def set_status(uid: str, s: StatusIn):
        if s.status not in ("applied", "ignored", "saved", "none"):
            raise HTTPException(400, "bad status")
        st = store()
        if s.status == "none":
            st.conn.execute("DELETE FROM status WHERE uid=?", (uid,))
            st.conn.commit()
        else:
            st.set_status(uid, s.status, s.note)
        return {"ok": True}

    # ---- score a pasted listing ----
    class PasteIn(BaseModel):
        title: str
        company: str = ""
        location: str = ""
        url: str = ""
        description: str

    @app.post("/api/score")
    def score_paste(p: PasteIn):
        if not settings.anthropic_api_key:
            raise HTTPException(400, "Add your Anthropic API key in Settings first.")
        if not paths["profile"].exists():
            raise HTTPException(400, "Build your profile first")
        prof = CandidateProfile.load(paths["profile"])
        job = Job(source="manual", title=p.title, company=p.company, location=p.location,
                  url=p.url or "", description=p.description, external_id=uuid.uuid4().hex[:10])
        llm = LLM(settings.anthropic_api_key, settings.score_model)
        res = score_jobs(llm, prof, [job])
        if not res:
            raise HTTPException(502, "Scoring failed")
        r = res[0]
        r.pop("job")
        st = store()
        st.upsert_jobs([job])
        st.save_score(job.uid, int(r["fit"]), r["verdict"], r)
        return {"uid": job.uid, "result": r, "usage": llm.usage_summary()}

    # ---- mailbox sync ----
    mail_state = {"running": False, "last": None, "last_result": None, "error": None}
    mail_lock = threading.Lock()

    def mail_configured() -> bool:
        return bool(settings.mail_user and settings.mail_password and settings.mail_imap_host)

    def mail_sync(force_days: int | None = None) -> dict:
        """One pass: fetch new mail, pick candidates, classify with Claude, store, notify."""
        if not mail_configured():
            return {"error": "Mailbox not configured"}
        if not settings.anthropic_api_key:
            return {"error": "Anthropic key missing"}
        with mail_lock:
            mail_state["running"] = True
            try:
                st = store()
                applied = st.applied_jobs()
                last = st.meta_get("mail_last_sync")
                since = M.default_since(force_days or int(settings.mail_lookback_days))
                if last and not force_days:
                    # overlap by a day so late-arriving mail isn't missed; mail_seen dedupes
                    from datetime import datetime as _dt, timedelta as _td
                    since = max(since, _dt.fromisoformat(last) - _td(days=1))
                box = M.Mailbox(settings.mail_imap_host, settings.mail_user, settings.mail_password, settings.mail_folder)
                msgs = box.fetch_since(since)
                already = st.mail_already_checked([m.msg_id for m in msgs])
                fresh = [m for m in msgs if m.msg_id not in already]
                cands = [m for m in fresh if M.candidate_matches(m, applied) or M.looks_recruiting(m)]
                found = 0
                if cands:
                    llm = LLM(settings.anthropic_api_key, settings.score_model)
                    for r in M.classify(llm, cands, applied):
                        m = r.pop("msg")
                        if not r["is_about_application"]:
                            continue
                        uid = r.get("job_uid") or ""
                        if not uid or not any(a["uid"] == uid for a in applied):
                            hits = M.candidate_matches(m, applied)
                            uid = hits[0]["uid"] if len(hits) == 1 else ""
                        st.save_reply({"msg_id": m.msg_id, "job_uid": uid, "from_name": m.from_name, "from_addr": m.from_addr,
                                       "subject": m.subject, "date": m.date, "category": r["category"], "summary": r["summary"],
                                       "action_needed": r["action_needed"], "deadline": r.get("deadline", "")})
                        found += 1
                st.mark_mail_checked([m.msg_id for m in fresh])
                st.meta_set("mail_last_sync", time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
                notified = 0
                pending = st.unnotified_replies()
                if pending and settings.mail_notify_email and settings.mail_notify_to:
                    body = "\n\n".join(f"[{r['category'].upper()}] {r['from_name']} <{r['from_addr']}>\n{r['subject']}\n{r['summary']}"
                                       + (f"\nDeadline: {r['deadline']}" if r.get("deadline") else "") for r in pending)
                    try:
                        M.send_self_notification(smtp_host=settings.mail_smtp_host, smtp_port=587, user=settings.mail_user,
                                                 password=settings.mail_password, to=settings.mail_notify_to,
                                                 subject=f"jobagent: {len(pending)} new repl{'y' if len(pending) == 1 else 'ies'}", body=body)
                        st.mark_replies([r["msg_id"] for r in pending], notified=True)
                        notified = len(pending)
                    except Exception as e:  # noqa: BLE001
                        log.warning("notify email failed: %s", e)
                res = {"checked": len(fresh), "candidates": len(cands), "replies_found": found, "notified": notified,
                       "applied_jobs": len(applied), "at": time.strftime("%H:%M:%S")}
                mail_state.update(last=res["at"], last_result=res, error=None)
                return res
            except Exception as e:  # noqa: BLE001
                log.exception("mail sync failed")
                mail_state["error"] = str(e)
                return {"error": str(e)}
            finally:
                mail_state["running"] = False

    def mail_loop():
        while True:
            mins = max(2, int(settings.mail_poll_minutes or 10))
            time.sleep(mins * 60)
            if mail_configured() and settings.anthropic_api_key:
                mail_sync()

    threading.Thread(target=mail_loop, daemon=True).start()

    class MailSettingsIn(BaseModel):
        mail_imap_host: str | None = None
        mail_smtp_host: str | None = None
        mail_user: str | None = None
        mail_password: str | None = None
        mail_folder: str | None = None
        mail_notify_to: str | None = None
        mail_notify_email: bool | None = None
        mail_lookback_days: int | None = None
        mail_poll_minutes: int | None = None

    @app.post("/api/mail/settings")
    def mail_settings(m: MailSettingsIn):
        settings.save({k: v for k, v in m.model_dump().items() if v is not None})
        return settings.public()

    @app.post("/api/mail/test")
    def mail_test():
        if not mail_configured():
            raise HTTPException(400, "Enter mailbox address and app password first")
        try:
            return {"ok": True, "message": M.Mailbox(settings.mail_imap_host, settings.mail_user, settings.mail_password, settings.mail_folder).test()}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Could not connect: {e}") from e

    class SyncIn(BaseModel):
        days: int | None = None

    @app.post("/api/mail/sync")
    def mail_sync_now(s: SyncIn):
        if mail_state["running"]:
            raise HTTPException(409, "Sync already running")
        res = mail_sync(force_days=s.days)
        if "error" in res:
            raise HTTPException(400, res["error"])
        return res

    @app.get("/api/mail/status")
    def mail_status():
        return {**mail_state, "configured": mail_configured(), "poll_minutes": settings.mail_poll_minutes,
                "counts": store().counts()}

    @app.get("/api/replies")
    def replies():
        return {"replies": store().replies()}

    class SeenIn(BaseModel):
        msg_ids: list[str]
        seen: bool = True

    @app.post("/api/replies/seen")
    def replies_seen(s: SeenIn):
        store().mark_replies(s.msg_ids, seen=s.seen)
        return {"ok": True}

    class LinkIn(BaseModel):
        msg_id: str
        job_uid: str

    @app.post("/api/replies/link")
    def replies_link(l: LinkIn):
        store().link_reply(l.msg_id, l.job_uid)
        return {"ok": True}

    @app.get("/api/jobs/applied")
    def applied_jobs():
        return {"jobs": store().applied_jobs()}

    # ---- self-update ----
    ROOT = Path(__file__).resolve().parent.parent

    def _git(*args) -> tuple[int, str]:
        import subprocess
        try:
            r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60)
            return r.returncode, (r.stdout + r.stderr).strip()
        except Exception as e:  # noqa: BLE001
            return 1, str(e)

    @app.get("/api/version")
    def version():
        if not (ROOT / ".git").exists():
            return {"git": False, "version": "not a git checkout"}
        _, local = _git("rev-parse", "--short", "HEAD")
        _, when = _git("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M")
        _, msg = _git("log", "-1", "--format=%s")
        return {"git": True, "version": local, "date": when, "message": msg}

    @app.post("/api/update")
    def update():
        if not (ROOT / ".git").exists():
            raise HTTPException(400, "Not a git checkout; re-download instead")
        _, before = _git("rev-parse", "--short", "HEAD")
        code, out = _git("pull", "--ff-only")
        if code != 0:
            raise HTTPException(502, f"git pull failed: {out}")
        _, after = _git("rev-parse", "--short", "HEAD")
        changed = before != after
        if changed:
            import subprocess, sys as _sys
            subprocess.run([_sys.executable, "-m", "pip", "install", "-q", "-e", str(ROOT)], capture_output=True)
        return {"updated": changed, "from": before, "to": after, "output": out,
                "restart_needed": changed}

    # ---- probe career pages ----
    @app.get("/api/probe")
    def probe():
        import httpx
        cos = load_yaml(companies_path).get("companies", []) if companies_path.exists() else []
        out = []
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
                    out.append({"name": co["name"], "ats": co["ats"], "ok": r.status_code == 200, "code": r.status_code, "listings": n})
                except Exception as e:  # noqa: BLE001
                    out.append({"name": co["name"], "ats": co["ats"], "ok": False, "code": type(e).__name__, "listings": None})
        return {"results": out}

    @app.exception_handler(Exception)
    async def _err(request, exc):
        log.exception("unhandled")
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True):
    import uvicorn
    app = create_app()
    if open_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
