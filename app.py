#!/usr/bin/env python3
"""
app.py — FastAPI backend for the Jobs AI web frontend
───────────────────────────────────────────────────────
Serves frontend/index.html and exposes REST + SSE endpoints
that drive the 7-phase pipeline.

Launch:
  python app.py
  or: uvicorn app:app --reload --port 8000
"""

import io
import json
import os
import queue
import secrets
import sys
import threading
import time
import uuid
import contextvars
from datetime import datetime
from pathlib import Path
from collections.abc import MutableMapping

# Force UTF-8 stdout/stderr so Rich emoji don't crash on Windows cp1252
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse

from pipeline.config import OUTPUT_DIR, RESOURCES_DIR

# Auth helpers — imported lazily so missing bcrypt doesn't crash the server
try:
    from auth_utils import (
        get_google_auth_url,
        hash_password,
        verify_google_token,
        verify_password,
    )
except ImportError:
    def hash_password(_pw):
        raise RuntimeError("bcrypt is required for password auth")
    def verify_password(_pw, _h):
        raise RuntimeError("bcrypt is required for password auth")
    def get_google_auth_url(_redirect_uri):
        raise RuntimeError("Google OAuth dependencies are not installed")
    def verify_google_token(_code, _redirect_uri, _state):
        raise RuntimeError("Google OAuth dependencies are not installed")

from session_store import SQLiteSessionStore
from pipeline.phases import (
    phase1_ingest_resume,
    phase2_discover_jobs,
    phase3_score_jobs,
    phase4_tailor_resume,
    phase5_simulate_submission,
    _load_existing_applications,
    phase6_update_tracker,
    phase7_run_report,
)
from pipeline.resume import _build_demo_resume, _read_resume, _save_tailored_resume

app = FastAPI(title="Jobs AI")
_AUTH_COOKIE = "jobs_ai_auth"
_STATE_COOKIE = "jobs_ai_session"
_AUTH_SESSIONS: dict[str, dict] = {}

# ── Serve frontend ─────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse("frontend/landing.html")

@app.get("/app")
def dashboard():
    return FileResponse("frontend/index.html")

@app.get("/frontend/{filename}")
def frontend_static(filename: str):
    p = Path("frontend") / filename
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(p)

@app.get("/output/{path:path}")
def serve_output_file(path: str):
    root = OUTPUT_DIR.resolve()
    p = (root / path).resolve()
    if root != p and root not in p.parents:
        raise HTTPException(403, "File access denied")
    session_root = (root / "sessions").resolve()
    if p == session_root or session_root in p.parents:
        current_root = _session_output_dir().resolve()
        if p != current_root and current_root not in p.parents:
            raise HTTPException(403, "File access denied")
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(p)

def _default_state() -> dict:
    return {
    # LLM backend
    "mode": "demo",
    "api_key": "",
    "ollama_model": "llama3.2",
    # Search / apply settings
    "threshold": 75,
    "job_titles": "Engineer",
    "location": "United States",
    "max_apps": 10,
    "max_scrape_jobs": 20,
    "days_old": 30,
    "cover_letter": False,
    "blacklist": "",
    "whitelist": "NVIDIA, Apple, Microsoft, Intel, IBM, Micron, Samsung, TSMC",
    # Filters
    "experience_levels": ["internship", "entry-level"],
    "education_filter": ["bachelors"],
    "include_unknown_education": True,
    "citizenship_filter": "exclude_required",
    "use_simplify": True,
    "llm_score_limit": 10,
    # Resume
    "resume_text": None,
    "latex_source": None,
    "resume_filename": None,
    # Phase results
    "profile": None,
    "jobs": None,
    "scored": None,
    "tailored_map": {},
    "applications": None,
    "tracker_path": None,
    "report": None,
    # Pipeline state
    "done": set(),
    "error": {},
    "elapsed": {},
    # Auth
    "user": None,
    # UI state
    "liked_ids": set(),
    "hidden_ids": set(),
    "dev_tweaks": {},
    "feedback": [],
}

class _SessionStateProxy(MutableMapping):
    def __init__(self):
        self._fallback = _default_state()
        self._state_var = contextvars.ContextVar("jobs_ai_state", default=None)
        self._session_var = contextvars.ContextVar("jobs_ai_session_id", default=None)

    def bind(self, state: dict, session_id: str = None):
        self._state_var.set(state)
        self._session_var.set(session_id)

    def current(self) -> dict:
        return self._state_var.get() or self._fallback

    def session_id(self) -> str | None:
        return self._session_var.get()

    def __getitem__(self, key):
        return self.current()[key]

    def __setitem__(self, key, value):
        self.current()[key] = value

    def __delitem__(self, key):
        del self.current()[key]

    def __iter__(self):
        return iter(self.current())

    def __len__(self):
        return len(self.current())

_S = _SessionStateProxy()

_store_db = OUTPUT_DIR / "jobs_ai_sessions.sqlite3"
_session_store = SQLiteSessionStore(_store_db, default_state_factory=_default_state)
_user_store = _session_store

def _bind_request_state(request: Request) -> tuple[str, dict, bool]:
    session_id = request.cookies.get(_STATE_COOKIE) or uuid.uuid4().hex
    loaded = _session_store.get_state(session_id)
    state = _default_state()
    state.update(loaded or {})
    state["done"] = set(state.get("done") or [])
    state["liked_ids"] = set(state.get("liked_ids") or [])
    state["hidden_ids"] = set(state.get("hidden_ids") or [])
    _S.bind(state, session_id)
    return session_id, state, not bool(request.cookies.get(_STATE_COOKIE))

