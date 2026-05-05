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

# Load .env if python-dotenv is available. Must run before any os.environ.get("…") read.
# We try both the directory containing app.py AND its parent — users sometimes drop
# `.env` next to the cloned repo folder rather than next to the source files.
try:
    from dotenv import load_dotenv
    _here = Path(__file__).resolve().parent
    for _candidate in (_here / ".env", _here.parent / ".env"):
        if _candidate.exists():
            load_dotenv(_candidate, override=False)
except ImportError:
    pass

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
_DEV_IMPERSONATE_COOKIE = "dev_impersonate_id"
_AUTH_COOKIE = "jobs_ai_auth"
_STATE_COOKIE = "jobs_ai_session"
_SESSION_SWITCH_HEADER = "x-jobs-ai-session-switch"
# Fallback cache used only when the SQLite store is unavailable.
_AUTH_SESSIONS_FALLBACK: dict[str, dict] = {}


def _auth_token_save(token: str, user_payload: dict) -> None:
    if not token:
        return
    if _session_store is not None and user_payload.get("id"):
        _session_store.create_auth_token(token, user_payload["id"], user_payload)
    else:
        _AUTH_SESSIONS_FALLBACK[token] = user_payload


def _auth_token_lookup(token: str) -> dict | None:
    if not token:
        return None
    if _session_store is not None:
        user = _session_store.get_auth_user(token)
        if user is not None:
            return user
    return _AUTH_SESSIONS_FALLBACK.get(token)


def _auth_token_delete(token: str) -> None:
    if not token:
        return
    if _session_store is not None:
        _session_store.delete_auth_token(token)
    _AUTH_SESSIONS_FALLBACK.pop(token, None)

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
def serve_output_file(path: str, request: Request):
    root = OUTPUT_DIR.resolve()
    p = (root / path).resolve()
    if root != p and root not in p.parents:
        raise HTTPException(403, "File access denied")
    session_root = (root / "sessions").resolve()
    if p == session_root or session_root in p.parents:
        # Per-session files require auth and a session-id match. Without
        # this check, anyone who knows or guesses a session_id cookie value
        # could read another user's tailored resumes.
        auth_user = _auth_token_lookup(request.cookies.get(_AUTH_COOKIE, ""))
        if not auth_user and not _is_dev_request(request):
            raise HTTPException(401, "Sign in required")
        # Dev users may read any session's output; regular users only their own.
        if not _is_dev_request(request):
            current_root = _session_output_dir().resolve()
            if p != current_root and current_root not in p.parents:
                raise HTTPException(403, "File access denied")
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(p)

# ── Session state ─────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
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
    # Resume ? scalar fields kept as the "active primary" copy for pipeline use
    "resume_text": None,
    "latex_source": None,
    "resume_filename": None,
    # Multi-resume store ? list of resume records (see _new_resume_record)
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


class _SessionStateProxy(MutableMapping):
    def __init__(self):
        self._fallback = _default_state()
        self._state_var = contextvars.ContextVar("jobs_ai_state", default=None)
        self._session_var = contextvars.ContextVar("jobs_ai_session_id", default=None)

    def bind(self, state: dict, session_id: str | None = None):
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

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_store_db = OUTPUT_DIR / "jobs_ai_sessions.sqlite3"
try:
    _session_store = SQLiteSessionStore(_store_db, default_state_factory=_default_state)
except Exception:
    _session_store = None
_user_store = _session_store
_memory_sessions: dict[str, dict] = {}


# ── Job ingestion bootstrap ──────────────────────────────────────────────────
# Started inside FastAPI's startup event so reloads / test harnesses don't
# spawn a duplicate scheduler.
@app.on_event("startup")
def _start_ingestion() -> None:
    if _session_store is None:
        print("[ingest] session store unavailable; skipping job ingestion")
        return
    try:
        from pipeline import ingest as _job_ingest
        _job_ingest.start_scheduler(
            connect=_session_store.connect,
            run_backfill=True,
            backfill_timeout=60,
        )
    except Exception as exc:
        # Ingestion failures must not block the API from coming up.
        print(f"[ingest] failed to start scheduler: {exc!r}")


@app.on_event("shutdown")
def _stop_ingestion() -> None:
    try:
        from pipeline import ingest as _job_ingest
        _job_ingest.shutdown()
    except Exception:
        pass

# Server-wide runtime knobs editable live from Dev Ops. Distinct from per-session
# config in `_S` — these affect every session until the process restarts.
_RUNTIME: dict = {
    "maintenance": False,    # when true, /api/phase/* runs are rejected before claim
    "verbose_logs": False,   # echo SSE log lines to stderr in addition to the queue
}

# Per-session locks for state mutations done from worker threads. Multiple
# concurrent SSE phases for the same session would otherwise interleave
# `_S["done"].add`, `state["error"][phase] = ...`, and JSON saves.
_session_locks: dict[str, threading.RLock] = {}
_session_locks_guard = threading.Lock()


def _session_lock(session_id: str | None) -> threading.RLock:
    if not session_id:
        # A dummy lock per call when there is no session id (rare, dev paths).
        return threading.RLock()
    with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.RLock()
            _session_locks[session_id] = lock
        return lock


# Cap concurrent phase runs per session — one SSE pipeline at a time.
_session_running_phases: dict[str, set[int]] = {}
_session_running_guard = threading.Lock()
_MAX_CONCURRENT_PHASES_PER_SESSION = 1


def _try_claim_phase(session_id: str | None, phase: int) -> bool:
    if not session_id:
        return True
    with _session_running_guard:
        running = _session_running_phases.setdefault(session_id, set())
        if len(running) >= _MAX_CONCURRENT_PHASES_PER_SESSION:
            return False
        running.add(phase)
        return True


def _release_phase(session_id: str | None, phase: int) -> None:
    if not session_id:
        return
    with _session_running_guard:
        running = _session_running_phases.get(session_id)
        if running is not None:
            running.discard(phase)
            if not running:
                _session_running_phases.pop(session_id, None)


def _is_local_request(request: Request) -> bool:
    host = getattr(request.client, "host", "") if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _load_session_state(session_id: str) -> dict:
    if _session_store is None:
        loaded = _memory_sessions.get(session_id) or _default_state()
    else:
        loaded = _session_store.get_state(session_id)
    state = _default_state()
    state.update(loaded or {})
    state["done"] = set(state.get("done") or [])
    state["liked_ids"] = set(state.get("liked_ids") or [])
    state["hidden_ids"] = set(state.get("hidden_ids") or [])
    state["extracting_ids"] = set(state.get("extracting_ids") or [])
    return state


def _bind_request_state(request: Request) -> tuple[str, dict, bool]:
    impersonated = request.cookies.get(_DEV_IMPERSONATE_COOKIE)
    session_id = impersonated if (_is_local_request(request) and impersonated) else request.cookies.get(_STATE_COOKIE) or uuid.uuid4().hex
    state = _load_session_state(session_id)
    _S.bind(state, session_id)
    request.state.session_id = session_id
    request.state.session_state = state
    return session_id, state, not bool(request.cookies.get(_STATE_COOKIE))


def _save_bound_state(state: dict = None, session_id: str = None) -> None:
    sid = session_id or _S.session_id()
    if not sid:
        return
    payload = state or _S.current()
    with _session_lock(sid):
        if _session_store is None:
            _memory_sessions[sid] = payload
            return
        _session_store.save_state(sid, payload)


