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
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

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
    from auth_utils import hash_password, verify_password
except ImportError:
    def hash_password(pw): return pw
    def verify_password(pw, h): return pw == h

# Simple user store backed by SQLite
try:
    from session_store import SQLiteSessionStore
    _user_store = SQLiteSessionStore(
        OUTPUT_DIR / "jobs_ai_sessions.sqlite3",
        default_state_factory=dict,
    )
except Exception:
    _user_store = None
from pipeline.phases import (
    phase1_ingest_resume,
    phase2_discover_jobs,
    phase3_score_jobs,
    phase4_tailor_resume,
    phase5_simulate_submission,
    phase6_update_tracker,
    phase7_run_report,
)
from pipeline.resume import _build_demo_resume, _read_resume, _save_tailored_resume

app = FastAPI(title="Jobs AI")

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
    p = OUTPUT_DIR / path
    if not p.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(p)

# ── Single-user in-memory state ───────────────────────────────────────────────

_S: dict = {
    # LLM backend
    "mode": "ollama",
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
    # Resume — scalar fields kept as the "active primary" copy for pipeline use
    "resume_text": None,
    "latex_source": None,
    "resume_filename": None,
    # Multi-resume store — list of resume records (see _new_resume_record)
    "resumes": [],
    # Set of resume IDs currently being extracted in background threads
    "extracting_ids": set(),
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

# ── Resume helpers ────────────────────────────────────────────────────────────

def _new_resume_record(filename: str, text: str, latex_source=None) -> dict:
    now = datetime.now().isoformat()
    return {
        "id": uuid.uuid4().hex,
        "filename": filename,
        "text": text,
        "latex_source": latex_source,
        "profile": None,
        "primary": False,
        "created_at": now,
        "updated_at": now,
    }

def _get_primary_resume() -> dict | None:
    for r in (_S.get("resumes") or []):
        if r.get("primary"):
            return r
    rs = _S.get("resumes") or []
    return rs[0] if rs else None

def _get_resume_by_id(rid: str) -> dict | None:
    for r in (_S.get("resumes") or []):
        if r["id"] == rid:
            return r
    return None

def _sync_primary_scalars(record=None):
    """Keep backward-compat scalar fields in sync with the primary resume."""
    pr = record or _get_primary_resume()
    if pr:
        _S["resume_text"] = pr["text"]
        _S["latex_source"] = pr.get("latex_source")
        _S["resume_filename"] = pr["filename"]
        _S["profile"] = pr.get("profile")
        # If the primary already has an extracted profile, mark Phase 1 done
        if pr.get("profile"):
            _S["done"].add(1)
        else:
            _S["done"].discard(1)
    else:
        _S["resume_text"] = None
        _S["latex_source"] = None
        _S["resume_filename"] = None
        _S["profile"] = None
        _S["done"].discard(1)

def _serialize_resume(r: dict) -> dict:
    """Produce the per-resume dict returned in /api/state."""
    p = r.get("profile") or {}
    is_extracting = r["id"] in (_S.get("extracting_ids") or set())
    # Pass through the full profile, stripping internal audit metadata keys
    full_profile = {k: v for k, v in p.items() if not k.startswith("_")} if p else None
    return {
        "id": r["id"],
        "filename": r["filename"],
        "primary": bool(r.get("primary")),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
        "analyzed": bool(p) and not is_extracting,
        "extracting": is_extracting,
        "extract_error": r.get("extract_error"),
        "profile": full_profile,
    }


def _run_extraction_bg(record: dict) -> None:
    """Background-thread worker: run Phase 1 for a resume record."""
    rid = record["id"]
    _S["extracting_ids"].add(rid)
    try:
        prov = _make_provider()
        preferred = [
            t.strip()
            for t in (_S.get("job_titles") or "").split(",")
            if t.strip() and t.strip().lower() != "engineer"
        ]
        result = phase1_ingest_resume(record["text"], prov, preferred_titles=preferred or None)
        record["profile"] = result
        record["updated_at"] = datetime.now().isoformat()
        # If this resume is currently primary, promote its result to global state
        if record.get("primary"):
            _S["profile"] = result
            _S["done"].add(1)
    except Exception as e:
        record["extract_error"] = str(e)
    finally:
        _S["extracting_ids"].discard(rid)

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
    """Generator: run *fn* in a thread, stream SSE + console output."""
    result_q: "queue.Queue[tuple]" = queue.Queue()
    log_q: "queue.Queue[str]" = queue.Queue()
    _phase_logs[phase] = log_q

    def _worker():
        import sys
        old_stdout = sys.stdout
        try:
            # Wrap stdout to capture while keeping normal output
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
        # Stream captured logs
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
        yield _sse({"type": "error", "phase": phase, "message": "phase timed out"})
        return

    elapsed = round(time.time() - t0, 1)

    if status == "err":
        _S["error"][phase] = val
        yield _sse({"type": "error", "phase": phase, "message": val})
    else:
        _S["done"].add(phase)
        _S["elapsed"][phase] = elapsed
        _S["error"].pop(phase, None)
        yield _sse({
            "type": "done",
            "phase": phase,
            "elapsed": elapsed,
            "data": _serialize(phase, val),
        })

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
        p = Path(str(val)).name if val else ""
        return {"tracker": p, "url": f"/output/{p}" if p else ""}
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

# ── Resume endpoints ──────────────────────────────────────────────────────────

@app.post("/api/resume/upload")
async def upload_resume(file: UploadFile = File(...)):
    import tempfile
    fname = file.filename or "resume.pdf"
    suffix = Path(fname).suffix or ".pdf"
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

    record = _new_resume_record(fname, text, latex)
    resumes = _S.setdefault("resumes", [])

    # First upload → becomes primary; subsequent uploads are non-primary
    is_first = len(resumes) == 0
    record["primary"] = is_first
    resumes.append(record)

    if is_first:
        _sync_primary_scalars(record)

    # Auto-extract profile in a background thread for every upload
    t = threading.Thread(target=_run_extraction_bg, args=(record,), daemon=True)
    t.start()

    return {"ok": True, "filename": fname, "length": len(text), "id": record["id"], "extracting": True}

@app.post("/api/resume/demo")
def load_demo_resume():
    text = _build_demo_resume()
    record = _new_resume_record("demo_resume.txt", text, None)
    resumes = _S.setdefault("resumes", [])
    is_first = len(resumes) == 0
    record["primary"] = is_first
    resumes.append(record)
    if is_first:
        _sync_primary_scalars(record)
    t = threading.Thread(target=_run_extraction_bg, args=(record,), daemon=True)
    t.start()
    return {"ok": True, "filename": "demo_resume.txt", "id": record["id"], "extracting": True}

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
    is_dev = is_local or bool(_S.get("force_dev_mode"))

    profile = _S.get("profile") or {}
    scored  = _S.get("scored") or []
    thr     = _S.get("threshold", 75)
    passed   = [j for j in scored if j.get("filter_status") == "passed"]
    auto     = [j for j in passed  if j.get("score", 0) >= thr]
    manual   = [j for j in passed  if j.get("score", 0) <  thr]

    files = []
    if OUTPUT_DIR.exists():
        for f in sorted(OUTPUT_DIR.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and not f.name.startswith("."):
                files.append({
                    "name": f.name,
                    "phase": _guess_phase(f.name),
                    "size_kb": round(f.stat().st_size / 1024, 1),
                })

    return {
        "done": list(_S["done"]),
        "error": _S.get("error", {}),
        "elapsed": _S.get("elapsed", {}),
        "has_resume": bool(_S.get("resumes")),
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
        # Pass the full profile through — strip internal audit keys only
        "profile": {k: v for k, v in profile.items() if not k.startswith("_")} if profile else None,
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
                    "skills":   ", ".join(list(j.get("skills_matched") or [])[:4]),
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
                "date":         datetime.now().strftime("%Y-%m-%d"),
            }
            for a in (_S.get("applications") or [])
        ],
        "output_files": files,
        # Auth / session
        "is_dev": is_dev,
        "user": _S.get("user") or ({"email": "dev@localhost", "name": "Developer"} if is_dev else None),
        "resumes": [_serialize_resume(r) for r in (_S.get("resumes") or [])],
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
    # Re-sync global profile from primary resume (preserves per-resume analyses)
    pr = _get_primary_resume()
    if pr and pr.get("profile"):
        _S["profile"] = pr["profile"]
        _S["done"].add(1)  # Phase 1 is effectively already done for this resume
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
    _S["user"] = {"id": user["id"], "email": user["email"]}
    return {"ok": True, "user": {"email": user["email"]}}

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
    _S["user"] = {"id": user_id, "email": email}
    return {"ok": True, "user": {"email": email}}

@app.post("/api/auth/logout")
def auth_logout():
    _S["user"] = None
    return {"ok": True}

@app.get("/api/auth/google")
def auth_google():
    return JSONResponse({"ok": False, "error": "Google OAuth not configured — use email/password login"}, status_code=200)

@app.get("/api/auth/google/callback")
def auth_google_callback():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/app")


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
    resume_id = body.get("resume_id", "")
    target = _get_resume_by_id(resume_id) if resume_id else _get_primary_resume()
    if not target:
        raise HTTPException(400, "No resume loaded")
    preferred = body.get("preferred_titles")
    def _fn():
        prov = _make_provider()
        result = phase1_ingest_resume(target["text"], prov, preferred_titles=preferred or None)
        target["profile"] = result
        target["updated_at"] = datetime.now().isoformat()
        # If extracting for the primary, update global profile too
        if target.get("primary"):
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
    if err: raise HTTPException(500, err["e"])
    return {"ok": True}


# ── Resume extra endpoints ────────────────────────────────────────────────────

@app.get("/api/resume/content")
def resume_content(id: str = ""):
    if id:
        r = _get_resume_by_id(id)
        return {"text": r["text"] if r else ""}
    pr = _get_primary_resume()
    return {"text": pr["text"] if pr else ""}

@app.post("/api/resume/text")
async def resume_save_text(req: Request):
    body = await req.json()
    text = body.get("text", "")
    rid = body.get("id", "")
    now = datetime.now().isoformat()
    r = _get_resume_by_id(rid) if rid else _get_primary_resume()
    if r:
        r["text"] = text
        r["updated_at"] = now
        # Editing clears this resume's stored profile so it needs re-extraction
        r["profile"] = None
        if r.get("primary"):
            _S["resume_text"] = text
            _S["done"].discard(1)
    return {"ok": True}

@app.delete("/api/resume/{resume_id}")
def resume_delete(resume_id: str):
    resumes = _S.get("resumes") or []
    target = _get_resume_by_id(resume_id)
    if not target:
        raise HTTPException(404, "Resume not found")
    was_primary = target.get("primary", False)
    _S["resumes"] = [r for r in resumes if r["id"] != resume_id]
    if was_primary:
        # Pick next resume as primary, or clear everything if none left
        remaining = _S["resumes"]
        if remaining:
            remaining[0]["primary"] = True
            _sync_primary_scalars(remaining[0])
            _S["done"].discard(1)
        else:
            for k in ("resume_text", "latex_source", "resume_filename", "profile"):
                _S[k] = None
            _S["done"].discard(1)
    return {"ok": True}

@app.post("/api/resume/primary/{resume_id}")
def resume_set_primary(resume_id: str):
    target = _get_resume_by_id(resume_id)
    if not target:
        raise HTTPException(404, "Resume not found")
    for r in (_S.get("resumes") or []):
        r["primary"] = r["id"] == resume_id
    # Sync scalar fields and global profile from the new primary
    _sync_primary_scalars(target)
    # Pipeline phases downstream of 1 may be stale if the profile changed
    _S["done"].discard(1)
    return {"ok": True}

@app.post("/api/resume/rename/{resume_id}")
async def resume_rename(resume_id: str, req: Request):
    body = await req.json()
    new_name = body.get("filename", "")
    if not new_name:
        raise HTTPException(400, "filename is required")
    r = _get_resume_by_id(resume_id)
    if not r:
        raise HTTPException(404, "Resume not found")
    r["filename"] = new_name
    r["updated_at"] = datetime.now().isoformat()
    if r.get("primary"):
        _S["resume_filename"] = new_name
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
        # Persist profile on the primary resume record so it survives primary switches
        pr = _get_primary_resume()
        if pr:
            pr["profile"] = result
            pr["updated_at"] = datetime.now().isoformat()
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
def run_phase2():
    if not _S.get("profile"):
        raise HTTPException(400, "Run Phase 1 first")
    titles = [
        t.strip()
        for t in (_S.get("job_titles") or "Engineer").split(",")
        if t.strip()
    ]
    loc = _S.get("location") or "United States"

    def _fn():
        prov = _make_provider()
        result = phase2_discover_jobs(
            _S["profile"], titles, loc, prov,
            use_simplify=_S.get("use_simplify", True),
            max_jobs=_S.get("max_scrape_jobs", 20),
            days_old=_S.get("days_old", 30),
            education_filter=_S.get("education_filter") or None,
            include_unknown_education=_S.get("include_unknown_education", True),
        )
        # Apply blacklist
        bl = {c.strip().lower() for c in (_S.get("blacklist") or "").split(",") if c.strip()}
        if bl:
            result = [j for j in result if j.get("company", "").lower() not in bl]
        _S["jobs"] = result
        return result

    return StreamingResponse(
        _run_phase_sse(2, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/api/phase/2/rerun")
def rerun_phase2():
    _clear_phases_after(2)
    return run_phase2()

# ── Phase 3 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/3/run")
def run_phase3():
    if _S.get("jobs") is None:
        raise HTTPException(400, "Run Phase 2 first")

    def _fn():
        prov = _make_provider()
        result = phase3_score_jobs(
            _S["jobs"], _S["profile"], prov, min_score=60,
            experience_levels=_S.get("experience_levels") or None,
            citizenship_filter=_S.get("citizenship_filter", "all"),
            llm_score_limit=_S.get("llm_score_limit", 10),
        )
        _S["scored"] = result
        return result

    return StreamingResponse(
        _run_phase_sse(3, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/api/phase/3/rerun")
def rerun_phase3():
    _clear_phases_after(3)
    return run_phase3()

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
                )
                resume_file = resume_files.get("pdf") or resume_files.get("tex")
                tailored_map[jk] = {"job": job, "tailored": tailored, "resume_file": resume_file}
                apps.append({**job, "resume_version": resume_file, "status": "Tailored"})
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
        already   = set()
        results   = []
        submitted = 0
        for job in apps:
            if job.get("score", 0) >= thr and submitted < max_apps:
                res = phase5_simulate_submission(job, already_applied=already)
                already.add((
                    job.get("company", "").lower(),
                    job.get("title", "").lower(),
                ))
                submitted += 1
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
        tracker_path  = phase6_update_tracker(apps)
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
        report = phase7_run_report(apps, _S.get("tracker_path"), prov)
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
    cache = RESOURCES_DIR / "sample_jobs.json"
    if not cache.exists():
        return {"exists": False}
    try:
        data = _json.loads(cache.read_text(encoding="utf-8"))
        first_url = (data[0].get("application_url", "") if data else "")
        _demo_domains = [
            "nvidia.com/careers", "intel.com/jobs", "microsoft.com/careers",
            "apple.com/jobs", "lumentum.com/careers", "micron.com/careers",
            "research.ibm.com", "samsung.com/us/careers",
        ]
        is_demo = any(d in first_url for d in _demo_domains)
        age_h = int((time.time() - cache.stat().st_mtime) / 3600)
        return {"exists": True, "count": len(data), "age_h": age_h, "is_demo": is_demo}
    except Exception as e:
        return {"exists": True, "count": 0, "age_h": 0, "is_demo": False, "error": str(e)}


@app.delete("/api/phase/2/cache")
def phase2_cache_clear():
    cache = RESOURCES_DIR / "sample_jobs.json"
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