def _save_bound_state(state: dict = None, session_id: str = None) -> None:
    sid = session_id or _S.session_id()
    if not sid:
        return
    _session_store.save_state(sid, state or _S.current())

def _session_output_dir(session_id: str = None) -> Path:
    sid = session_id or _S.session_id() or "local"
    safe_sid = "".join(ch for ch in sid if ch.isalnum() or ch in ("-", "_")) or "local"
    out = OUTPUT_DIR / "sessions" / safe_sid
    out.mkdir(parents=True, exist_ok=True)
    return out

def _output_url(path: Path | str) -> str:
    p = Path(path)
    try:
        rel = p.resolve().relative_to(OUTPUT_DIR.resolve()).as_posix()
    except Exception:
        rel = p.name
    return f"/output/{rel}"

@app.middleware("http")
async def session_state_middleware(request: Request, call_next):
    session_id, state, is_new = _bind_request_state(request)
    response = await call_next(request)
    if is_new:
        response.set_cookie(_STATE_COOKIE, session_id, httponly=True, samesite="lax")
    if not response.headers.get("content-type", "").startswith("text/event-stream"):
        _save_bound_state(state, session_id)
    return response

# ── Provider factory ──────────────────────────────────────────────────────────

def _make_provider():
    from pipeline.providers import DemoProvider, AnthropicProvider, OllamaProvider
    mode = _S.get("mode", "demo")
    if mode == "demo":
        return DemoProvider()
    if mode == "ollama":
        return OllamaProvider(model=_S.get("ollama_model", "llama3.2"))
    key = _S.get("api_key", "")
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    return AnthropicProvider()

# ── SSE helper ────────────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"

_phase_logs: dict = {}

class _LogCapture:
    """Wraps a file object to both write normally and capture to queue."""
    def __init__(self, original, queue):
        self.original = original
        self.queue = queue

    def write(self, text):
        if text and text.strip():
            # Send to queue for streaming
            try:
                self.queue.put_nowait(text)
            except:
                pass
        # Also write normally so console still works
        return self.original.write(text)

    def flush(self):
        return self.original.flush()

    def isatty(self):
        return self.original.isatty()

    def __getattr__(self, name):
        return getattr(self.original, name)

def _run_phase_sse(phase: int, fn):
    """Run *fn* in a thread and stream SSE for this bound user session."""
    state = _S.current()
    session_id = _S.session_id()

    def _events():
        _S.bind(state, session_id)
        result_q: "queue.Queue[tuple]" = queue.Queue()
        log_q: "queue.Queue[str]" = queue.Queue()
        _phase_logs[(session_id, phase)] = log_q

        def _worker():
            import sys
            _S.bind(state, session_id)
            old_stdout = sys.stdout
            try:
                sys.stdout = _LogCapture(old_stdout, log_q)
                val = fn()
                result_q.put(("ok", val))
            except Exception as exc:
                result_q.put(("err", str(exc)))
            finally:
                sys.stdout = old_stdout

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t0 = time.time()

        yield _sse({"type": "start", "phase": phase})

        while t.is_alive():
            try:
                while True:
                    text = log_q.get_nowait()
                    yield _sse({"type": "log", "phase": phase, "text": text})
            except queue.Empty:
                pass

            yield ": keep-alive\n\n"
            t.join(timeout=0.2)

        try:
            status, val = result_q.get_nowait()
        except queue.Empty:
            state["error"][phase] = "phase timed out"
            _save_bound_state(state, session_id)
            yield _sse({"type": "error", "phase": phase, "message": "phase timed out"})
            return

        elapsed = round(time.time() - t0, 1)

        if status == "err":
            state["error"][phase] = val
            _save_bound_state(state, session_id)
            yield _sse({"type": "error", "phase": phase, "message": val})
        else:
            state["done"].add(phase)
            state["elapsed"][phase] = elapsed
            state["error"].pop(phase, None)
            data = _serialize(phase, val)
            _save_bound_state(state, session_id)
            yield _sse({
                "type": "done",
                "phase": phase,
                "elapsed": elapsed,
                "data": data,
            })

    return _events()