def _bind_thread_state(state: dict, session_id: str | None = None) -> None:
    _S.bind(state, session_id)


def _start_session(state: dict = None, user_id: str = None) -> str:
    session_id = uuid.uuid4().hex
    next_state = _default_state()
    if state:
        next_state.update(state)
    next_state["done"] = set(next_state.get("done") or [])
    next_state["liked_ids"] = set(next_state.get("liked_ids") or [])
    next_state["hidden_ids"] = set(next_state.get("hidden_ids") or [])
    next_state["extracting_ids"] = set(next_state.get("extracting_ids") or [])
    _S.bind(next_state, session_id)
    _save_bound_state(next_state, session_id)
    if user_id and _session_store is not None:
        _session_store.associate_session_with_user(session_id, user_id)
    return session_id


def _switch_to_user_session(user: dict, auth_user: dict) -> str:
    if _session_store is None:
        session_id = uuid.uuid4().hex
        state = _default_state()
        state["user"] = auth_user
        _S.bind(state, session_id)
        _save_bound_state(state, session_id)
        return session_id
    sessions = _session_store.get_user_sessions(user["id"])
    session_id = sessions[0] if sessions else uuid.uuid4().hex
    state = _load_session_state(session_id)
    state["user"] = auth_user
    # On every fresh login, clear stale "Test as Customer" / "Force Dev" flags.
    # Otherwise a developer who once clicked Test as Customer would be locked
    # into simulation mode forever — every subsequent sign-in inherits the
    # session's force_customer_mode, _is_dev_request returns False, and the
    # Dev Ops nav item disappears with no way back.
    if state.get("force_customer_mode") or state.get("force_dev_mode"):
        state["force_customer_mode"] = False
        state["force_dev_mode"] = False
    _S.bind(state, session_id)
    _save_bound_state(state, session_id)
    _session_store.associate_session_with_user(session_id, user["id"])
    return session_id


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
    # Bind the session state up front. A failure here (e.g. corrupt session_state JSON
    # in SQLite) used to bubble up as a plain "Internal Server Error" — the frontend
    # then choked on `JSON.parse("Internal S…")`. Convert it to a logged JSON 500 so
    # the user gets a real error message and the cause shows up in the server console.
    try:
        request_session_id, _state, is_new = _bind_request_state(request)
    except Exception as exc:
        import traceback as _tb
        print("[session_state_middleware] _bind_request_state FAILED:", file=sys.stderr)
        _tb.print_exc()
        return JSONResponse(
            {"ok": False, "error": f"Session load failed: {type(exc).__name__}: {exc}"},
            status_code=500,
        )
    response = await call_next(request)
    switched_session_id = response.headers.get(_SESSION_SWITCH_HEADER)
    if switched_session_id:
        del response.headers[_SESSION_SWITCH_HEADER]
        response.set_cookie(_STATE_COOKIE, switched_session_id, httponly=True, samesite="lax")
        return response
    current_session_id = _S.session_id() or request_session_id
    if is_new or current_session_id != request_session_id:
        response.set_cookie(_STATE_COOKIE, current_session_id, httponly=True, samesite="lax")
    # Skip the post-response state save for read-only endpoints. /api/state in
    # particular is polled every 8 s and used to clobber `google_oauth_state` set
    # by a concurrent /api/auth/google request — load happens before the OAuth
    # mutation, save happens after, so the OAuth state vanishes and the callback
    # fails the state check.
    skip_save = (
        response.headers.get("content-type", "").startswith("text/event-stream")
        or request.url.path in ("/api/state",)
    )
    if not skip_save:
        try:
            _save_bound_state(_S.current(), current_session_id)
        except Exception:
            import traceback as _tb
            _tb.print_exc()
    return response


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
    pr = record or _get_primary_resume()
    if pr:
        _S["resume_text"] = pr["text"]
        _S["latex_source"] = pr.get("latex_source")
        _S["resume_filename"] = pr["filename"]
        _S["profile"] = pr.get("profile")
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
    p = r.get("profile") or {}
    is_extracting = r["id"] in (_S.get("extracting_ids") or set())
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


def _run_extraction_bg(record: dict, state: dict, session_id: str | None,
                       force: bool = False) -> None:
    import traceback as _tb
    _bind_thread_state(state, session_id)
    rid = record["id"]
    lock = _session_lock(session_id)
    with lock:
        ids = state.get("extracting_ids")
        if not isinstance(ids, set):
            ids = set(ids or [])
            state["extracting_ids"] = ids
        ids.add(rid)
    try:
        prov = _make_provider()
        preferred = [
            t.strip()
            for t in (state.get("job_titles") or "").split(",")
            if t.strip() and t.strip().lower() != "engineer"
        ]
        result = phase1_ingest_resume(
            record["text"], prov,
            preferred_titles=preferred or None,
            force=force,
            ollama_model=_ollama_model_for_insights(),
        )
        with lock:
            # Re-resolve the record from state in case the user uploaded
            # another resume or deleted this one mid-extraction.
            current = next(
                (r for r in (state.get("resumes") or []) if r.get("id") == rid),
                None,
            )
            target = current or record
            target["profile"] = result
            target["updated_at"] = datetime.now().isoformat()
            target.pop("extract_error", None)
            # When the freshly-extracted resume is the primary, push every
            # scalar derived from it (profile, resume_text, latex_source,
            # filename, done flag) so the Profile page reflects the new data
            # without the user having to click anything.
            if target.get("primary"):
                state["resume_text"] = target["text"]
                state["latex_source"] = target.get("latex_source")
                state["resume_filename"] = target["filename"]
                state["profile"] = result
                if result:
                    state["done"].add(1)
    except Exception as e:
        with lock:
            current = next(
                (r for r in (state.get("resumes") or []) if r.get("id") == rid),
                None,
            )
            target = current or record
            target["extract_error"] = str(e)
            print(f"[resume extraction] {rid} failed: {_tb.format_exc()}", flush=True)
    finally:
        with lock:
            ids = state.get("extracting_ids")
            if isinstance(ids, set):
                ids.discard(rid)
        _save_bound_state(state, session_id)


def _ollama_model_for_insights() -> str:
    """Model used for the resume-insight verification step.

    Always returns a model name so Demo / Anthropic users still get the LLM
    double-check when a local Ollama daemon is reachable. Connection errors
    are caught inside `verify_with_ollama`, which falls back to heuristic
    insights when the daemon is offline.
    """
    return _S.get("ollama_model") or "llama3.2"
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