def _serialize(phase: int, val) -> dict:
    def _title_str(t):
        return t.get("title", str(t)) if isinstance(t, dict) else str(t)
    def _skill_str(s):
        return s.get("skill", str(s)) if isinstance(s, dict) else str(s)

    if phase == 1:
        p = val or {}
        return {
            "name":            p.get("name", ""),
            "email":           p.get("email", ""),
            "linkedin":        p.get("linkedin", ""),
            "location":        p.get("location", ""),
            "target_titles":   [_title_str(t) for t in (p.get("target_titles") or [])],
            "top_hard_skills": [_skill_str(s) for s in (p.get("top_hard_skills") or [])],
            "top_soft_skills": [str(s) for s in (p.get("top_soft_skills") or [])],
            "education":       p.get("education") or [],
            "resume_gaps":     p.get("resume_gaps") or [],
        }
    if phase == 2:
        jobs = val or []
        return {
            "total": len(jobs),
            "jobs": [
                {
                    "co":          j.get("company", ""),
                    "role":        j.get("title", ""),
                    "loc":         j.get("location", ""),
                    "remote":      j.get("remote", False),
                    "experience":  j.get("experience_level", ""),
                    "education":   j.get("education_required", ""),
                    "citizenship": j.get("citizenship_required", ""),
                    "salary":      j.get("salary_range", ""),
                    "platform":    j.get("platform", ""),
                    "source":      j.get("source", ""),
                    "posted":      j.get("posted_date", ""),
                    "url":         j.get("application_url", ""),
                }
                for j in jobs[:200]
            ],
        }
    if phase == 3:
        scored = val or []
        thr      = _S.get("threshold", 75)
        passed   = [j for j in scored if j.get("filter_status") == "passed"]
        auto     = [j for j in passed  if j.get("score", 0) >= thr]
        manual   = [j for j in passed  if j.get("score", 0) <  thr]
        below    = [j for j in scored  if j.get("filter_status") == "below_threshold"]
        filtered = [j for j in scored  if (j.get("filter_status") or "").startswith("filtered_")]
        all_sorted = sorted(scored, key=lambda x: x.get("score", 0), reverse=True)
        return {
            "total": len(scored), "auto": len(auto), "manual": len(manual),
            "below": len(below), "filtered": len(filtered),
            "jobs": [
                {
                    "co":          j.get("company", ""),
                    "role":        j.get("title", ""),
                    "loc":         j.get("location", ""),
                    "score":       j.get("score", 0),
                    "matching":    list(j.get("matching_skills") or [])[:6],
                    "missing":     list(j.get("missing_skills") or [])[:6],
                    "status":      j.get("filter_status", ""),
                    "reason":      j.get("filter_reason", "") or j.get("reasoning", ""),
                    "experience":  j.get("experience_level", ""),
                    "education":   j.get("education_required", ""),
                    "citizenship": j.get("citizenship_required", ""),
                    "salary":      j.get("salary_range", ""),
                    "url":         j.get("application_url", ""),
                }
                for j in all_sorted[:30]
            ],
        }
    if phase == 4:
        apps = val or []
        tmap = _S.get("tailored_map") or {}
        items = []
        for a in apps:
            jk = a.get("id") or a.get("title", "")
            td = tmap.get(jk, {})
            t  = td.get("tailored") or {}
            skills_raw = t.get("skills_reordered") or []
            skills = [s.get("skill", str(s)) if isinstance(s, dict) else str(s) for s in skills_raw]
            items.append({
                "co":            a.get("company", ""),
                "role":          a.get("title", ""),
                "score":         a.get("score", 0),
                "status":        a.get("status", ""),
                "resume_file":   a.get("resume_version", ""),
                "ats_before":    t.get("ats_score_before", 0),
                "ats_after":     t.get("ats_score_after", 0),
                "ats_gaps":      [s.get("skill", str(s)) if isinstance(s, dict) else str(s) for s in (t.get("ats_keywords_missing") or [])][:6],
                "skills":        skills[:8],
                "has_cl":        bool(t.get("cover_letter")),
            })
        return {"count": len(apps), "items": items}
    if phase == 5:
        apps = val or []
        applied = sum(1 for a in apps if a.get("status") == "Applied")
        manual  = sum(1 for a in apps if a.get("status") == "Manual Required")
        return {
            "applied": applied, "manual": manual,
            "apps": [
                {
                    "co":           a.get("company", ""),
                    "role":         a.get("title", ""),
                    "score":        a.get("score", 0),
                    "status":       a.get("status", ""),
                    "confirmation": a.get("confirmation", ""),
                    "resume":       a.get("resume_version", ""),
                    "url":          a.get("application_url", ""),
                }
                for a in apps
            ],
        }
    if phase == 6:
        p = Path(str(val)) if val else None
        return {"tracker": p.name if p else "", "url": _output_url(p) if p else ""}
    if phase == 7:
        return {"report": str(val) if val else ""}
    return {}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _guess_phase(name: str) -> int:
    if any(x in name for x in ("_Resume_", ".tex", ".pdf")):
        return 4
    if "Tracker" in name or ".xlsx" in name:
        return 6
    if "report" in name.lower() or name.endswith(".md"):
        return 7
    return 0

def _dedupe_jobs_for_state(jobs: list) -> list:
    seen = set()
    out = []
    for job in jobs:
        key = (
            job.get("application_url")
            or f"{job.get('company', '').strip().lower()}|{job.get('title', '').strip().lower()}"
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out

# ── Resume endpoints ──────────────────────────────────────────────────────────

@app.post("/api/resume/upload")
async def upload_resume(file: UploadFile = File(...)):
    import tempfile
    suffix = Path(file.filename or "resume.pdf").suffix or ".pdf"
    content = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        text, latex = _read_resume(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if not text:
        raise HTTPException(400, "Could not extract text from resume")
    _S["resume_text"] = text
    _S["latex_source"] = latex
    _S["resume_filename"] = file.filename
    return {"ok": True, "filename": file.filename, "length": len(text)}

@app.post("/api/resume/demo")
def load_demo_resume():
    _S["resume_text"] = _build_demo_resume()
    _S["latex_source"] = None
    _S["resume_filename"] = "demo_resume.txt"
    return {"ok": True, "filename": "demo_resume.txt"}

# ── Config ────────────────────────────────────────────────────────────────────

@app.post("/api/config")
async def update_config(req: Request):
    body = await req.json()
    for k in (
        "mode", "api_key", "ollama_model",
        "threshold", "job_titles", "location",
        "max_apps", "max_scrape_jobs", "days_old",
        "cover_letter", "blacklist", "whitelist",
        "experience_levels", "education_filter",
        "include_unknown_education", "citizenship_filter",
        "use_simplify", "llm_score_limit",
        "force_dev_mode", "force_customer_mode",
    ):
        if k in body:
            _S[k] = body[k]
    return {"ok": True}


# ── State ─────────────────────────────────────────────────────────────────────

@app.get("/api/state")
def get_state(request: Request):
    # Localhost connections are always treated as developer sessions
    client_host = getattr(request.client, "host", "") if request.client else ""
    is_local = client_host in ("127.0.0.1", "::1", "localhost")
    is_dev = False if _S.get("force_customer_mode") else (
        is_local or bool(_S.get("force_dev_mode"))
    )
    auth_user = _AUTH_SESSIONS.get(request.cookies.get(_AUTH_COOKIE, ""))

    profile = _S.get("profile") or {}
    scored  = _S.get("scored") or []
    thr     = _S.get("threshold", 75)
    passed   = [j for j in scored if j.get("filter_status") == "passed"]
    auto     = [j for j in passed  if j.get("score", 0) >= thr]
    manual   = [j for j in passed  if j.get("score", 0) <  thr]

    files = []
    user_output = _session_output_dir()
    if user_output.exists():
        for f in sorted(user_output.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and not f.name.startswith("."):
                files.append({
                    "name": f.name,
                    "phase": _guess_phase(f.name),
                    "size_kb": round(f.stat().st_size / 1024, 1),
                    "url": _output_url(f),
                })

    return {
        "done": list(_S["done"]),
        "error": _S.get("error", {}),
        "elapsed": _S.get("elapsed", {}),
        "has_resume": bool(_S.get("resume_text")),
        "resume_filename": _S.get("resume_filename"),
        "mode": _S.get("mode", "demo"),
        "ollama_model": _S.get("ollama_model", "llama3.2"),
        "threshold": _S.get("threshold", 75),
        "job_titles": _S.get("job_titles", ""),
        "location": _S.get("location", "United States"),
        "max_apps": _S.get("max_apps", 10),
        "max_scrape_jobs": _S.get("max_scrape_jobs", 20),
        "days_old": _S.get("days_old", 30),
        "cover_letter": _S.get("cover_letter", False),
        "blacklist": _S.get("blacklist", ""),
        "whitelist": _S.get("whitelist", ""),
        "experience_levels": _S.get("experience_levels", ["internship", "entry-level"]),
        "education_filter": _S.get("education_filter", ["bachelors"]),
        "include_unknown_education": _S.get("include_unknown_education", True),
        "citizenship_filter": _S.get("citizenship_filter", "exclude_required"),
        "use_simplify": _S.get("use_simplify", True),
        "llm_score_limit": _S.get("llm_score_limit", 10),
        "profile": {
            "name": profile.get("name", ""),
            "email": profile.get("email", ""),
            "location": profile.get("location", ""),
            "target_titles": profile.get("target_titles", []),
            "top_hard_skills": profile.get("top_hard_skills", []),
            "top_soft_skills": profile.get("top_soft_skills", []),
            "resume_gaps": profile.get("resume_gaps", []),
        } if profile else None,
        "job_count": len(_S.get("jobs") or []),
        "scored_summary": {
            "total": len(scored),
            "auto": len(auto),
            "manual": len(manual),
            "below": sum(1 for j in scored if j.get("filter_status") == "below_threshold"),
            "filtered": sum(1 for j in scored if (j.get("filter_status") or "").startswith("filtered_")),
            "jobs": [
                {
                    "co":       j.get("company", ""),
                    "role":     j.get("title", ""),
                    "loc":      j.get("location", ""),
                    "score":    j.get("score", 0),
                    "id":       j.get("id") or f"{j.get('company', '')}|{j.get('title', '')}",
                    "url":      j.get("application_url", ""),
                    "skills":   ", ".join(list(j.get("matching_skills") or [])[:4]),
                    "status":   j.get("filter_status", ""),
                }
                for j in sorted(passed, key=lambda x: x.get("score", 0), reverse=True)[:20]
            ],
        } if scored else None,
        "applications": [
            {
                "co":           a.get("company", ""),
                "role":         a.get("title", ""),
                "loc":          a.get("location", ""),
                "score":        a.get("score", 0),
                "app_status":   a.get("status", ""),
                "confirmation": a.get("confirmation", ""),
                "id":           a.get("id") or f"{a.get('company', '')}|{a.get('title', '')}",
                "url":          a.get("application_url", ""),
                "date":         datetime.now().strftime("%Y-%m-%d"),
            }
            for a in (_S.get("applications") or [])
        ],
        "output_files": files,
        # Auth / session
        "is_dev": is_dev,
        "user": auth_user or _S.get("user") or ({"email": "dev@localhost", "name": "Developer"} if is_dev else None),
        # Resume list (synthesised from single-resume state)
        "resumes": (
            [{
                "id": "primary",
                "filename": _S.get("resume_filename") or "resume.txt",
                "primary": True,
                "created_at": None,
            }]
            if _S.get("resume_text") else []
        ),
        # UI state
        "liked_ids": list(_S.get("liked_ids") or []),
        "hidden_ids": list(_S.get("hidden_ids") or []),
        "dev_tweaks": _S.get("dev_tweaks") or {},
    }

@app.post("/api/reset")
def reset_state():
    for k in ("profile", "jobs", "scored", "applications", "tracker_path"):
        _S[k] = None
    _S["tailored_map"] = {}
    _S["done"] = set()
    _S["error"] = {}
    _S["elapsed"] = {}
    _S["liked_ids"] = set()
    _S["hidden_ids"] = set()
    return {"ok": True}


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def auth_login(req: Request):
    body = await req.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        return JSONResponse({"ok": False, "error": "Email and password are required"})
    if _user_store is None:
        return JSONResponse({"ok": False, "error": "Auth store unavailable"})
    user = _user_store.get_user_by_email(email)
    if not user or not verify_password(password, user.get("password_hash") or ""):
        return JSONResponse({"ok": False, "error": "Invalid email or password"})
    auth_user = {"id": user["id"], "email": user["email"]}
    _S["user"] = auth_user
    _session_store.associate_session_with_user(_S.session_id(), user["id"])
    token = secrets.token_urlsafe(32)
    _AUTH_SESSIONS[token] = auth_user
    resp = JSONResponse({"ok": True, "user": {"email": user["email"]}})
    resp.set_cookie(_AUTH_COOKIE, token, httponly=True, samesite="lax")
    return resp

@app.post("/api/auth/signup")
async def auth_signup(req: Request):
    body = await req.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or len(password) < 6:
        return JSONResponse({"ok": False, "error": "Valid email and password (≥6 chars) required"})
    if _user_store is None:
        return JSONResponse({"ok": False, "error": "Auth store unavailable"})
    if _user_store.get_user_by_email(email):
        return JSONResponse({"ok": False, "error": "An account with this email already exists"})
    pw_hash = hash_password(password)
    user_id = _user_store.create_user(email, pw_hash)
    auth_user = {"id": user_id, "email": email}
    _S["user"] = auth_user
    _session_store.associate_session_with_user(_S.session_id(), user_id)
    token = secrets.token_urlsafe(32)
    _AUTH_SESSIONS[token] = auth_user
    resp = JSONResponse({"ok": True, "user": {"email": email}})
    resp.set_cookie(_AUTH_COOKIE, token, httponly=True, samesite="lax")
    return resp

@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = request.cookies.get(_AUTH_COOKIE, "")
    if token:
        _AUTH_SESSIONS.pop(token, None)
    _S["user"] = None
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_AUTH_COOKIE)
    return resp

@app.get("/api/auth/google")
def auth_google(request: Request):
    try:
        redirect_uri = str(request.url_for("auth_google_callback"))
        url, state = get_google_auth_url(redirect_uri)
        _S["google_oauth_state"] = state
        return {"ok": True, "url": url}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.get("/api/auth/google/callback")
def auth_google_callback(request: Request, code: str = "", state: str = ""):
    from fastapi.responses import RedirectResponse
    if not code:
        return RedirectResponse("/app#auth")
    expected_state = _S.get("google_oauth_state")
    if expected_state and state != expected_state:
        return RedirectResponse("/app#auth")
    try:
        redirect_uri = str(request.url_for("auth_google_callback"))
        info = verify_google_token(code, redirect_uri, state)
        email = (info.get("email") or "").strip().lower()
        google_id = info.get("sub") or info.get("id")
        name = info.get("name") or email.split("@")[0]
        if not email:
            return RedirectResponse("/app#auth")

        user = None
        if _user_store is not None:
            if google_id:
                user = _user_store.get_user_by_google_id(google_id)
            if user is None:
                user = _user_store.get_user_by_email(email)
            if user is None:
                user_id = _user_store.create_user(
                    email=email,
                    password_hash=None,
                    google_id=google_id,
                )
                user = {"id": user_id, "email": email, "google_id": google_id}
            auth_user = {"id": user["id"], "email": user["email"], "name": name}
            _S["user"] = auth_user
            _session_store.associate_session_with_user(_S.session_id(), user["id"])
        else:
            auth_user = {"email": email, "name": name}
            _S["user"] = auth_user

        token = secrets.token_urlsafe(32)
        _AUTH_SESSIONS[token] = auth_user
        _S.pop("google_oauth_state", None)
        resp = RedirectResponse("/app")
        resp.set_cookie(_AUTH_COOKIE, token, httponly=True, samesite="lax")
        return resp
    except Exception:
        return RedirectResponse("/app#auth")


# ── Profile ───────────────────────────────────────────────────────────────────

@app.get("/api/profile")
def get_profile():
    return _S.get("profile") or {}

@app.post("/api/profile")
async def save_profile(req: Request):
    body = await req.json()
    if _S.get("profile"):
        _S["profile"].update(body)
    else:
        _S["profile"] = body
    return {"ok": True}

@app.post("/api/profile/extract")
async def extract_profile(req: Request):
    body = await req.json()
    if not _S.get("resume_text"):
        raise HTTPException(400, "No resume loaded")
    preferred = body.get("preferred_titles")
    def _fn():
        prov = _make_provider()
        result = phase1_ingest_resume(_S["resume_text"], prov, preferred_titles=preferred or None)
        _S["profile"] = result
        _S["done"].add(1)
        return result
    import threading as _t
    err = {}
    def _run():
        try: _fn()
        except Exception as e: err["e"] = str(e)
    th = _t.Thread(target=_run, daemon=True)
    th.start(); th.join(timeout=120)
    if th.is_alive():
        raise HTTPException(504, "Profile extraction timed out")
    if err: raise HTTPException(500, err["e"])
    return {"ok": True}


# ── Resume extra endpoints ────────────────────────────────────────────────────

@app.get("/api/resume/content")
def resume_content():
    return {"text": _S.get("resume_text") or ""}

@app.post("/api/resume/text")
async def resume_save_text(req: Request):
    body = await req.json()
    text = body.get("text", "")
    _S["resume_text"] = text
    _S["done"].discard(1)  # invalidate profile so it gets re-extracted
    return {"ok": True}

@app.delete("/api/resume/{resume_id}")
def resume_delete(resume_id: str):
    _S["resume_text"] = None
    _S["latex_source"] = None
    _S["resume_filename"] = None
    _S["profile"] = None
    _S["done"].discard(1)
    return {"ok": True}

@app.post("/api/resume/primary/{resume_id}")
def resume_set_primary(resume_id: str):
    return {"ok": True}

@app.post("/api/resume/rename/{resume_id}")
async def resume_rename(resume_id: str, req: Request):
    body = await req.json()
    _S["resume_filename"] = body.get("filename", _S.get("resume_filename"))
    return {"ok": True}


# ── Jobs actions ─────────────────────────────────────────────────────────────

@app.post("/api/jobs/action")
async def jobs_action(req: Request):
    body = await req.json()
    action = body.get("action", "")
    job_id = body.get("job_id", "")
    if action == "like":
        _S["liked_ids"].add(job_id)
    elif action == "unlike":
        _S["liked_ids"].discard(job_id)
    elif action == "hide":
        _S["hidden_ids"].add(job_id)
    elif action == "unhide":
        _S["hidden_ids"].discard(job_id)
    return {"ok": True}


# ── Feedback ──────────────────────────────────────────────────────────────────

@app.post("/api/feedback")
async def submit_feedback(req: Request):
    body = await req.json()
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "Message is required")
    entry = {"id": str(time.time()), "message": msg, "read": False,
             "created_at": datetime.now().isoformat()}
    _S["feedback"].append(entry)
    print(f"[Feedback] {msg[:120]}")
    return {"ok": True}


# ── Dev endpoints ─────────────────────────────────────────────────────────────

def _is_dev_request(request: Request) -> bool:
    host = getattr(request.client, "host", "") if request.client else ""
    if _S.get("force_customer_mode"):
        return False
    return host in ("127.0.0.1", "::1", "localhost") or bool(_S.get("force_dev_mode"))

@app.get("/api/dev/overview")
def dev_overview(request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    import shutil, sys as _sys
    disk = shutil.disk_usage(OUTPUT_DIR)
    out_files = list(OUTPUT_DIR.glob("*")) if OUTPUT_DIR.exists() else []
    sessions = [{
        "id": "local",
        "name": (_S.get("profile") or {}).get("name") or "Local User",
        "email": (_S.get("user") or {}).get("email") or "",
        "has_resume": bool(_S.get("resume_text")),
        "resume_filename": _S.get("resume_filename") or "",
        "mode": _S.get("mode", "demo"),
        "done": sorted(_S["done"]),
        "errors": _S.get("error") or {},
        "job_count": len(_S.get("jobs") or []),
        "scored_count": len(_S.get("scored") or []),
        "application_count": len(_S.get("applications") or []),
        "applied_count": sum(1 for a in (_S.get("applications") or []) if a.get("status") == "Applied"),
        "manual_count": sum(1 for a in (_S.get("applications") or []) if a.get("status") == "Manual Required"),
        "target": _S.get("job_titles") or "",
        "location": _S.get("location") or "",
        "feedback_count": len(_S.get("feedback") or []),
        "unread_feedback_count": sum(1 for f in (_S.get("feedback") or []) if not f.get("read")),
    }]
    apps = _S.get("applications") or []
    return {
        "summary": {
            "users": 1,
            "with_resume": 1 if _S.get("resume_text") else 0,
            "applications": len(apps),
            "applied": sum(1 for a in apps if a.get("status") == "Applied"),
            "manual": sum(1 for a in apps if a.get("status") == "Manual Required"),
            "errors": len([v for v in (_S.get("error") or {}).values() if v]),
        },
        "status": {
            "app": "running",
            "python": _sys.version.split()[0],
            "output_files": len(out_files),
            "session_files": 1,
            "session_db_mb": round(
                (OUTPUT_DIR / "jobs_ai_sessions.sqlite3").stat().st_size / 1e6, 2
            ) if (OUTPUT_DIR / "jobs_ai_sessions.sqlite3").exists() else 0,
            "disk_free_gb": round(disk.free / 1e9, 1),
            "tweaks": _S.get("dev_tweaks") or {},
        },
        "sessions": sessions,
        "events": [],
    }

@app.get("/api/dev/session/{session_id}")
def dev_session_detail(session_id: str, request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    return {
        **(_S.get("profile") or {}),
        "resume_text": (_S.get("resume_text") or "")[:2000],
        "feedback": _S.get("feedback") or [],
    }

@app.post("/api/dev/session/{session_id}/reset")
def dev_session_reset(session_id: str, request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    return reset_state()

@app.delete("/api/dev/session/{session_id}")
def dev_session_delete(session_id: str, request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    return reset_state()

@app.post("/api/dev/session/{session_id}/impersonate")
def dev_impersonate(session_id: str, request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    return {"ok": True}

@app.post("/api/dev/session/stop-impersonating")
def dev_stop_impersonate(request: Request):
    return {"ok": True}

@app.post("/api/dev/session/{session_id}/feedback/read")
def dev_mark_feedback_read(session_id: str, request: Request):
    for f in _S.get("feedback") or []:
        f["read"] = True
    return {"ok": True}

@app.post("/api/dev/cli")
async def dev_cli(request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    body = await request.json()
    command = body.get("command", "")
    import subprocess
    safe = {
        "git_status":     ["git", "status", "--short"],
        "recent_outputs": ["python", "-c", f"import os; files=sorted(os.listdir(r'{OUTPUT_DIR}'), key=lambda f: os.path.getmtime(os.path.join(r'{OUTPUT_DIR}',f)), reverse=True)[:20]; print('\\n'.join(files))"],
        "session_db":     ["python", "-c", "import sqlite3,os; db='" + str(OUTPUT_DIR / 'jobs_ai_sessions.sqlite3') + "'; print(f'Size: {os.path.getsize(db)/1024:.1f} KB' if os.path.exists(db) else 'No DB yet')"],
        "pip_freeze":     ["pip", "freeze"],
    }
    cmd = safe.get(command)
    if not cmd:
        return {"output": f"Unknown command: {command}"}
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=10)
        return {"output": out[:4000]}
    except Exception as e:
        return {"output": str(e)[:2000]}

@app.post("/api/dev/tweaks")
async def dev_tweaks(request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    body = await request.json()
    _S["dev_tweaks"] = {**(_S.get("dev_tweaks") or {}), **body}
    return {"tweaks": _S["dev_tweaks"]}

def _clear_phases_after(phase: int):
    """Clear results and state from phase+1 onwards."""
    if phase < 1:
        _clear_phases_after(1)
        return
    clear_map = {
        1: ("jobs", "scored", "applications", "tracker_path"),
        2: ("scored", "applications", "tracker_path"),
        3: ("applications", "tracker_path"),
        4: ("applications", "tracker_path"),
        5: ("tracker_path",),
    }
    for k in clear_map.get(phase, []):
        _S[k] = None
    if phase <= 4:
        _S["tailored_map"] = {}
    for p in range(phase + 1, 8):
        _S["done"].discard(p)
        _S["error"].pop(p, None)
        _S["elapsed"].pop(p, None)

# ── Phase 1 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/1/run")
def run_phase1():
    if not _S.get("resume_text"):
        raise HTTPException(400, "Load a resume first (sidebar → Resume)")
    preferred = [
        t.strip()
        for t in (_S.get("job_titles") or "").split(",")
        if t.strip() and t.strip().lower() != "engineer"
    ]

    def _fn():
        prov = _make_provider()
        result = phase1_ingest_resume(
            _S["resume_text"], prov,
            preferred_titles=preferred or None,
        )
        _S["profile"] = result
        return result

    return StreamingResponse(
        _run_phase_sse(1, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/api/phase/1/rerun")
def rerun_phase1():
    _clear_phases_after(1)
    return run_phase1()

# ── Phase 2 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/2/run")
def run_phase2(request: Request = None):
    if not _S.get("profile"):
        raise HTTPException(400, "Run Phase 1 first")
    titles = [
        t.strip()
        for t in (_S.get("job_titles") or "Engineer").split(",")
        if t.strip()
    ]
    loc = _S.get("location") or "United States"
    params = request.query_params if request else {}
    deep = str(params.get("deep", "")).lower() in ("1", "true", "yes")
    append = str(params.get("append", "")).lower() in ("1", "true", "yes")
    force_live = str(params.get("force", "")).lower() in ("1", "true", "yes") or append

    def _fn():
        prov = _make_provider()
        offset = len(_S.get("jobs") or []) if append else 0
        result = phase2_discover_jobs(
            _S["profile"], titles, loc, prov,
            use_simplify=_S.get("use_simplify", True),
            max_jobs=_S.get("max_scrape_jobs", 20),
            days_old=_S.get("days_old", 30),
            education_filter=_S.get("education_filter") or None,
            include_unknown_education=_S.get("include_unknown_education", True),
            deep_search=deep,
            force_live=force_live,
            offset=offset,
        )
        # Apply blacklist
        bl = {c.strip().lower() for c in (_S.get("blacklist") or "").split(",") if c.strip()}
        if bl:
            result = [j for j in result if j.get("company", "").lower() not in bl]
        if append:
            result = _dedupe_jobs_for_state((_S.get("jobs") or []) + result)
        _S["jobs"] = result
        return result

    return StreamingResponse(
        _run_phase_sse(2, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/api/phase/2/rerun")
def rerun_phase2(request: Request):
    append = str(request.query_params.get("append", "")).lower() in ("1", "true", "yes")
    if not append:
        _clear_phases_after(2)
    return run_phase2(request)

# ── Phase 3 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/3/run")
def run_phase3(request: Request = None):
    if _S.get("jobs") is None:
        raise HTTPException(400, "Run Phase 2 first")

    def _fn():
        prov = _make_provider()
        params = request.query_params if request else {}
        fast_only = str(params.get("fast", "")).lower() in ("1", "true", "yes")
        result = phase3_score_jobs(
            _S["jobs"], _S["profile"], prov, min_score=60,
            experience_levels=_S.get("experience_levels") or None,
            citizenship_filter=_S.get("citizenship_filter", "all"),
            llm_score_limit=_S.get("llm_score_limit", 10),
            fast_only=fast_only,
        )
        _S["scored"] = result
        return result

    return StreamingResponse(
        _run_phase_sse(3, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/api/phase/3/rerun")
def rerun_phase3(request: Request):
    _clear_phases_after(3)
    return run_phase3(request)

# ── Phase 4 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/4/run")
def run_phase4():
    if _S.get("scored") is None:
        raise HTTPException(400, "Run Phase 3 first")

    def _fn():
        prov   = _make_provider()
        scored = _S["scored"] or []
        passed = [j for j in scored if j.get("filter_status") == "passed"]
        tailored_map = {}
        apps = []
        for job in passed:
            jk = job.get("id") or job.get("title", "")
            try:
                tailored = phase4_tailor_resume(
                    job, _S["profile"],
                    _S.get("resume_text", ""), prov,
                    include_cover_letter=bool(_S.get("cover_letter", False)),
                )
                resume_files = _save_tailored_resume(
                    job, tailored, _S["profile"],
                    _S.get("latex_source"),
                    resume_text=_S.get("resume_text", ""),
                    output_dir=_session_output_dir(),
                )
                resume_file = resume_files.get("pdf") or resume_files.get("tex")
                resume_path = _session_output_dir() / resume_file
                resume_ref = Path(_output_url(resume_path)).as_posix().removeprefix("/output/")
                tailored_map[jk] = {"job": job, "tailored": tailored, "resume_file": resume_ref}
                apps.append({**job, "resume_version": resume_ref, "status": "Tailored"})
            except Exception as exc:
                apps.append({**job, "status": "Error", "notes": str(exc)})
        _S["tailored_map"] = tailored_map
        _S["applications"] = apps
        return apps

    return StreamingResponse(
        _run_phase_sse(4, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/api/phase/4/rerun")
def rerun_phase4():
    _clear_phases_after(4)
    return run_phase4()

# ── Phase 5 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/5/run")
def run_phase5():
    if _S.get("applications") is None:
        raise HTTPException(400, "Run Phase 4 first")

    def _fn():
        apps      = _S["applications"] or []
        thr       = _S.get("threshold", 75)
        max_apps  = _S.get("max_apps", 10)
        already   = _load_existing_applications(_session_output_dir())
        results   = []
        submitted = 0
        for job in apps:
            if job.get("score", 0) >= thr and submitted < max_apps:
                res = phase5_simulate_submission(job, already_applied=already)
                if res.get("status") != "Skipped":
                    submitted += 1
                already.add((job.get("company", "").lower(), job.get("title", "").lower()))
                results.append({**job, **res})
            else:
                results.append({**job, "status": "Manual Required", "confirmation": "N/A"})
        _S["applications"] = results
        return results

    return StreamingResponse(
        _run_phase_sse(5, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/api/phase/5/rerun")
def rerun_phase5():
    _clear_phases_after(5)
    return run_phase5()

# ── Phase 6 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/6/run")
def run_phase6():
    if 5 not in _S["done"]:
        raise HTTPException(400, "Run Phase 5 first")

    def _fn():
        apps          = _S["applications"] or []
        tracker_path  = phase6_update_tracker(apps, output_dir=_session_output_dir())
        _S["tracker_path"] = tracker_path
        return tracker_path

    return StreamingResponse(
        _run_phase_sse(6, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/api/phase/6/rerun")
def rerun_phase6():
    _clear_phases_after(6)
    return run_phase6()

# ── Phase 7 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/7/run")
def run_phase7():
    if 6 not in _S["done"]:
        raise HTTPException(400, "Run Phase 6 first")

    def _fn():
        prov   = _make_provider()
        apps   = _S["applications"] or []
        report = phase7_run_report(apps, _S.get("tracker_path"), prov, output_dir=_session_output_dir())
        _S["report"] = report
        return report

    return StreamingResponse(
        _run_phase_sse(7, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Phase 2 cache ─────────────────────────────────────────────────────────────

@app.get("/api/phase/2/cache")
def phase2_cache_status():
    import json as _json
    caches = [RESOURCES_DIR / "sample_jobs_quick.json", RESOURCES_DIR / "sample_jobs_deep.json"]
    existing = [p for p in caches if p.exists()]
    if not existing:
        return {"exists": False}
    try:
        entries = []
        total = 0
        newest = max(p.stat().st_mtime for p in existing)
        for cache in existing:
            payload = _json.loads(cache.read_text(encoding="utf-8"))
            jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
            total += len(jobs or [])
            entries.append({"name": cache.name, "count": len(jobs or [])})
        age_h = int((time.time() - newest) / 3600)
        return {"exists": True, "count": total, "age_h": age_h, "files": entries}
    except Exception as e:
        return {"exists": True, "count": 0, "age_h": 0, "files": [], "error": str(e)}


@app.delete("/api/phase/2/cache")
def phase2_cache_clear():
    for cache in (RESOURCES_DIR / "sample_jobs_quick.json", RESOURCES_DIR / "sample_jobs_deep.json"):
        cache.unlink(missing_ok=True)
    for k in ("jobs", "scored", "applications", "tracker_path"):
        _S[k] = None
    _S["tailored_map"] = {}
    for phase in (2, 3, 4, 5, 6, 7):
        _S["done"].discard(phase)
        _S["error"].pop(phase, None)
    return {"ok": True}


# ── Ollama helpers ────────────────────────────────────────────────────────────

@app.get("/api/ollama/status")
def ollama_status():
    import urllib.request as _ur
    import json as _json
    model = _S.get("ollama_model", "llama3.2")
    try:
        resp = _ur.urlopen("http://localhost:11434/api/tags", timeout=3)
        data = _json.loads(resp.read().decode())
        models = data.get("models", [])
        local_names = [m.get("name", "") for m in models]
        local_bases = {n.split(":")[0] for n in local_names}
        req_base = model.split(":")[0]
        pulled = (model in local_names) or (req_base in local_bases)
        # Build rich model list with metadata
        model_list = [
            {
                "name":   m.get("name", ""),
                "size_gb": round(m.get("size", 0) / 1e9, 1),
                "family": m.get("details", {}).get("family", ""),
                "params": m.get("details", {}).get("parameter_size", ""),
            }
            for m in models
        ]
        return {
            "running": True, "pulled": pulled, "model": model,
            "available": sorted(local_bases),
            "models": model_list,
        }
    except Exception as e:
        return {"running": False, "pulled": False, "model": model, "available": [], "models": [], "error": str(e)}


@app.get("/api/ollama/pull")
def ollama_pull():
    import subprocess
    model = _S.get("ollama_model", "llama3.2")

    def _stream():
        yield _sse({"type": "start", "model": model})
        try:
            proc = subprocess.Popen(
                ["ollama", "pull", model],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    yield _sse({"type": "log", "line": line})
            proc.wait()
            if proc.returncode == 0:
                yield _sse({"type": "done", "model": model})
            else:
                yield _sse({"type": "error", "message": f"ollama pull exited with code {proc.returncode}"})
        except FileNotFoundError:
            yield _sse({"type": "error", "message": "ollama not found on PATH — install from ollama.com"})
        except Exception as exc:
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Jobs AI — starting on http://localhost:8000")
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