def _run_phase_sse(phase: int, fn, state: dict | None = None, session_id: str | None = None):
    """Generator: run *fn* in a thread, stream SSE + console output.

    Adds:
      - Per-session concurrency cap (one SSE phase at a time).
      - Cooperative cancellation when the SSE client disconnects:
        the worker thread checks `cancel_event` between log flushes,
        and we always release the running-phase claim.
      - Thread-safe state mutation under the per-session lock.
    """
    if _RUNTIME.get("maintenance"):
        yield _sse({
            "type": "error",
            "phase": phase,
            "message": "Server is in maintenance mode — phase runs are paused",
        })
        return
    # Belt-and-suspenders plan gate: defends against state set before a downgrade
    # or sneaking around the /api/config gate. Anthropic provider requires Pro.
    if state and state.get("mode") == "anthropic":
        plan = ((state.get("user") or {}).get("plan_tier")) or "free"
        if plan != "pro":
            yield _sse({
                "type": "error",
                "phase": phase,
                "message": "Claude requires the Pro plan. Switch provider in Settings or upgrade.",
                "code": "plan_required",
            })
            return
    if not _try_claim_phase(session_id, phase):
        yield _sse({
            "type": "error",
            "phase": phase,
            "message": "Another phase is already running for this session",
        })
        return

    result_q: "queue.Queue[tuple]" = queue.Queue()
    log_q: "queue.Queue[str]" = queue.Queue()
    cancel_event = threading.Event()
    _phase_logs[(session_id, phase)] = log_q
    lock = _session_lock(session_id)

    def _worker():
        import sys
        old_stdout = sys.stdout
        try:
            if state is not None:
                _bind_thread_state(state, session_id)
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

    try:
        yield _sse({"type": "start", "phase": phase})

        while t.is_alive():
            try:
                while True:
                    text = log_q.get_nowait()
                    if _RUNTIME.get("verbose_logs"):
                        print(f"[phase {phase}] {text}", file=sys.stderr, flush=True)
                    yield _sse({"type": "log", "phase": phase, "text": text})
            except queue.Empty:
                pass
            yield ": keep-alive\n\n"
            t.join(timeout=0.2)

        try:
            status, val = result_q.get_nowait()
        except queue.Empty:
            with lock:
                state["error"][phase] = "phase timed out"
            _save_bound_state(state, session_id)
            yield _sse({"type": "error", "phase": phase, "message": "phase timed out"})
            return

        elapsed = round(time.time() - t0, 1)

        if status == "err":
            with lock:
                state["error"][phase] = val
            _save_bound_state(state, session_id)
            yield _sse({"type": "error", "phase": phase, "message": val})
        else:
            with lock:
                state["done"].add(phase)
                state["elapsed"][phase] = elapsed
                state["error"].pop(phase, None)
            _save_bound_state(state, session_id)
            yield _sse({
                "type": "done",
                "phase": phase,
                "elapsed": elapsed,
                "data": _serialize(phase, val),
            })
    except GeneratorExit:
        # Client disconnected. Signal the worker so cooperative tasks can stop;
        # daemon threads will not block process exit either way.
        cancel_event.set()
        raise
    finally:
        _release_phase(session_id, phase)
        _phase_logs.pop((session_id, phase), None)
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

def _require_auth_user(request: Request) -> dict:
    """Reject unauthenticated callers. Prevents ghost profiles from anonymous uploads."""
    auth_user = _auth_token_lookup(request.cookies.get(_AUTH_COOKIE, ""))
    if not auth_user:
        raise HTTPException(401, "Sign in required")
    return auth_user


@app.post("/api/resume/upload")
async def upload_resume(request: Request, file: UploadFile = File(...)):
    _require_auth_user(request)
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

    # Pre-mark extracting so the very next /api/state poll sees the badge.
    _S["extracting_ids"].add(record["id"])

    # Auto-extract profile in a background thread for every upload
    t = threading.Thread(target=_run_extraction_bg, args=(record, _S.current(), _S.session_id()), daemon=True)
    t.start()

    return {"ok": True, "filename": fname, "length": len(text), "id": record["id"], "extracting": True}

@app.post("/api/resume/demo")
def load_demo_resume(request: Request):
    _require_auth_user(request)
    text = _build_demo_resume()
    record = _new_resume_record("demo_resume.txt", text, None)
    resumes = _S.setdefault("resumes", [])
    is_first = len(resumes) == 0
    record["primary"] = is_first
    resumes.append(record)
    if is_first:
        _sync_primary_scalars(record)
    _S["extracting_ids"].add(record["id"])
    t = threading.Thread(target=_run_extraction_bg, args=(record, _S.current(), _S.session_id()), daemon=True)
    t.start()
    return {"ok": True, "filename": "demo_resume.txt", "id": record["id"], "extracting": True}

# ── Config ────────────────────────────────────────────────────────────────────

@app.post("/api/config")
async def update_config(req: Request):
    body = await req.json()
    # Plan gate: switching the LLM provider to Anthropic requires Pro. Devs with
    # is_developer=1 (or LOCAL_DEV_BYPASS=1 on localhost) bypass so dev workflows
    # aren't blocked.
    if body.get("mode") == "anthropic":
        auth_user = _auth_token_lookup(req.cookies.get(_AUTH_COOKIE, ""))
        plan = (auth_user or {}).get("plan_tier") or "free"
        if plan != "pro" and not _is_underlying_dev_request(req):
            return JSONResponse(
                {"ok": False, "error": "Claude requires the Pro plan", "code": "plan_required"},
                status_code=402,
            )
    for k in (
        "mode", "api_key", "ollama_model",
        "threshold", "job_titles", "location",
        "max_apps", "max_scrape_jobs", "days_old",
        "cover_letter", "blacklist", "whitelist",
        "experience_levels", "education_filter",
        "include_unknown_education", "citizenship_filter",
        "use_simplify", "llm_score_limit",
        "force_customer_mode",
        "light_mode",
    ):
        if k in body:
            _S[k] = body[k]
    return {"ok": True}


# ── State ─────────────────────────────────────────────────────────────────────

def _index_total_active() -> int:
    """Total non-deleted job_postings rows. Used to give Dashboard / rail
    a non-zero count even before Phase 3 has run on any session."""
    if _session_store is None:
        return 0
    try:
        from pipeline import job_repo
        with _session_store.connect() as conn:
            return int(job_repo.total_active(conn))
    except Exception:
        return 0


def _scored_summary_from_index(profile: dict | None) -> dict | None:
    """Build a rough scored_summary from the live index when no Phase 3
    has run yet. Lets Dashboard / AgentPage / rail counts light up
    without forcing the LLM scorer.
    """
    if _session_store is None:
        return None
    try:
        from pipeline.job_search import search, SearchFilters
        from pipeline import job_repo
        thr = int(_S.get("threshold", 75) or 75)
        prof_for_search = None
        if profile:
            prof_for_search = {
                "target_titles": profile.get("target_titles") or [],
                "top_hard_skills": profile.get("top_hard_skills") or [],
            }
        with _session_store.connect() as conn:
            total = job_repo.total_active(conn)
            page = search(
                conn=conn,
                filters=SearchFilters(),
                profile=prof_for_search,
                cursor=None, limit=20, rank_pool=120,
            )
    except Exception:
        return None
    jobs_payload = [
        {
            "co":     j.company,
            "role":   j.title,
            "loc":    j.location or "",
            "score":  round(j.score * 100),
            "id":     j.id,
            "url":    j.url,
            "skills": ", ".join((j.requirements or [])[:4]),
            "status": "passed",
        }
        for j in page.jobs
    ]
    auto   = sum(1 for j in jobs_payload if j["score"] >= thr)
    manual = sum(1 for j in jobs_payload if j["score"] <  thr)
    return {
        "total":    total,
        "auto":     auto,
        "manual":   manual,
        "below":    0,
        "filtered": 0,
        "jobs":     jobs_payload,
        "synthetic": True,
    }


def _scored_summary_for_state(scored, passed, auto_jobs, manual_jobs,
                               profile) -> dict | None:
    """Prefer real Phase-3 scoring when present; fall back to the live index."""
    if scored:
        return {
            "total":    len(scored),
            "auto":     len(auto_jobs),
            "manual":   len(manual_jobs),
            "below":    sum(1 for j in scored if j.get("filter_status") == "below_threshold"),
            "filtered": sum(1 for j in scored if (j.get("filter_status") or "").startswith("filtered_")),
            "jobs": [
                {
                    "co":     j.get("company", ""),
                    "role":   j.get("title", ""),
                    "loc":    j.get("location", ""),
                    "score":  j.get("score", 0),
                    "id":     j.get("id") or f"{j.get('company', '')}|{j.get('title', '')}",
                    "url":    j.get("application_url", ""),
                    "skills": ", ".join(list(j.get("matching_skills") or [])[:4]),
                    "status": j.get("filter_status", ""),
                }
                for j in sorted(passed, key=lambda x: x.get("score", 0), reverse=True)[:20]
            ],
        }
    return _scored_summary_from_index(profile)


@app.get("/api/state")
def get_state(request: Request):

    # Dev mode now requires an authenticated user with the is_developer flag.
    is_dev = _is_dev_request(request)
    _raw_auth_cookie = request.cookies.get(_AUTH_COOKIE, "")
    auth_user = _auth_token_lookup(_raw_auth_cookie)

    # dev_simulating: the underlying user IS a developer but is currently viewing
    # the app as a customer via the force_customer_mode toggle. Lets the frontend
    # show an "Exit customer mode" pill so the dev isn't trapped without nav.
    dev_simulating = False
    if _S.get("force_customer_mode"):
        if auth_user and auth_user.get("id") and _user_store is not None:
            fresh = _user_store.get_user_by_id(auth_user["id"])
            if fresh and bool(fresh.get("is_developer")):
                dev_simulating = True
        if not dev_simulating and os.environ.get("LOCAL_DEV_BYPASS") == "1":
            host = getattr(request.client, "host", "") if request.client else ""
            if host in ("127.0.0.1", "::1", "localhost"):
                dev_simulating = True
    if _raw_auth_cookie and auth_user is None:
        # We have a cookie but no DB hit — usually means the auth_tokens row was
        # wiped (server restart with in-memory fallback, or session DB rebuilt).
        print(
            f"[api/state] auth cookie present but lookup returned None: "
            f"token={_raw_auth_cookie[:8]}… session={_S.session_id()} store={_session_store is not None}",
            file=sys.stderr,
        )

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
        "has_resume": bool(_S.get("resumes")),
        "resume_filename": _S.get("resume_filename"),
        "mode": _S.get("mode", "ollama"),
        "light_mode": bool(_S.get("light_mode")),
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
        "job_count": len(_S.get("jobs") or []) or _index_total_active(),
        "scored_summary": _scored_summary_for_state(scored, passed, auto, manual, profile),
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
        "dev_simulating": dev_simulating,
        "runtime": dict(_RUNTIME),

        # Plan tier (mirrors is_developer end-to-end). Free is the default for any
        # unauthenticated visitor or new account; only the manual Dev Ops flip (or
        # future Stripe webhook) sets it to 'pro'.
        "plan_tier": (auth_user or {}).get("plan_tier", "free"),
        "is_pro": (auth_user or {}).get("plan_tier") == "pro",

        # Don't synthesize a fake user from is_dev — uploads require a real auth_user,
        # so a synthesized one would let the frontend bypass login and then 401 on upload.
        "user": auth_user or _S.get("user"),
        "resumes": [_serialize_resume(r) for r in (_S.get("resumes") or [])],

        # UI state
        "liked_ids": list(_S.get("liked_ids") or []),
        "hidden_ids": list(_S.get("hidden_ids") or []),
        "dev_tweaks": _S.get("dev_tweaks") or {},
    }

@app.post("/api/reset")
def reset_state():
    # Preserve auth + provider/UI prefs so the user stays logged in and configured
    preserved = {
        k: _S.get(k) for k in (
            "user", "dev_tweaks", "mode", "api_key", "ollama_model", "light_mode",
            "force_customer_mode",
        )
    }

    state = _S.current()
    fresh = _default_state()
    state.clear()
    state.update(fresh)
    for k, v in preserved.items():
        if v is not None:
            state[k] = v

    # Wipe generated output files for this session (resumes, trackers, reports)
    try:
        out_dir = _session_output_dir()
        if out_dir.exists():
            for f in out_dir.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                    except Exception:
                        pass
    except Exception:
        pass
    return {"ok": True}


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def auth_login(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid request body"}, status_code=400)
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        return JSONResponse({"ok": False, "error": "Email and password are required"})
    if _user_store is None:
        return JSONResponse({"ok": False, "error": "Auth store unavailable"})
    try:
        user = _user_store.get_user_by_email(email)
        if not user or not verify_password(password, user.get("password_hash") or ""):
            return JSONResponse({"ok": False, "error": "Invalid email or password"})

        auth_user = {
            "id": user["id"],
            "email": user["email"],
            "is_developer": bool(user.get("is_developer")),
            "plan_tier": user.get("plan_tier") or "free",
        }
        app_session_id = _switch_to_user_session(user, auth_user)
        token = secrets.token_urlsafe(32)
        _auth_token_save(token, auth_user)
    except Exception as exc:
        import traceback as _tb
        _tb.print_exc()
        return JSONResponse({"ok": False, "error": f"Login failed: {exc}"}, status_code=500)
    resp = JSONResponse({"ok": True, "user": {"email": user["email"]}})
    resp.set_cookie(_AUTH_COOKIE, token, httponly=True, samesite="lax")
    resp.set_cookie(_STATE_COOKIE, app_session_id, httponly=True, samesite="lax")
    resp.headers[_SESSION_SWITCH_HEADER] = app_session_id
    return resp

@app.post("/api/auth/signup")
async def auth_signup(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid request body"}, status_code=400)
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or len(password) < 6:
        return JSONResponse({"ok": False, "error": "Valid email and password (≥6 chars) required"})
    if _user_store is None:
        return JSONResponse({"ok": False, "error": "Auth store unavailable"})
    try:
        if _user_store.get_user_by_email(email):
            return JSONResponse({"ok": False, "error": "An account with this email already exists"})
        pw_hash = hash_password(password)
        user_id = _user_store.create_user(email, pw_hash)

        auth_user = {"id": user_id, "email": email, "is_developer": False, "plan_tier": "free"}
        app_session_id = _start_session({"user": auth_user}, user_id=user_id)
        token = secrets.token_urlsafe(32)
        _auth_token_save(token, auth_user)
    except Exception as exc:
        import traceback as _tb
        _tb.print_exc()
        return JSONResponse({"ok": False, "error": f"Signup failed: {exc}"}, status_code=500)
    resp = JSONResponse({"ok": True, "user": {"email": email}})
    resp.set_cookie(_AUTH_COOKIE, token, httponly=True, samesite="lax")
    resp.set_cookie(_STATE_COOKIE, app_session_id, httponly=True, samesite="lax")
    resp.headers[_SESSION_SWITCH_HEADER] = app_session_id
    return resp

@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = request.cookies.get(_AUTH_COOKIE, "")
    if token:
        _auth_token_delete(token)
    app_session_id = _start_session()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_AUTH_COOKIE)
    resp.set_cookie(_STATE_COOKIE, app_session_id, httponly=True, samesite="lax")
    resp.headers[_SESSION_SWITCH_HEADER] = app_session_id
    return resp

@app.get("/api/auth/google")
def auth_google(request: Request):
    try:
        redirect_uri = str(request.url_for("auth_google_callback"))
        url, state = get_google_auth_url(redirect_uri)
        _S["google_oauth_state"] = state
        print(
            f"[google oauth init] session={_S.session_id()} "
            f"state_set={state[:8] if state else '<empty>'}… redirect_uri={redirect_uri}",
            file=sys.stderr,
        )
        return {"ok": True, "url": url}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.get("/api/auth/google/callback")
def auth_google_callback(request: Request, code: str = "", state: str = ""):
    from fastapi.responses import RedirectResponse
    if not code:
        return RedirectResponse("/app#auth")
    expected_state = _S.get("google_oauth_state") or ""
    # Reject if we have no state on file or it doesn't match exactly. The
    # previous version bypassed verification when expected_state was empty,
    # which let an attacker complete the flow with a forged callback for
    # a session that never initiated OAuth.
    if not expected_state or not secrets.compare_digest(state or "", expected_state):
        # Allow the dev `dummy_state` round-trip used when GOOGLE_CLIENT_ID
        # is unset, but only when the code matches the dummy too.
        if not (code == "dummy_code" and state == "dummy_state"):
            print(
                f"[google oauth callback] state check failed: "
                f"expected={'<empty>' if not expected_state else '<set>'} "
                f"received={'<empty>' if not state else '<set>'} "
                f"session={_S.session_id()}",
                file=sys.stderr,
            )
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
            auth_user = {
                "id": user["id"],
                "email": user["email"],
                "name": name,
                "is_developer": bool(user.get("is_developer")),
                "plan_tier": user.get("plan_tier") or "free",
            }
            app_session_id = _switch_to_user_session(user, auth_user)
        else:
            auth_user = {"email": email, "name": name, "is_developer": False, "plan_tier": "free"}
            app_session_id = _start_session({"user": auth_user})

        token = secrets.token_urlsafe(32)
        _auth_token_save(token, auth_user)
        # Verify it actually persisted; this is the most common silent failure.
        _verify = _auth_token_lookup(token)
        _S.pop("google_oauth_state", None)
        print(
            f"[google oauth callback] success: "
            f"user_id={auth_user.get('id')} email={auth_user.get('email')} "
            f"token={token[:8]}… app_session={app_session_id} "
            f"token_lookup_ok={_verify is not None} store={_session_store is not None}",
            file=sys.stderr,
        )
        resp = RedirectResponse("/app")
        resp.set_cookie(_AUTH_COOKIE, token, httponly=True, samesite="lax")
        resp.set_cookie(_STATE_COOKIE, app_session_id, httponly=True, samesite="lax")
        resp.headers[_SESSION_SWITCH_HEADER] = app_session_id
        return resp
    except Exception as exc:
        # Log to the server console so "Continue with Google does nothing" is at least
        # diagnosable from the terminal output. Common causes: redirect_uri_mismatch in
        # Google Cloud Console; missing google-auth-oauthlib package; expired/wrong code.
        print(f"[google oauth callback] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
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
    resume_id = body.get("resume_id", "")
    target = _get_resume_by_id(resume_id) if resume_id else _get_primary_resume()
    if not target:
        raise HTTPException(400, "No resume loaded")
    preferred = body.get("preferred_titles") or None
    if preferred:
        # Persist the user's preferences so _run_extraction_bg picks them up.
        _S["job_titles"] = ", ".join(str(t).strip() for t in preferred if str(t).strip())
    force = bool(body.get("force", True))

    # Clear stale per-resume profile so the UI shows "Analyzing" immediately.
    target["profile"] = None
    target.pop("extract_error", None)
    if target.get("primary"):
        _S["profile"] = None
        _S["done"].discard(1)

    # Mark extracting in the request thread so the middleware-saved state
    # already reflects it before the worker thread is even scheduled.
    _S["extracting_ids"].add(target["id"])

    threading.Thread(
        target=_run_extraction_bg,
        args=(target, _S.current(), _S.session_id()),
        kwargs={"force": force},
        daemon=True,
    ).start()
    return {"ok": True, "extracting": True, "resume_id": target["id"]}


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
    # Sync scalar fields and global profile from the new primary. If the new
    # primary already has an extracted profile, _sync_primary_scalars copies
    # name/email/education/projects/etc. into _S["profile"] so the Profile
    # page reflects the new resume immediately.
    _sync_primary_scalars(target)

    # Phase results downstream of 1 (jobs, scored, applications, tracker)
    # are tied to the old profile, so clear them. _sync_primary_scalars
    # already handled phase 1's done flag based on whether the new primary
    # has a profile — do NOT overwrite that here.
    for k in ("jobs", "scored", "applications", "tracker_path"):
        _S[k] = None
    _S["tailored_map"] = {}
    for p in range(2, 8):
        _S["done"].discard(p)
        _S["error"].pop(p, None)
        _S["elapsed"].pop(p, None)

    # If the new primary has no profile yet, auto-trigger extraction so the
    # Profile page populates without the user having to click "Extract".
    needs_extraction = (
        not target.get("profile")
        and target["id"] not in (_S.get("extracting_ids") or set())
    )
    if needs_extraction:
        _S["extracting_ids"].add(target["id"])
        threading.Thread(
            target=_run_extraction_bg,
            args=(target, _S.current(), _S.session_id()),
            daemon=True,
        ).start()

    return {"ok": True, "extracting": needs_extraction or not target.get("profile")}

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


# ── Ask Atlas — per-job chat advisor ─────────────────────────────────────────


def _find_job_by_id(job_id: str) -> dict | None:
    """Look up a job by id across the session's known job lists.

    Frontend ids are computed as ``f"{company}|{title}"`` when the underlying
    record has no explicit id, so we match against both forms.
    """
    if not job_id:
        return None
    pools = []
    for k in ("scored", "jobs", "applications"):
        v = _S.get(k)
        if isinstance(v, list):
            pools.append(v)
    for pool in pools:
        for j in pool:
            jid = j.get("id") or f"{j.get('company', '')}|{j.get('title', '')}"
            if jid == job_id:
                return j
    # Also try the persistent job_postings store (post-ingest worker path)
    try:
        from pipeline import job_repo
        rec = job_repo.get_job(job_id) if hasattr(job_repo, "get_job") else None
        if rec:
            return rec
    except Exception:
        pass
    return None


def _build_atlas_system_prompt(job: dict, profile: dict | None) -> str:
    """Turn the LLM into a 'master of this job' — every signal we have, in one block.

    The system prompt is intentionally dense: full job posting + the user's profile
    so the model can answer fit/gap/strategy questions without re-asking for context.
    """
    def _fmt_list(label: str, value) -> str:
        if not value:
            return ""
        if isinstance(value, (list, tuple, set)):
            items = ", ".join(str(v).strip() for v in value if str(v).strip())
        else:
            items = str(value).strip()
        return f"{label}: {items}\n" if items else ""

    co = (job.get("company") or job.get("co") or "").strip() or "the company"
    title = (job.get("title") or job.get("role") or "").strip() or "the role"

    job_block_parts = [
        f"COMPANY: {co}",
        f"ROLE: {title}",
    ]
    for label, key in [
        ("LOCATION", "location"), ("LOCATION", "loc"),
        ("REMOTE", "remote"),
        ("EXPERIENCE LEVEL", "experience_level"), ("EXPERIENCE LEVEL", "exp"),
        ("EDUCATION REQUIRED", "education_required"),
        ("CITIZENSHIP REQUIRED", "citizenship_required"),
        ("SALARY RANGE", "salary_range"), ("SALARY RANGE", "salary"),
        ("INDUSTRY", "industry"),
        ("PLATFORM", "platform"),
        ("POSTED", "posted_date"),
        ("APPLICATION URL", "application_url"), ("APPLICATION URL", "url"),
    ]:
        v = job.get(key)
        if v in (None, "", False) or any(label.split()[0].lower() in p.lower() for p in job_block_parts):
            continue
        job_block_parts.append(f"{label}: {v}")

    skills_str = job.get("skills")
    if isinstance(skills_str, (list, set, tuple)):
        skills_str = ", ".join(str(s) for s in skills_str)
    if skills_str:
        job_block_parts.append(f"REQUIRED/MENTIONED SKILLS: {skills_str}")

    score = job.get("score")
    if score is not None:
        job_block_parts.append(f"ATLAS MATCH SCORE: {score}/100")

    matching = job.get("matching_skills")
    missing = job.get("missing_skills") or job.get("ats_keywords_missing")
    if matching:
        job_block_parts.append(_fmt_list("USER SKILLS THAT MATCH", matching).rstrip())
    if missing:
        job_block_parts.append(_fmt_list("GAPS (keywords this job wants but the resume lacks)", missing).rstrip())

    description = (
        job.get("description")
        or job.get("full_description")
        or job.get("requirements")
        or ""
    )
    if description:
        job_block_parts.append("\n--- FULL POSTING ---\n" + str(description).strip())

    job_block = "\n".join(p for p in job_block_parts if p)

    profile_lines: list[str] = []
    if profile:
        for label, key in [
            ("Name", "name"),
            ("Target titles", "target_titles"),
            ("Top hard skills", "top_hard_skills"),
            ("Top soft skills", "top_soft_skills"),
            ("Education", "education"),
            ("Years of experience", "years_experience"),
            ("Location preference", "location"),
            ("Resume gaps", "resume_gaps"),
        ]:
            line = _fmt_list(label, profile.get(key))
            if line:
                profile_lines.append(line.rstrip())
    profile_block = "\n".join(profile_lines) if profile_lines else "(no profile loaded)"

    return (
        "You are Atlas, the user's personal job-search advisor for the SPECIFIC role below. "
        "You have read the entire posting and the user's resume profile. Be the world's expert on "
        "THIS role at THIS company — interview rituals, what bullets to emphasize, what gaps to call "
        "out, salary framing, decision-makers, how the role typically progresses, and what the user "
        "should ask their recruiter. Be concrete and direct: cite specific lines from the posting, "
        "name specific projects from the user's profile when relevant, and avoid generic résumé advice "
        "that could apply to any job. If the posting is missing information you'd need to answer well, "
        "say so plainly. Keep responses tight — 2 to 5 short paragraphs, no bullet-point spam unless "
        "the user explicitly asks for a list. Use plain text. Never invent facts about the company or "
        "the user that aren't in the data below.\n\n"
        "==================== JOB POSTING ====================\n"
        f"{job_block}\n"
        "================== USER'S PROFILE ===================\n"
        f"{profile_block}\n"
        "====================================================="
    )


@app.post("/api/jobs/ask")
async def jobs_ask(req: Request):
    """Per-job chat advisor — 'Ask Atlas' button on the JobCard.

    Body: { job_id: str, message: str, history?: [{role, content}, ...] }
    Returns: { reply: str, mode: str, job: { co, role } }
    """
    body = await req.json()
    job_id = (body.get("job_id") or "").strip()
    message = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not message:
        raise HTTPException(400, "Empty message")

    job = _find_job_by_id(job_id)
    if not job:
        raise HTTPException(404, f"Job not found in this session: {job_id!r}")

    profile = _S.get("profile") or None
    system = _build_atlas_system_prompt(job, profile)

    # Keep the rolling history bounded so we don't blow context on long threads.
    trimmed: list[dict] = []
    for turn in history[-12:]:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            trimmed.append({"role": role, "content": content})
    trimmed.append({"role": "user", "content": message})

    mode = _S.get("mode", "demo")
    try:
        provider = _make_provider()
        reply = provider.chat(system, trimmed, max_tokens=1024)
    except NotImplementedError:
        raise HTTPException(501, f"Chat is not supported by the current provider ({mode}).")
    except Exception as exc:
        # Surface the underlying error message — usually a missing key, network, or model issue.
        raise HTTPException(502, f"Chat request failed: {type(exc).__name__}: {exc}")

    return {
        "reply": reply or "(empty response)",
        "mode": mode,
        "job": {
            "co": job.get("company") or job.get("co") or "",
            "role": job.get("title") or job.get("role") or "",
        },
    }


# ── New job feed (R2) ────────────────────────────────────────────────────────
# Reads straight from the persistent ``job_postings`` table populated by the
# background ingestion worker (pipeline/ingest.py). Phase 2's SSE pipeline is
# still wired up for diagnostic / "force re-ingest" use, but this endpoint is
# the path the SPA hits on every JobsPage render and scroll.

def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(p.strip() for p in str(value).split(",") if p.strip())


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _profile_for_search() -> dict | None:
    pr = _S.get("profile") or None
    if pr:
        return {
            "target_titles": pr.get("target_titles") or [],
            "top_hard_skills": pr.get("top_hard_skills") or [],
        }
    # Fall back to bare config so we still rank by user-stated job_titles
    # before any LLM extraction has finished.
    titles = [t.strip() for t in (_S.get("job_titles") or "").split(",") if t.strip()]
    return {"target_titles": titles, "top_hard_skills": []} if titles else None


@app.get("/api/jobs/feed")
def jobs_feed(request: Request):
    """Cursor-paginated, profile-ranked feed read from the local DB.

    Query params (all optional):
      cursor      — opaque pagination token from a previous response
      limit       — page size (default 30, max 100)
      q           — free text query (FTS5 MATCH)
      exp         — comma-separated experience levels
      edu         — comma-separated education levels
      cit         — 'all' | 'exclude_required' | 'only_required'
      remote      — '1' to require remote_only
      days        — posted within last N days
      location    — substring match on the location field
      since_id    — return rows whose last_seen_at > the given id's
                    last_seen_at (used for the 25s polling tick).
    """
    if _session_store is None:
        return {"jobs": [], "next_cursor": None, "total_estimate": 0,
                "warning": "session store unavailable"}
    qs = request.query_params
    limit = max(1, min(100, int(qs.get("limit", 30) or 30)))

    # since_id branch — used by the SPA poll to prepend new rows.
    since_id = qs.get("since_id") or ""
    if since_id:
        from pipeline.job_search import newer_than
        with _session_store.connect() as conn:
            rows = newer_than(conn=conn, top_id=since_id, limit=limit)
        return {"jobs": [_dto_to_json(j) for j in rows], "next_cursor": None,
                "total_estimate": len(rows)}

    # Treat URL params as authoritative when present (even if empty),
    # so callers can override the per-session whitelist/blacklist that's
    # baked into _default_state. Only fall back to state when the param
    # is fully omitted from the query string.
    bl_qs = qs.get("blacklist")
    wl_qs = qs.get("whitelist")
    blacklist = _csv(bl_qs if bl_qs is not None else _S.get("blacklist") or "")
    whitelist = _csv(wl_qs if wl_qs is not None else _S.get("whitelist") or "")

    from pipeline.job_search import search, SearchFilters
    filters = SearchFilters(
        q=qs.get("q") or "",
        location=qs.get("location") or "",
        remote_only=_truthy(qs.get("remote")),
        experience_levels=_csv(qs.get("exp")),
        education_levels=_csv(qs.get("edu")),
        citizenship_filter=qs.get("cit") or "all",
        posted_within_days=int(qs["days"]) if qs.get("days") else None,
        blacklist=blacklist,
        whitelist=whitelist,
        include_unknown_education=_truthy(qs.get("include_unknown") or "1"),
    )
    with _session_store.connect() as conn:
        page = search(conn=conn, filters=filters, profile=_profile_for_search(),
                      cursor=qs.get("cursor") or None, limit=limit)
    # Drop hidden ids client-side intent applied here too.
    hidden = _S.get("hidden_ids") or set()
    visible = [j for j in page.jobs if j.id not in hidden]
    return {
        "jobs": [_dto_to_json(j) for j in visible],
        "next_cursor": page.next_cursor,
        "total_estimate": page.total_estimate,
    }


def _dto_to_json(j) -> dict:
    """Adapt a JobDTO to the wire shape the SPA already speaks (matches the
    keys used by ``state.scored_summary.jobs``: ``id, co, role, loc, score,
    skills, url, status``). Extra fields are added for richer cards."""
    return {
        "id":      j.id,
        "co":      j.company,
        "role":    j.title,
        "loc":     j.location,
        "score":   round(j.score * 100),    # 0..100 for the existing UI bar
        "skills":  ", ".join(j.requirements[:6]),
        "url":     j.url,
        "remote":  j.remote,
        "salary":  j.salary_range,
        "exp":     j.experience_level,
        "edu":     j.education_required,
        "cit":     j.citizenship_required,
        "posted":  j.posted_at,
        "source":  j.source,
        "status":  "passed",
    }


@app.get("/api/jobs/source-status")
def jobs_source_status(request: Request):
    """Per-source health snapshot for the dev page."""
    if _session_store is None:
        return {"sources": [], "total_active": 0}
    from pipeline import job_repo
    with _session_store.connect() as conn:
        rows = job_repo.latest_source_runs(conn)
        # row counts per source
        counts = dict(conn.execute(
            "SELECT source, COUNT(*) FROM job_postings WHERE deleted = 0 GROUP BY source"
        ).fetchall())
        active = job_repo.total_active(conn)
    for r in rows:
        r["active_count"] = int(counts.get(r["source"], 0))
    return {"sources": rows, "total_active": int(active)}


@app.post("/api/jobs/source-status")
async def jobs_source_status_force(request: Request):
    """Force-tick one source (or all if body is empty). Restricted to dev."""
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access required")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    name = (body or {}).get("source") or None
    from pipeline import ingest as _job_ingest
    results = _job_ingest.force_run(name)
    return {"ok": True, "results": results}


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
    """Authoritative dev check.

    Truthy iff the caller is authenticated AND that user has the
    is_developer flag set in the users table. The legacy IP-based
    fallback is gated behind LOCAL_DEV_BYPASS=1 so production
    deployments behind a proxy can never grant dev access from
    request.client.host alone.
    """
    if _S.get("force_customer_mode"):
        return False
    return _is_underlying_dev_request(request)


def _is_underlying_dev_request(request: Request) -> bool:
    """Same as _is_dev_request but ignores force_customer_mode.

    Use for endpoints that let a developer escape simulation mode or
    edit server-wide runtime knobs while still in customer-simulation
    view. Without this, a dev who flips `Test as Customer` would be
    locked out of every /api/dev/* endpoint and have no way back.
    """
    auth_user = _auth_token_lookup(request.cookies.get(_AUTH_COOKIE, ""))
    if auth_user and auth_user.get("id") and _user_store is not None:
        fresh = _user_store.get_user_by_id(auth_user["id"])
        if fresh and bool(fresh.get("is_developer")):
            return True
    if os.environ.get("LOCAL_DEV_BYPASS") == "1":
        host = getattr(request.client, "host", "") if request.client else ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return True
    return False


def _list_tracked_sessions() -> list[dict]:
    if _session_store is not None:
        return _session_store.list_sessions(limit=500)

    sessions = []
    for session_id, state in _memory_sessions.items():
        profile = state.get("profile") or {}
        scored = state.get("scored") or []
        applications = state.get("applications") or []
        feedback = state.get("feedback") or []
        sessions.append(
            {
                "id": session_id,
                "created_at": None,
                "updated_at": None,
                "name": profile.get("name") or "Unprofiled user",
                "email": profile.get("email") or "",
                "has_resume": bool(state.get("resume_text")),
                "resume_filename": state.get("resume_filename") or "",
                "mode": state.get("mode", "demo"),
                "done": sorted(state.get("done") or []),
                "errors": state.get("error") or {},
                "job_count": len(state.get("jobs") or []),
                "scored_count": len(scored),
                "application_count": len(applications),
                "applied_count": sum(1 for app in applications if app.get("status") == "Applied"),
                "manual_count": sum(1 for app in applications if app.get("status") == "Manual Required"),
                "target": state.get("job_titles") or "",
                "location": state.get("location") or "",
                "feedback_count": len(feedback),
                "unread_feedback_count": sum(1 for f in feedback if not f.get("read")),
            }
        )
    return sessions

@app.get("/api/dev/overview")
def dev_overview(request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    import shutil, sys as _sys
    disk = shutil.disk_usage(OUTPUT_DIR)
    out_files = list(OUTPUT_DIR.glob("*")) if OUTPUT_DIR.exists() else []
    sessions = _list_tracked_sessions()
    all_apps = sum(session.get("application_count", 0) for session in sessions)
    all_applied = sum(session.get("applied_count", 0) for session in sessions)
    all_manual = sum(session.get("manual_count", 0) for session in sessions)
    all_errors = sum(1 for session in sessions for v in (session.get("errors") or {}).values() if v)
    return {
        "summary": {
            "users": len(sessions),
            "with_resume": sum(1 for session in sessions if session.get("has_resume")),
            "applications": all_apps,
            "applied": all_applied,
            "manual": all_manual,
            "errors": all_errors,
        },
        "status": {
            "app": "running",
            "python": _sys.version.split()[0],
            "output_files": len(out_files),
            "session_files": len(sessions),
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
    state = _load_session_state(session_id)
    detail = dict(state)
    detail["resume_text"] = (state.get("resume_text") or "")[:2000]
    detail["feedback"] = state.get("feedback") or []
    detail["done"] = sorted(state.get("done") or [])
    detail["liked_ids"] = sorted(state.get("liked_ids") or [])
    detail["hidden_ids"] = sorted(state.get("hidden_ids") or [])
    return detail

@app.post("/api/dev/session/{session_id}/reset")
def dev_session_reset(session_id: str, request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    if _session_store is None:
        _memory_sessions[session_id] = _default_state()
        return {"ok": True}
    _load_session_state(session_id)
    _session_store.reset_state(session_id)
    return {"ok": True}

@app.delete("/api/dev/session/{session_id}")
def dev_session_delete(session_id: str, request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    if _session_store is None:
        _memory_sessions.pop(session_id, None)
        return {"ok": True}
    _load_session_state(session_id)
    _session_store.delete_session(session_id)
    return {"ok": True}

@app.post("/api/dev/session/{session_id}/impersonate")
def dev_impersonate(session_id: str, request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    _load_session_state(session_id)
    response = JSONResponse({"ok": True})
    response.set_cookie(_DEV_IMPERSONATE_COOKIE, session_id, httponly=True, samesite="lax")
    return response

@app.post("/api/dev/session/stop-impersonating")
def dev_stop_impersonate(request: Request):
    response = JSONResponse({"ok": True})
    response.delete_cookie(_DEV_IMPERSONATE_COOKIE)
    return response

@app.post("/api/dev/session/{session_id}/feedback/read")
def dev_mark_feedback_read(session_id: str, request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    # When the dev is inspecting their own bound session, mutate _S.current()
    # directly so the middleware's end-of-request save reflects the change —
    # otherwise the unmutated snapshot from request-start overwrites our write.
    if _S.session_id() == session_id:
        target = _S.current()
    else:
        target = _load_session_state(session_id)
    for f in target.get("feedback") or []:
        f["read"] = True
    _save_bound_state(target, session_id)
    return {"ok": True}

@app.post("/api/dev/cli")
async def dev_cli(request: Request):
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    body = await request.json()
    command = body.get("command", "")
    import subprocess
    output_dir = str(OUTPUT_DIR)
    db_path = str(OUTPUT_DIR / "jobs_ai_sessions.sqlite3")

    def _recent_outputs() -> str:
        if not os.path.isdir(output_dir):
            return ""
        files = sorted(
            os.listdir(output_dir),
            key=lambda f: os.path.getmtime(os.path.join(output_dir, f)),
            reverse=True,
        )[:20]
        return "\n".join(files)

    def _session_db_info() -> str:
        if not os.path.exists(db_path):
            return "No DB yet"
        return f"Size: {os.path.getsize(db_path) / 1024:.1f} KB"

    if command == "recent_outputs":
        return {"output": _recent_outputs()[:4000]}
    if command == "session_db":
        return {"output": _session_db_info()[:4000]}

    safe_external = {
        "git_status": ["git", "status", "--short"],
        "pip_freeze": [sys.executable, "-m", "pip", "freeze"],
    }
    cmd = safe_external.get(command)
    if not cmd:
        return {"output": f"Unknown command: {command}"}
    try:
        out = subprocess.check_output(
            cmd, text=True, stderr=subprocess.STDOUT, timeout=10
        )
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


@app.get("/api/dev/runtime")
def dev_runtime_get(request: Request):
    # Use _is_underlying_dev_request so a dev currently simulating-as-customer
    # can still inspect/flip server runtime flags.
    if not _is_underlying_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    return {
        "runtime": dict(_RUNTIME),
        "env": {
            "anthropic_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "local_dev_bypass": os.environ.get("LOCAL_DEV_BYPASS") == "1",
            "ollama_url": os.environ.get("OLLAMA_URL", "http://localhost:11434"),
            "smtp_configured": all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS")),
        },
        "session": {
            "force_customer_mode": bool(_S.get("force_customer_mode")),
        },
    }


@app.post("/api/dev/runtime")
async def dev_runtime_set(request: Request):
    if not _is_underlying_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    body = await request.json()
    for k in ("maintenance", "verbose_logs"):
        if k in body:
            _RUNTIME[k] = bool(body[k])
    return {"ok": True, "runtime": dict(_RUNTIME)}


@app.post("/api/dev/reload-env")
async def dev_reload_env(request: Request):
    """Re-read .env from disk and apply any new values to os.environ.

    Useful for picking up a new ANTHROPIC_API_KEY without restarting the
    server. Only the dev who owns the process should be able to trigger this.
    """
    if not _is_underlying_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    loaded = []
    try:
        from dotenv import load_dotenv
        here = Path(__file__).resolve().parent
        for candidate in (here / ".env", here.parent / ".env"):
            if candidate.exists():
                load_dotenv(candidate, override=True)
                loaded.append(str(candidate))
    except ImportError:
        return JSONResponse({"ok": False, "error": "python-dotenv not installed"}, status_code=500)
    return {
        "ok": True,
        "loaded": loaded,
        "anthropic_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "smtp_configured": all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS")),
    }


@app.get("/api/dev/users")
def dev_users_list(request: Request):
    """List all user accounts with plan tier — powers the Dev Ops Users panel."""
    if not _is_underlying_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    if _user_store is None:
        return {"users": []}
    return {"users": _user_store.list_users(limit=500)}


@app.post("/api/dev/users/{user_id}/plan")
async def dev_users_set_plan(user_id: str, request: Request):
    """Manually grant or revoke Pro for a user. Stub for the eventual Stripe webhook."""
    if not _is_underlying_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    body = await request.json()
    tier = (body.get("tier") or "").strip()
    if tier not in ("free", "pro"):
        raise HTTPException(400, f"Invalid tier: {tier!r} (expected 'free' or 'pro')")
    if _user_store is None:
        raise HTTPException(503, "User store unavailable")
    _user_store.set_user_plan_tier(user_id, tier)
    # Refresh any cached auth tokens for this user so plan_tier propagates
    # immediately on the user's next /api/state poll without forcing re-login.
    if hasattr(_user_store, "_connect"):
        try:
            with _user_store._connect() as conn:
                rows = conn.execute(
                    "SELECT token, user_json FROM auth_tokens WHERE user_id = ?", (user_id,)
                ).fetchall()
                for row in rows:
                    payload = json.loads(row[1] or "{}")
                    payload["plan_tier"] = tier
                    conn.execute(
                        "UPDATE auth_tokens SET user_json = ? WHERE token = ?",
                        (json.dumps(payload), row[0]),
                    )
        except Exception:
            pass
    return {"ok": True, "user_id": user_id, "plan_tier": tier}


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
            ollama_model=_ollama_model_for_insights(),
        )
        _S["profile"] = result
        # Persist profile on the primary resume record so it survives primary switches
        pr = _get_primary_resume()
        if pr:
            pr["profile"] = result
            pr["updated_at"] = datetime.now().isoformat()
        return result

    return StreamingResponse(
        _run_phase_sse(1, _fn, _S.current(), _S.session_id()),
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
        _run_phase_sse(2, _fn, _S.current(), _S.session_id()),
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
        _run_phase_sse(3, _fn, _S.current(), _S.session_id()),
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
        _run_phase_sse(4, _fn, _S.current(), _S.session_id()),
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
        _run_phase_sse(5, _fn, _S.current(), _S.session_id()),
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
        _run_phase_sse(6, _fn, _S.current(), _S.session_id()),
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
        _run_phase_sse(7, _fn, _S.current(), _S.session_id()),
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
        proc = None
        try:
            try:
                proc = subprocess.Popen(
                    ["ollama", "pull", model],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
            except FileNotFoundError:
                yield _sse({"type": "error", "message": "ollama not found on PATH — install from ollama.com"})
                return
            try:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        yield _sse({"type": "log", "line": line})
            except GeneratorExit:
                # Client disconnected — terminate the pull instead of leaking it.
                raise
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                yield _sse({"type": "error", "message": "ollama pull timed out after streaming finished"})
                return
            if proc.returncode == 0:
                yield _sse({"type": "done", "model": model})
            else:
                yield _sse({
                    "type": "error",
                    "message": f"ollama pull exited with code {proc.returncode}",
                })
        except GeneratorExit:
            raise
        except Exception as exc:
            yield _sse({"type": "error", "message": str(exc)})
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except Exception:
                    pass
            if proc is not None and proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

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
