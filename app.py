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
import re
import secrets
import sys
import threading
import time
import uuid
import contextvars
from datetime import datetime
from pathlib import Path
from collections.abc import MutableMapping

# Load .env if python-dotenv is available. Must run before any os.environ.get("…")
# read. find_dotenv() walks up from this file's directory, so it picks up `.env`
# whether the user dropped it next to app.py OR in the repo root one level up.
try:
    from dotenv import load_dotenv, find_dotenv
    _env_file = find_dotenv(filename=".env", usecwd=False)
    if _env_file:
        load_dotenv(_env_file, override=False)
except ImportError:
    pass

# Force UTF-8 stdout/stderr so Rich emoji don't crash on Windows cp1252.
# Skip when stdout is already utf-8 — re-wrapping pytest's capture file
# breaks its session-end teardown (mirrors the same guard in pipeline/config.py).
if hasattr(sys.stdout, "buffer") and "utf" not in (sys.stdout.encoding or "").lower():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer") and "utf" not in (sys.stderr.encoding or "").lower():
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Server log mirror: tee stdout/stderr (and Python `logging`) into a
# bounded ring so the Dev Ops page can echo the terminal without
# scraping process pipes from outside.  Writes still pass through
# to the originals, so the real terminal keeps its native output.
import collections as _collections
import queue as _queue
import threading as _threading
import time as _logmirror_time
import logging as _logging

_LOG_RING_MAX = 3000
_LOG_RING_LOCK = _threading.Lock()
_LOG_RING = _collections.deque(maxlen=_LOG_RING_MAX)
_LOG_SEQ = 0
_LOG_SUBSCRIBERS = []

def _log_ring_push(stream, line):
    """Append one line to the ring and fan-out to live SSE subscribers."""
    line = line.rstrip("\r\n")
    if not line:
        return
    global _LOG_SEQ
    with _LOG_RING_LOCK:
        _LOG_SEQ += 1
        rec = {
            "seq":    _LOG_SEQ,
            "ts":     _logmirror_time.time(),
            "stream": stream,
            "line":   line,
        }
        _LOG_RING.append(rec)
        for q in list(_LOG_SUBSCRIBERS):
            try:
                q.put_nowait(rec)
            except Exception:
                pass

class _StreamTee:
    """File-like wrapper mirroring writes to original AND the ring."""
    def __init__(self, original, stream_name):
        self._orig = original
        self._name = stream_name
        self._buf  = ""
        self._lock = _threading.Lock()
    def write(self, data):
        if data:
            try: self._orig.write(data)
            except Exception: pass
            with self._lock:
                self._buf += data
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    _log_ring_push(self._name, line)
        return len(data) if data else 0
    def flush(self):
        try: self._orig.flush()
        except Exception: pass
    def isatty(self):
        try: return bool(self._orig.isatty())
        except Exception: return False
    def fileno(self):
        return self._orig.fileno()
    def __getattr__(self, name):
        return getattr(self._orig, name)

# Skip the tee under pytest — it captures stdout itself and re-wrapping
# breaks its session-end teardown.
if "pytest" not in (sys.modules or {}) and not getattr(sys.stdout, "_log_tee_installed", False):
    sys.stdout = _StreamTee(sys.stdout, "stdout")
    sys.stderr = _StreamTee(sys.stderr, "stderr")
    setattr(sys.stdout, "_log_tee_installed", True)

# Mirror Python `logging` into the same ring so uvicorn access logs
# (which go through the `uvicorn` / `uvicorn.access` loggers, NOT plain
# print) also surface in the Dev Ops panel.
class _RingLogHandler(_logging.Handler):
    def emit(self, record):
        try: msg = self.format(record)
        except Exception: msg = record.getMessage()
        _log_ring_push("logger:" + record.name, msg)

_RING_LOG_HANDLER = _RingLogHandler(level=_logging.DEBUG)
_RING_LOG_HANDLER.setFormatter(_logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
))
_logging.getLogger().addHandler(_RING_LOG_HANDLER)
for _ln in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _lg = _logging.getLogger(_ln)
    if _lg.level == _logging.NOTSET or _lg.level > _logging.INFO:
        _lg.setLevel(_logging.INFO)

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, HTMLResponse

from pipeline.config import OUTPUT_DIR, RESOURCES_DIR, DATA_DIR, DB_PATH

# Auth helpers — imported lazily so missing bcrypt doesn't crash the server
try:
    from auth_utils import (
        get_google_auth_url,
        hash_password,
        verify_google_token,
        verify_password,
    )
except ImportError as _exc:
    _AUTH_IMPORT_ERROR = f"auth_utils import failed ({type(_exc).__name__}: {_exc}). "
    _AUTH_PW_HINT = (
        _AUTH_IMPORT_ERROR
        + "Password sign-in needs `bcrypt`. Run `pip install 'bcrypt>=4.1.0'` "
          "in the Python interpreter that runs app.py, then restart the server."
    )
    _AUTH_GOOGLE_HINT = (
        _AUTH_IMPORT_ERROR
        + "Google OAuth needs `google-auth-oauthlib`. Run "
          "`pip install 'google-auth-oauthlib>=1.2.0'` in the Python interpreter "
          "that runs app.py, then restart the server."
    )
    def hash_password(_pw):
        raise RuntimeError(_AUTH_PW_HINT)
    def verify_password(_pw, _h):
        raise RuntimeError(_AUTH_PW_HINT)
    def get_google_auth_url(_redirect_uri):
        raise RuntimeError(_AUTH_GOOGLE_HINT)
    def verify_google_token(_code, _redirect_uri, _state):
        raise RuntimeError(_AUTH_GOOGLE_HINT)

from session_store import SQLiteSessionStore
from pipeline import stripe_billing as _stripe_billing
from pipeline.phases import (
    phase1_ingest_resume,
    phase2_discover_jobs,
    phase3_score_jobs,
    phase4_tailor_resume,
    _ats_score,
    _load_existing_applications,
    phase6_update_tracker,
    phase7_run_report,
)
from pipeline.resume import (
    _build_demo_resume, _read_resume, _save_tailored_resume,
    _render_resume_pdf_reportlab,
)
from pipeline.helpers import EDUCATION_RANK as _EDU_RANK

app = FastAPI(title="Jobs AI")
_DEV_IMPERSONATE_COOKIE = "dev_impersonate_id"
_AUTH_COOKIE = "jobs_ai_auth"
_STATE_COOKIE = "jobs_ai_session"
_SESSION_SWITCH_HEADER = "x-jobs-ai-session-switch"
# Fallback cache used only when the SQLite store is unavailable.
_AUTH_SESSIONS_FALLBACK: dict[str, dict] = {}


# Set to "1" / "true" / "yes" in production to mark auth cookies Secure so
# browsers reject them over plain HTTP. Leave unset in local dev (HTTP).
_COOKIE_SECURE = os.environ.get("PRODUCTION", "").lower() in ("1", "true", "yes")

_EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _is_valid_email(email: str) -> bool:
    return bool(email) and bool(_EMAIL_RE.match(email))


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
    # Same no-cache policy as `/app` — landing.html is the entry point for
    # marketing visitors AND existing users who navigate to the bare host;
    # without no-cache headers a single bad CSS deploy can sit cached on
    # someone's browser indefinitely (this is exactly what hid the
    # broken-scrubber regression for hours after f74ca22 introduced it).
    return FileResponse("frontend/landing.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma":        "no-cache",
        "Expires":       "0",
    })

@app.get("/app")
def dashboard():
    # Cache-bust the in-browser-Babel JSX import. Without a versioned URL,
    # browsers will hold onto a stale `app.jsx` across server restarts even
    # when the response sets no-cache, because their previously-cached
    # entry was permissive. Stamping the script tag with the file mtime
    # guarantees the browser sees a fresh URL the moment the JSX changes
    # and is forced to refetch.
    index_path = Path("frontend/index.html")
    jsx_path   = Path("frontend/app.jsx")
    html = index_path.read_text(encoding="utf-8")
    if jsx_path.exists():
        v = int(jsx_path.stat().st_mtime)
        html = html.replace('/frontend/app.jsx"', f'/frontend/app.jsx?v={v}"')
    return HTMLResponse(html, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma":        "no-cache",
        "Expires":       "0",
    })

@app.get("/frontend/{filepath:path}")
def frontend_static(filepath: str):
    # `:path` lets one-segment names (`app.jsx`) AND nested ones
    # (`landing/demo-run.json`) both resolve. Sandbox the result back into
    # the frontend/ tree to block any `..` traversal attempt.
    base = Path("frontend").resolve()
    p = (base / filepath).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        raise HTTPException(404, "Not found")
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "Not found")
    # No build step — `app.jsx` is transpiled by in-browser Babel on every
    # load. Without `no-cache` headers browsers serve a stale copy across
    # restarts, so a developer's edit can sit invisible behind the cache
    # for hours. Force revalidation on every request.
    return FileResponse(p, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma":        "no-cache",
        "Expires":       "0",
    })

_OUTPUT_FILE_BLOCKED_SUFFIXES = {
    # Anything that looks like a database, key store, or raw config — these
    # never belong to a user-facing artifact and must never be served, even
    # if a future bug accidentally drops one inside OUTPUT_DIR.
    ".sqlite", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm",
    ".db", ".db-wal", ".db-shm",
    ".env", ".pem", ".key", ".crt", ".p12", ".pfx",
    ".json", ".yaml", ".yml",
}

# User-facing artifact extensions the static-file route is *allowed* to
# return. Everything else 404s. Keep this short and intentional.
_OUTPUT_FILE_ALLOWED_SUFFIXES = {
    ".pdf", ".tex", ".docx", ".txt", ".md", ".log",
    ".xlsx", ".xls", ".csv",
    ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp",
    ".html", ".htm",
}


@app.get("/output/{path:path}")
def serve_output_file(path: str, request: Request):
    root = OUTPUT_DIR.resolve()
    p = (root / path).resolve()
    # Reject anything that resolves outside OUTPUT_DIR — the standard
    # path-traversal guard.
    if root != p and root not in p.parents:
        raise HTTPException(404, "Not found")

    # Reject by suffix BEFORE we check existence so an attacker can't probe
    # which sensitive files are present. Note .sqlite3-wal etc. are checked
    # via the trailing-component string because Path.suffix only catches
    # the final segment after the last dot.
    name_lower = p.name.lower()
    if any(name_lower.endswith(suf) for suf in _OUTPUT_FILE_BLOCKED_SUFFIXES):
        raise HTTPException(404, "Not found")
    if p.suffix.lower() not in _OUTPUT_FILE_ALLOWED_SUFFIXES:
        raise HTTPException(404, "Not found")

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

# ── Server-side Ollama config ────────────────────────────────────────────────
# OLLAMA_URL: env-driven so deployments can point at a different host (e.g. a
#   beefier Tailnet machine). On the production RPi this resolves to the RPi's
#   own Ollama daemon, NOT the visiting user's laptop.
# LOCAL_OLLAMA_MODEL / CLOUD_OLLAMA_MODEL: the canonical model names for
#   the Free vs Pro tiers. Free users default to the local one (small,
#   fast, runs on the Pi). Pro upgrades unlock the cloud one (proxied to
#   Ollama Turbo). Both must be `ollama pull`-ed on the daemon — the boot
#   auto-pull handles the local one for free.
# DEFAULT_OLLAMA_MODEL: which model new sessions get + which model the
#   boot-time auto-pull ensures is on disk. Defaults to the cloud tier
#   because we're in testing-phase mode where every user is upgraded to
#   Pro (see pipeline/migrations.py). Override via env var to pin to
#   the local model on dev laptops without Ollama Turbo credentials.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
# Testing phase: every user is Pro (see pipeline/migrations.py), so the
# "local" model alias resolves to the cloud one too. The downgrade-snap
# migration in _load_session_state therefore has no observable effect
# (LOCAL == CLOUD). When the testing phase ends, point LOCAL back at a
# small open-weight Ollama model so the Free tier has a real fallback.
LOCAL_OLLAMA_MODEL = "gemma4:31b-cloud"
CLOUD_OLLAMA_MODEL = "gemma4:31b-cloud"
DEFAULT_OLLAMA_MODEL = os.environ.get("DEFAULT_OLLAMA_MODEL", CLOUD_OLLAMA_MODEL)


# ── Session state ─────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
    # LLM backend
    "mode": "ollama",
    "ollama_model": DEFAULT_OLLAMA_MODEL,
    # Search / apply settings — every field that depends on who the user is
    # starts EMPTY. They get populated from the resume after Phase 1 runs;
    # see `_apply_profile_search_prefs`. Hardcoding "Engineer" / "United
    # States" / "bachelors" lied to non-engineering / non-US / non-undergrad
    # users on first paint, so we no longer ship those.
    "threshold": 75,
    "job_titles": "",
    "location": "",
    "max_apps": 10,
    "max_scrape_jobs": 50,
    "days_old": 30,
    "cover_letter": False,
    "blacklist": "",
    "whitelist": "",
    # Filters — empty until resume is parsed. citizenship defaults to 'all'
    # because that's the broadest, least-presumptuous filter; the previous
    # 'exclude_required' silently dropped roles that may have been fine.
    "experience_levels": [],
    "education_filter": [],
    "include_unknown_education": True,
    "include_unknown_experience": True,
    "citizenship_filter": "all",
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
    # Phase 6 — ``tracker_path`` is preserved for back-compat with any session
    # row that pre-dates the in-page tracker, but new web runs never populate
    # it (the spreadsheet lives entirely in ``tracker_data``).
    "tracker_path": None,
    "tracker_data": None,
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
    # One-shot flag set by /api/pipeline/reset so the user's NEXT Phase 2 run
    # forces a synchronous ingestion tick (`force_live=True`) before searching.
    # Without this, "Reset run" would just clear the cached jobs and re-search
    # the same persistent index with the same query — returning the same top-N
    # rows. Consumed (and cleared) by /api/phase/2/run.
    "force_phase2_next_run": False,
    # Theme + dev "Test as Customer" toggle. Both are user-tunable via
    # POST /api/config and exposed in GET /api/state — they belong here so
    # the schema is self-consistent and `/api/reset` preserves them via the
    # explicit preserve list rather than relying on `.get()` masking.
    "light_mode": False,
    "force_customer_mode": False,
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
DATA_DIR.mkdir(parents=True, exist_ok=True)
_store_db = DB_PATH
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
def _ensure_schema_migrations() -> None:
    """Belt-and-suspenders schema migration runner. The session store also
    runs migrations during its own __init__, but doing it again at startup
    means: (a) a code update that adds a new column gets applied even if
    the session_store object was constructed before the new migration
    was added; (b) the migration log shows up in journalctl regardless of
    when the store was first touched. Idempotent — every call is a no-op
    once the schema is current.

    This is the long-term fix for the silent ALTER TABLE failures that
    used to cause `no such column: jp.job_category` on the Pi after
    `git pull && systemctl restart jobapp`.
    """
    if _session_store is None:
        return
    try:
        from pipeline.migrations import apply_all_migrations
        from pipeline.job_repo import init_schema as _init_jobs_schema
        with _session_store.connect() as conn:
            # Re-run jobs CREATE statements first so a fresh column added in
            # the same release as a fresh table doesn't fail the column add.
            _init_jobs_schema(conn)
            applied = apply_all_migrations(conn)
        if applied:
            print(
                f"[schema] migrations applied this boot: {len(applied)} step(s)",
                file=sys.stderr, flush=True,
            )
    except Exception as exc:
        # Migration must NEVER abort startup — failures are logged loudly so
        # the user can see them in journalctl and apply the manual fix.
        import traceback
        print(
            f"[schema] WARNING: schema migration failed at startup — "
            f"jobs feed may return 500: {type(exc).__name__}: {exc}",
            file=sys.stderr, flush=True,
        )
        traceback.print_exc(file=sys.stderr)


@app.on_event("startup")
def _start_ingestion() -> None:
    if os.environ.get("JOBS_AI_DISABLE_INGESTION"):
        # Test harness sets this to skip the 60s parallel backfill that
        # FastAPI's startup event would otherwise hang on under TestClient.
        return
    if _session_store is None:
        print("[ingest] session store unavailable; skipping job ingestion")
        return
    try:
        from pipeline import ingest as _job_ingest
        # Skip the parallel backfill if the index already has fresh data.
        # When the user restarts (e.g. to pick up a code change), the DB
        # already holds 70K+ jobs from yesterday's scheduler ticks; firing
        # 8 concurrent upserters at boot blocks SQLite reads for 30-60s
        # and the SPA's /api/jobs/feed spins forever during warm-up. The
        # scheduled per-source ticks (every cadence_seconds) refresh data
        # continuously after boot — backfill only matters on a cold DB.
        run_backfill = True
        try:
            with _session_store.connect() as _probe:
                latest = _probe.execute(
                    "SELECT MAX(started_at) FROM source_runs WHERE ok = 1"
                ).fetchone()
            if latest and latest[0]:
                from datetime import datetime as _dt, timezone as _tz
                last = _dt.fromisoformat(latest[0].replace("Z", "+00:00"))
                age_min = (_dt.now(_tz.utc) - last).total_seconds() / 60.0
                # 60-minute freshness window — anything newer means at least
                # one scheduler tick already ran successfully and the user
                # would not benefit from yet another backfill at boot.
                if age_min < 60:
                    run_backfill = False
                    print(f"[ingest] skipping startup backfill — last successful run "
                          f"{age_min:.0f} min ago; scheduler will keep data fresh")
        except Exception as exc:
            print(f"[ingest] freshness probe failed ({exc!r}); falling back to backfill")
        _job_ingest.start_scheduler(
            connect=_session_store.connect,
            run_backfill=run_backfill,
            backfill_timeout=60,
        )
    except Exception as exc:
        # Ingestion failures must not block the API from coming up.
        print(f"[ingest] failed to start scheduler: {exc!r}")


@app.on_event("startup")
def _start_user_scoring_loop() -> None:
    """Persistent per-user scoring refresh — independent of the ingestion
    scheduler. Every 10 minutes (after a 30 s warm-up) we walk the set of
    users that already have at least one row in ``user_job_scores`` and
    incrementally score any jobs that have landed since their last
    refresh. Full rescores fire on primary-resume change via
    ``_kick_user_scoring``; this tick keeps stable users in sync with
    the ingestion firehose without re-touching jobs that already have
    rows. Gated by ``JOBS_AI_DISABLE_USER_SCORING`` for tests.
    """
    if os.environ.get("JOBS_AI_DISABLE_USER_SCORING"):
        return
    if _session_store is None:
        return
    try:
        threading.Thread(
            target=_user_scoring_loop, daemon=True,
            name="user-scoring-loop",
        ).start()
    except Exception as exc:
        print(f"[user-scoring] loop failed to start: {exc!r}")


_USER_SCORING_TICK_SECONDS = 600  # 10 min
_USER_SCORING_PROFILE_CACHE: dict[str, tuple[float, dict]] = {}


def _user_scoring_loop() -> None:
    """Daemon-thread loop: every ~10 minutes, refresh incremental scores
    for users with at least one stored row. Pulls the user's profile from
    `auth_tokens.user_json` → `session_state.state_json` so we don't need
    a request to be in flight. Best-effort; swallows per-user failures."""
    time.sleep(30)  # let the boot backfill calm down before the first tick
    while True:
        try:
            _user_scoring_tick()
        except Exception as exc:
            print(f"[user-scoring] tick failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
        time.sleep(_USER_SCORING_TICK_SECONDS)


def _profile_for_user_id(user_id: str) -> dict | None:
    """Find the most recent profile JSON for *user_id* by scanning their
    persisted session_state rows. Falls back to None when the user
    hasn't uploaded a resume yet."""
    if _session_store is None or not user_id:
        return None
    try:
        with _session_store.connect() as conn:
            row = conn.execute(
                "SELECT ss.state_json FROM session_state ss "
                "JOIN sessions s ON s.id = ss.session_id "
                "WHERE s.user_id = ? ORDER BY ss.updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        if not row or not row[0]:
            return None
        st = json.loads(row[0])
    except Exception:
        return None
    profile = st.get("profile") if isinstance(st, dict) else None
    if not profile:
        return None
    if not profile.get("experience_levels"):
        # Mirror the runtime fallback in `_profile_for_search`: stash the
        # configured experience_levels so location_seniority math has the
        # same inputs the live endpoint sees.
        profile["experience_levels"] = st.get("experience_levels") or []
    return profile


def _user_scoring_tick() -> None:
    from pipeline import user_scoring as _us
    try:
        from pipeline import ingest as _ingest
        write_lock = getattr(_ingest, "_WRITE_LOCK", None)
    except Exception:
        write_lock = None
    if _session_store is None:
        return
    with _session_store.connect() as conn:
        users = _us.known_user_ids_with_scores(conn, limit=500)
    if not users:
        return
    print(f"[user-scoring] tick — refreshing {len(users)} active user(s)", flush=True)
    for uid in users:
        try:
            profile = _profile_for_user_id(uid)
            if not profile:
                continue
            with _session_store.connect() as conn:
                _us.score_new_jobs_for_user(
                    conn, uid, profile, write_lock=write_lock, max_jobs=5000,
                )
        except Exception as exc:
            print(f"[user-scoring] {uid[:8]} failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            continue


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

# Wall-clock timestamp captured the first time this module is imported.
# Surfaced via /api/dev/overview as "server uptime for this session" — i.e.
# how long since the uvicorn process bound, NOT the OS boot.
_SERVER_STARTED_AT = datetime.now()


def _server_uptime_seconds() -> float:
    """Seconds since this Python process started, as a float."""
    return max(0.0, (datetime.now() - _SERVER_STARTED_AT).total_seconds())


def _os_boot_time() -> float | None:
    """Epoch seconds at host OS boot, or None if psutil is unavailable."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        return float(psutil.boot_time())
    except Exception:
        return None


def _os_uptime_payload() -> dict:
    """Serialized host-uptime fields shared by /api/dev/metrics and overview.

    Single source of truth so both endpoints stay in sync and we only
    call `psutil.boot_time()` once per request.
    """
    boot = _os_boot_time()
    if boot is None:
        return {"os_uptime_s": None, "os_boot_at": None}
    uptime = max(0.0, time.time() - boot)
    return {
        "os_uptime_s": round(uptime, 1),
        "os_boot_at":  datetime.fromtimestamp(boot).isoformat(timespec="seconds"),
    }


# Per-core CPU sampling.  psutil's first call to cpu_percent() returns 0 on
# every call without an interval, so we keep a primed sampler and read its
# delta on each request.  ``cpu_times_percent`` decomposes the load into
# user / system / iowait / idle so the SPA can render htop-style segmented
# bars (green = user, red = system, blue = iowait).
_CPU_SAMPLE_LOCK = threading.Lock()
_CPU_LAST_SAMPLE = {"ts": 0.0, "data": None}


def _cpu_snapshot() -> dict:
    """Return a per-core CPU breakdown suitable for an htop-style UI.

    Cached for 800 ms so a polling Dev Ops page (typically 2 s tick) gets
    fresh numbers without re-blocking the request loop.  Returns:

      {
        "cores": [{"user": 12.3, "system": 4.1, "iowait": 0.8, "idle": 82.8,
                   "total": 17.2}],
        "logical": int,
        "physical": int | None,
        "load_avg": [1m, 5m, 15m] | None,
      }
    """
    try:
        import psutil
    except ImportError:
        return {"cores": [], "logical": 0, "physical": None, "load_avg": None,
                "error": "psutil not installed"}

    now = time.time()
    with _CPU_SAMPLE_LOCK:
        last = _CPU_LAST_SAMPLE
        if last["data"] is not None and (now - last["ts"]) < 0.8:
            return last["data"]
        try:
            # interval=None → returns the delta since the last call. We
            # prime the sampler at module load and re-read here; first
            # request after boot can return zeros, which is correct.
            per_core = psutil.cpu_times_percent(interval=None, percpu=True) or []
            cores: list[dict] = []
            for c in per_core:
                user    = float(getattr(c, "user", 0.0) or 0.0)
                nice    = float(getattr(c, "nice", 0.0) or 0.0)
                system  = float(getattr(c, "system", 0.0) or 0.0)
                iowait  = float(getattr(c, "iowait", 0.0) or 0.0)
                irq     = float(getattr(c, "irq", 0.0) or 0.0)
                softirq = float(getattr(c, "softirq", 0.0) or 0.0)
                steal   = float(getattr(c, "steal", 0.0) or 0.0)
                idle    = float(getattr(c, "idle", 0.0) or 0.0)
                # htop's coloring: user (incl. nice) → green; system (incl.
                # IRQ + steal) → red; iowait → blue.
                user_t = user + nice
                sys_t  = system + irq + softirq + steal
                io_t   = iowait
                total  = round(min(100.0, max(0.0, user_t + sys_t + io_t)), 1)
                cores.append({
                    "user":   round(user_t, 1),
                    "system": round(sys_t, 1),
                    "iowait": round(io_t, 1),
                    "idle":   round(max(0.0, idle), 1),
                    "total":  total,
                })
            try:
                phys = psutil.cpu_count(logical=False)
            except Exception:
                phys = None
            try:
                load = list(psutil.getloadavg())   # may raise on stripped envs
            except (AttributeError, OSError):
                load = None
            data = {
                "cores": cores,
                "logical": len(cores),
                "physical": phys,
                "load_avg": load,
            }
            last["ts"] = now
            last["data"] = data
            return data
        except Exception as exc:
            return {"cores": [], "logical": 0, "physical": None, "load_avg": None,
                    "error": f"{type(exc).__name__}: {exc}"}


def _memory_snapshot() -> dict:
    """Quick RAM headline — total / used / percent.  Same lazy-import
    guard as the CPU snapshot so this stays optional."""
    try:
        import psutil
    except ImportError:
        return {}
    try:
        m = psutil.virtual_memory()
        return {
            "total_mb":   round(m.total   / (1024 * 1024), 0),
            "used_mb":    round(m.used    / (1024 * 1024), 0),
            "percent":    round(m.percent, 1),
        }
    except Exception:
        return {}


# Sampling for top-processes — psutil's `cpu_percent()` measures across
# the interval BETWEEN consecutive calls, so the first call always returns
# 0%. We keep a primed registry that prepares each visible process and a
# 1.5 s cache so the dev page polls don't pay the per-process priming
# cost on every tick.
_PROC_SAMPLE_LOCK = threading.Lock()
_PROC_LAST_SAMPLE = {"ts": 0.0, "data": None}


def _top_processes(limit: int = 5) -> dict:
    """Return the top processes by CPU and by memory, htop-style.

    Result shape:
        {
          "by_cpu": [{pid, name, cpu, mem_mb, mem_pct, user}, ...],
          "by_mem": [...same shape...],
          "sampled_at": <iso>,
        }

    Sampling strategy: psutil's process-level cpu_percent only returns a
    real number on its 2nd+ call (interval=None compares against the
    previous sample). We cache a primed list for ~1.5 s so repeated
    polls don't lose the delta to teardown/rebuild churn.
    """
    try:
        import psutil
    except ImportError:
        return {"by_cpu": [], "by_mem": [], "error": "psutil not installed"}

    now = time.time()
    with _PROC_SAMPLE_LOCK:
        cached = _PROC_LAST_SAMPLE
        # 2.5 s TTL — strictly longer than the SPA's 2 s poll cadence so
        # consecutive Server-tab polls coalesce on the cached snapshot
        # instead of each paying the 0.5 s sleep below.
        if cached["data"] is not None and (now - cached["ts"]) < 2.5:
            return cached["data"]
        try:
            ncpu = psutil.cpu_count() or 1
            # First sweep primes psutil's per-process cpu accumulator.
            procs: list = []
            for p in psutil.process_iter(["pid", "name", "username"]):
                try:
                    p.cpu_percent(interval=None)
                    procs.append(p)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            # 0.5 s delta — long enough that even on a Pi (where iterating
            # /proc takes a few hundred ms) every process gets a usable
            # cpu_percent reading. Shorter windows produced "everything
            # at 0 %" tables on the production box.
            time.sleep(0.5)
            rows: list[dict] = []
            for p in procs:
                try:
                    cpu = p.cpu_percent(interval=None) / float(ncpu)
                    mi  = p.memory_info()
                    mem_mb  = round(mi.rss / (1024 * 1024), 1)
                    mem_pct = round(p.memory_percent(), 1)
                    rows.append({
                        "pid":     p.pid,
                        "name":    (p.info.get("name") or "")[:40],
                        "user":    (p.info.get("username") or "")[:24],
                        "cpu":     round(cpu, 1),
                        "mem_mb":  mem_mb,
                        "mem_pct": mem_pct,
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            by_cpu = sorted(rows, key=lambda r: r["cpu"],     reverse=True)[:limit]
            by_mem = sorted(rows, key=lambda r: r["mem_mb"],  reverse=True)[:limit]
            data = {
                "by_cpu":     by_cpu,
                "by_mem":     by_mem,
                "sampled_at": datetime.now().isoformat(timespec="seconds"),
                "total":      len(rows),
            }
            cached["ts"] = time.time()
            cached["data"] = data
            return data
        except Exception as exc:
            return {"by_cpu": [], "by_mem": [], "error": f"{type(exc).__name__}: {exc}"}


def _cpu_temperature() -> dict | None:
    """Read the CPU package temperature where the OS exposes it.

    Returns ``{current, high, critical, label}`` or ``None`` if no
    temperature sensor is reachable. Linux exposes most thermal zones
    via ``/sys/class/thermal``; macOS requires `sudo`-only IOKit calls
    (psutil returns empty); Windows requires WMI which often errors
    without admin.  None is a perfectly valid result on those systems —
    the UI just hides the temperature row.
    """
    try:
        import psutil
    except ImportError:
        return None
    if not hasattr(psutil, "sensors_temperatures"):
        return None
    try:
        sensors = psutil.sensors_temperatures(fahrenheit=False) or {}
    except Exception:
        return None
    if not sensors:
        return None
    # Prefer the package-level reading (Intel: `coretemp`, AMD: `k10temp`,
    # generic: `cpu_thermal` on Pi). Fall back to whichever sensor reports.
    for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "cpu", "soc_thermal"):
        readings = sensors.get(key) or []
        if not readings:
            continue
        primary = next(
            (r for r in readings
             if "package" in (r.label or "").lower() or "tctl" in (r.label or "").lower()),
            readings[0],
        )
        return {
            "current":  round(float(primary.current), 1),
            "high":     round(float(primary.high), 1)     if primary.high     else None,
            "critical": round(float(primary.critical), 1) if primary.critical else None,
            "label":    primary.label or key,
            "source":   key,
        }
    # Last resort: pick whatever sensor reported anything.
    for key, readings in sensors.items():
        if readings:
            r = readings[0]
            return {
                "current":  round(float(r.current), 1),
                "high":     round(float(r.high), 1)     if r.high     else None,
                "critical": round(float(r.critical), 1) if r.critical else None,
                "label":    r.label or key,
                "source":   key,
            }
    return None


# Prime psutil's per-core CPU sampler at uvicorn startup so the first
# /api/dev/metrics poll returns real numbers instead of all-zero bars.
# We deliberately don't prime per-process counters here — `_top_processes`
# does its own sweep on each request and module-load priming would expire
# before the first dev-page poll arrives anyway.
@app.on_event("startup")
def _prime_psutil_sampler() -> None:
    try:
        import psutil
        psutil.cpu_times_percent(interval=None, percpu=True)
    except ImportError:
        pass
    except Exception:
        pass


# ── Claude CLI health check ───────────────────────────────────────────────────

def _claude_cli_credential_diagnostic() -> str:
    """Return a one-line operator hint about where Claude CLI looks for OAuth.

    Used in health-check failure logs so journalctl readers can immediately
    see where to investigate. Doesn't read or validate the credentials — just
    points at the canonical locations.
    """
    if sys.platform == "darwin":
        return ("Check macOS Keychain item 'Claude Code-credentials' "
                "(open Keychain Access → search 'claude'). "
                "Re-auth: run `claude /login` on the server.")
    # Linux / others
    home = os.path.expanduser("~/.claude/.credentials.json")
    exists = os.path.exists(home)
    if exists:
        return (f"OAuth file exists at {home} — token may be expired or invalid. "
                "Re-auth: run `claude /login` on the server.")
    return (f"OAuth file missing at {home}. "
            "Run `claude /login` on the server to authenticate.")


@app.on_event("startup")
async def _claude_cli_health_check_startup():
    """One-shot CLI health check at app startup.

    Sets pipeline.providers._CLI_HEALTHY = True/False. Pro users coerce to
    Ollama (via _can_use_claude) when False. Runs off-thread so the
    blocking subprocess doesn't stall the FastAPI event loop.
    """
    import asyncio
    import pipeline.providers as _p
    if os.environ.get("CLAUDE_CLI_DISABLE_HEALTH_CHECK") == "1":
        # Skip the boot probe — used by integration tests + ops kill switch.
        # CLAUDE_CLI_DISABLED=1 additionally forces _CLI_HEALTHY=False.
        _p._CLI_HEALTHY = False if os.environ.get("CLAUDE_CLI_DISABLED") == "1" else True
        return
    try:
        await asyncio.to_thread(_p._run_cli, "ping", timeout_s=20.0, budget_usd=0.01)
        _p._CLI_HEALTHY = True
        print("[claude-cli] verified at startup", flush=True)
    except Exception as e:
        _p._CLI_HEALTHY = False
        hint = _claude_cli_credential_diagnostic()
        print(
            f"[claude-cli] FAILED at startup — Pro users coerced to Ollama: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr, flush=True,
        )
        print(f"[claude-cli] {hint}", file=sys.stderr, flush=True)


@app.on_event("startup")
async def _start_claude_cli_health_ticker():
    """Spawn the 5-min periodic re-check daemon.

    Survives transient keychain expiry / network blips and auto-restores
    _CLI_HEALTHY when the credential issue is fixed. Only logs on state
    transitions (healthy→degraded and degraded→healthy) to avoid journalctl
    noise on every 5-min tick.
    """
    import pipeline.providers as _p
    if os.environ.get("CLAUDE_CLI_DISABLE_HEALTH_CHECK") == "1":
        _p._CLI_HEALTHY = False if os.environ.get("CLAUDE_CLI_DISABLED") == "1" else True
        return

    def _loop():
        import time as _time
        while True:
            _time.sleep(300)
            try:
                _p._run_cli("ping", timeout_s=20.0, budget_usd=0.01)
                if not _p._CLI_HEALTHY:
                    print("[claude-cli] health restored", flush=True)
                _p._CLI_HEALTHY = True
            except Exception as e:
                if _p._CLI_HEALTHY:
                    hint = _claude_cli_credential_diagnostic()
                    print(
                        f"[claude-cli] health DEGRADED: {type(e).__name__}: {e}",
                        file=sys.stderr, flush=True,
                    )
                    print(f"[claude-cli] {hint}", file=sys.stderr, flush=True)
                _p._CLI_HEALTHY = False

    threading.Thread(target=_loop, daemon=True, name="claude-cli-health").start()
    print("[claude-cli] 5-min health ticker started", flush=True)


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


# Per-session phase progress with bounded recent-log buffer. Surfaced via
# /api/state.running_phases so the SPA can show "this phase is still running
# on the server" when the user starts a phase, navigates to another page,
# and comes back. The local AgentPage `running` state is lost on unmount —
# without this server-side mirror the UI wrongly reports "idle" while the
# worker thread is still busy.
_session_phase_progress: dict[str, dict[int, dict]] = {}
_session_phase_progress_lock = threading.Lock()
_PHASE_PROGRESS_LOG_BUFFER = 60   # last N log lines kept in memory per phase


def _phase_progress_open(session_id: str | None, phase: int) -> None:
    if not session_id:
        return
    with _session_phase_progress_lock:
        bucket = _session_phase_progress.setdefault(session_id, {})
        bucket[phase] = {"started_at": time.time(), "recent_logs": []}


def _phase_progress_log(session_id: str | None, phase: int, line: str) -> None:
    if not session_id or not line:
        return
    line = line.rstrip()
    if not line:
        return
    with _session_phase_progress_lock:
        bucket = _session_phase_progress.get(session_id) or {}
        rec = bucket.get(phase)
        if rec is None:
            return
        rec["recent_logs"].append(line)
        if len(rec["recent_logs"]) > _PHASE_PROGRESS_LOG_BUFFER:
            del rec["recent_logs"][:-_PHASE_PROGRESS_LOG_BUFFER]


def _phase_progress_close(session_id: str | None, phase: int) -> None:
    if not session_id:
        return
    with _session_phase_progress_lock:
        bucket = _session_phase_progress.get(session_id, {})
        bucket.pop(phase, None)
        if not bucket:
            _session_phase_progress.pop(session_id, None)


def _phase_progress_snapshot(session_id: str | None) -> list[dict]:
    """Public-facing list of running phases for /api/state. Caps the
    per-phase log tail so a long-running phase doesn't bloat the polled
    payload — the SPA only needs enough lines to feel "live"."""
    if not session_id:
        return []
    with _session_phase_progress_lock:
        bucket = _session_phase_progress.get(session_id) or {}
        out = []
        for phase, rec in sorted(bucket.items()):
            out.append({
                "phase":       phase,
                "started_at":  rec["started_at"],
                "elapsed_s":   round(time.time() - rec["started_at"], 1),
                "recent_logs": list(rec["recent_logs"])[-20:],
            })
        return out


def _is_local_request(request: Request) -> bool:
    host = getattr(request.client, "host", "") if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


_LEGACY_WHITELIST_DEFAULT = "NVIDIA, Apple, Microsoft, Intel, IBM, Micron, Samsung, TSMC"


def _load_session_state(session_id: str) -> dict:
    # Anonymous sessions live in memory. Authenticated sessions live in SQLite.
    # Try memory first; if missing, peek SQLite (read-only — never INSERT a
    # row for an anonymous session, otherwise it shows up as a ghost user).
    loaded = _memory_sessions.get(session_id)
    if loaded is None and _session_store is not None:
        loaded = _session_store.peek_state(session_id)
    state = _default_state()
    state.update(loaded or {})
    state["done"] = set(state.get("done") or [])
    state["liked_ids"] = set(state.get("liked_ids") or [])
    state["hidden_ids"] = set(state.get("hidden_ids") or [])
    state["extracting_ids"] = set(state.get("extracting_ids") or [])
    # One-shot scrub: clear the legacy hardcoded EE/semiconductor whitelist
    # for any session that still has it saved verbatim. Only matches the
    # exact prior default — users who actually customized it keep their list.
    if str(state.get("whitelist") or "").strip() == _LEGACY_WHITELIST_DEFAULT:
        state["whitelist"] = ""
    # `mode='demo'` was retired — coerce stale state forward to the new
    # default so every poll of /api/state returns a still-valid mode.
    if state.get("mode") == "demo":
        state["mode"] = "ollama"
    # Non-permitted users who saved `mode='anthropic'` before this gate landed
    # would otherwise hit the plan_required gate on every phase run. Coerce
    # them back to Ollama so the app stays usable. Permitted users (devs and
    # Pro tier) keep their config.
    user = state.get("user") or {}
    if state.get("mode") == "anthropic" and not _can_use_claude(user):
        state["mode"] = "ollama"
    # Cloud Ollama models are Pro-only. A free user whose session was saved
    # while they were Pro (or before the gate landed) would otherwise silently
    # keep using a paid model after downgrade. Snap them to the canonical
    # free-tier local model; the SettingsPage UI does the same on next render.
    # Default fallback is "pro" — we're in the everyone-is-Pro testing phase
    # (per CLAUDE.md §2). When a user dict is missing plan_tier (legacy data
    # or in-flight propagation), treat as Pro rather than Free.
    plan = (user.get("plan_tier") or "pro").lower()
    model = str(state.get("ollama_model") or "")
    if (state.get("mode") == "ollama"
            and model.lower().endswith("cloud")
            and plan != "pro"
            and not user.get("is_developer")):
        state["ollama_model"] = LOCAL_OLLAMA_MODEL
    # Testing-phase: every Ollama session is forced to the single
    # canonical model `CLOUD_OLLAMA_MODEL` (gemma4:31b-cloud). No
    # per-user variation, no cloud-variant tolerance, no developer
    # exemption — the product surface only supports one model right
    # now. Setting a different value via the Settings dropdown will be
    # reverted on the next /api/state poll. Remove this block when the
    # testing phase ends and the auto-promote-to-Pro migration in
    # pipeline/migrations.py is lifted.
    if state.get("mode") == "ollama":
        state["ollama_model"] = CLOUD_OLLAMA_MODEL
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
    # Only persist sessions that belong to an authenticated user. Anonymous
    # sessions (pre-login OAuth state, fresh visits) are kept in memory so
    # they never create ghost rows in the SQLite users/sessions tables.
    has_user = bool((payload.get("user") or {}).get("id")) or bool((payload.get("user") or {}).get("email"))
    with _session_lock(sid):
        if _session_store is None or not has_user:
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
        response.set_cookie(_STATE_COOKIE, switched_session_id, httponly=True, samesite="lax", secure=_COOKIE_SECURE)
        return response
    current_session_id = _S.session_id() or request_session_id
    if is_new or current_session_id != request_session_id:
        response.set_cookie(_STATE_COOKIE, current_session_id, httponly=True, samesite="lax", secure=_COOKIE_SECURE)
    # Skip the post-response state save for read-only endpoints. /api/state in
    # particular is polled every 8 s and used to clobber `google_oauth_state` set
    # by a concurrent /api/auth/google request — load happens before the OAuth
    # mutation, save happens after, so the OAuth state vanishes and the callback
    # fails the state check.
    # GET requests are read-only by convention. Saving stale per-request state
    # on every read clobbers concurrent writes — e.g. /api/reset clears the
    # session, then a GET /api/ollama/status that started before the reset
    # finishes after and writes its (pre-reset) snapshot back to the DB,
    # silently undoing the reset. The two GET endpoints that DO mutate state
    # (auth_google sets google_oauth_state; the OAuth callback fully
    # re-binds the session) save explicitly inside their handlers.
    skip_save = (
        response.headers.get("content-type", "").startswith("text/event-stream")
        or request.method == "GET"
        or request.url.path in ("/api/state", "/api/webhooks/stripe")
    )
    if not skip_save:
        try:
            _save_bound_state(_S.current(), current_session_id)
        except Exception:
            import traceback as _tb
            _tb.print_exc()
    return response


# ── Resume helpers ────────────────────────────────────────────────────────────

def _new_resume_record(filename: str, text: str, latex_source=None,
                       original_path: str | None = None,
                       source_format: str | None = None) -> dict:
    now = datetime.now().isoformat()
    if source_format is None and filename:
        suffix = Path(filename).suffix.lower().lstrip(".")
        if suffix in ("tex", "docx", "pdf", "txt", "md"):
            source_format = suffix
    return {
        "id": uuid.uuid4().hex,
        "filename": filename,
        "text": text,
        "latex_source": latex_source,
        # Path (relative to OUTPUT_DIR, posix style) of the original uploaded
        # file kept on disk so the Resume preview can embed the actual PDF
        # rather than just the extracted text. None for the demo resume and
        # for paste-as-text records — the SPA falls back to the text view.
        "original_path": original_path,
        # "tex" | "docx" | "pdf" | "txt" | "md" | None — drives which
        # tailoring renderer runs in /api/resume/tailor. None means "use
        # the default template-library path".
        "source_format": source_format,
        "profile": None,
        "primary": False,
        "created_at": now,
        "updated_at": now,
    }


def _get_primary_resume() -> dict | None:
    primary = None
    rs = _S.get("resumes") or []
    for r in rs:
        if r.get("primary"):
            primary = r
            break
    if primary is None and rs:
        primary = rs[0]
    if primary is None:
        return None
    # Backfill source_format on legacy records (uploaded before this field existed).
    if "source_format" not in primary or primary.get("source_format") is None:
        suffix = ""
        if primary.get("original_path"):
            suffix = Path(primary["original_path"]).suffix.lower().lstrip(".")
        elif primary.get("filename"):
            suffix = Path(primary["filename"]).suffix.lower().lstrip(".")
        primary["source_format"] = suffix if suffix in ("tex", "docx", "pdf", "txt", "md") else None
    return primary


def _get_resume_by_id(rid: str) -> dict | None:
    for r in (_S.get("resumes") or []):
        if r["id"] == rid:
            return r
    return None


_DEGREE_TOKEN_TO_LEVEL = (
    ("phd",         "phd"),  ("doctor",      "phd"),
    ("master",      "masters"), ("m.s",         "masters"), ("m.eng",       "masters"),
    ("msc",         "masters"), ("mba",         "masters"),
    ("bachelor",    "bachelors"), ("b.s",        "bachelors"), ("b.eng",       "bachelors"),
    ("bsc",         "bachelors"), ("b.a",        "bachelors"),
    ("associate",   "associates"), ("a.a",       "associates"), ("a.s",         "associates"),
    ("high school", "high_school"),
)


def _infer_edu_filter_from_profile(profile: dict) -> list[str]:
    """Return a sensible default education-filter list based on the highest
    degree in the profile. The convention used downstream is "show jobs
    requiring this OR lower" — so `[bachelors]` keeps high-school +
    associates + bachelors postings. We always pin to the highest level
    we can detect; the user can broaden it in Settings."""
    edu_entries = (profile or {}).get("education") or []
    best_rank = -1
    best_level = ""
    for e in edu_entries:
        if not isinstance(e, dict):
            continue
        haystack = " ".join(str(e.get(k) or "") for k in ("degree", "field", "name")).lower()
        if not haystack.strip():
            continue
        for token, level in _DEGREE_TOKEN_TO_LEVEL:
            if token in haystack and _EDU_RANK[level] > best_rank:
                best_rank = _EDU_RANK[level]
                best_level = level
    return [best_level] if best_level else []


def _infer_experience_levels_from_profile(profile: dict) -> list[str]:
    """Infer the experience-level chips (``internship`` / ``entry-level`` /
    ``mid-level`` / ``senior``) from the resume profile.

    Order of precedence (first hit wins so a single strong signal isn't
    drowned out by a weaker one further down):
      1. Student / new-graduate signals **in the summary** —
         "fresh graduating", "currently studying", "recent graduate", etc.
         These come first because a student-with-leadership often holds a
         "Chief / Lead / President" club title that would otherwise trip
         the senior heuristic (Colin Tse held "Chief of Operation" at a
         student film club).
      2. Intern token in the most-recent role title — late-stage interns
         without a student-signal summary still belong here.
      3. Senior keywords in the most-recent role title or target titles
         (Senior / Lead / Principal / Staff / Director / Head / VP /
         Architect) → ``["senior"]``.
      4. In-progress education — a degree whose end year is in the future
         or marked ``Present`` / ``Current``, with less than ~5 years of
         work span. Catches active students even when the LLM rewrites the
         summary in a way the rule-1 regex misses. The 5-year gate is
         what protects mid-career professionals pursuing an MBA / EdD
         from being miscategorised as students.
      5. Fallback: rough years-of-experience computed from date ranges on
         work entries — earliest start year to latest end year (treats
         Present/Current as today's calendar year).

    Returns lowercase, hyphenated values that match the frontend
    ``_EXP_LEVEL_OPTIONS`` chips. Empty list when nothing's confident.
    """
    if not profile:
        return []

    summary = (profile.get("summary") or "").lower()

    exp_entries = profile.get("experience") or profile.get("work_experience") or []
    titles_blob_parts: list[str] = []
    for e in exp_entries[:3]:
        if isinstance(e, dict):
            t = e.get("title") or ""
            if t:
                titles_blob_parts.append(str(t).lower())
    for t in (profile.get("target_titles") or []):
        if isinstance(t, dict):
            t = t.get("title") or ""
        if t:
            titles_blob_parts.append(str(t).lower())
    titles_blob = " ".join(titles_blob_parts)

    # ── Rule 1: student / new-graduate signals in the summary ──────────
    # Trailing \w* is intentional: matches "fresh graduating", "graduated",
    # etc., where a trailing \b would fail because the next char is a word
    # character. Summary-only because club titles ("Senior Class President")
    # would otherwise misroute student profiles to senior in titles.
    student_re = re.compile(
        r"(?:fresh\s+graduat\w*|new\s+graduat\w*|recent\s+graduat\w*|"
        r"graduating\s+student|rising\s+(?:senior|junior)|"
        r"current(?:ly)?\s+studying|undergraduate\s+student|"
        r"current\s+student|college\s+student)",
        re.IGNORECASE,
    )
    if student_re.search(summary):
        return ["internship", "entry-level"]

    # ── Rule 2: intern / co-op token in role titles ───────────────────
    intern_re = re.compile(
        r"\b(?:intern|internship|trainee|co-?op)\b",
        re.IGNORECASE,
    )
    if intern_re.search(titles_blob):
        return ["internship", "entry-level"]

    # If the profile is genuinely empty (no summary, no titles, no
    # experience entries) return [] so a force_refresh caller doesn't
    # clobber an existing user-set value with a default guess.
    if not summary.strip() and not titles_blob.strip() and not exp_entries:
        return []

    # ── Compute the "active student" flag up front so it can gate the
    # senior-keyword check below. An active student is someone with a
    # degree-in-progress AND less than ~5 years of work history. The
    # 5-year work-span gate prevents misclassifying a senior engineer who
    # happens to be pursuing an MBA. We need this BEFORE the senior check
    # because student leadership titles ("Chief of Operation" at a film
    # club) would otherwise hit rule 3 even when the LLM rewrites the
    # summary in a way the rule-1 regex doesn't catch.
    import datetime as _dt
    this_year = _dt.date.today().year
    year_re = re.compile(r"(?:19|20)\d{2}")
    in_progress_marker_re = re.compile(
        r"\b(?:present|current|now|expected|in\s+progress|ongoing)\b",
        re.IGNORECASE,
    )

    work_years_seen: list[int] = []
    work_has_present = False
    for e in exp_entries:
        if not isinstance(e, dict):
            continue
        dates = e.get("dates") or e.get("date") or ""
        if not isinstance(dates, str):
            continue
        for m in year_re.finditer(dates):
            try:
                work_years_seen.append(int(m.group(0)))
            except ValueError:
                pass
        if in_progress_marker_re.search(dates):
            work_has_present = True
    if work_years_seen:
        work_latest = this_year if work_has_present else max(work_years_seen)
        work_span = max(0, work_latest - min(work_years_seen))
    else:
        work_span = 0

    edu_in_progress = False
    for edu in (profile.get("education") or []):
        if not isinstance(edu, dict):
            continue
        edu_blob = " ".join(
            str(edu.get(k) or "")
            for k in ("year", "years", "dates", "date", "name", "institution")
        )
        if in_progress_marker_re.search(edu_blob):
            edu_in_progress = True
            break
        for m in year_re.finditer(edu_blob):
            try:
                if int(m.group(0)) > this_year:
                    edu_in_progress = True
                    break
            except ValueError:
                pass
        if edu_in_progress:
            break
    is_active_student = edu_in_progress and work_span < 5

    # ── Rule 3: senior keywords in role / target titles ───────────────
    # Skipped for active students because their leadership titles are
    # virtually always student-organisation roles, not real senior
    # employment.
    senior_re = re.compile(
        r"\b(?:senior|lead|principal|staff|director|head\s+of|"
        r"chief|vp|vice\s+president|architect)\b",
        re.IGNORECASE,
    )
    if not is_active_student and senior_re.search(titles_blob):
        return ["senior"]

    # ── Rule 4: active student fall-through ───────────────────────────
    if is_active_student:
        return ["internship", "entry-level"]

    # ── Rule 5: fallback years-of-experience from work date ranges ────
    if not work_years_seen:
        return ["entry-level"]
    if work_span >= 7:
        return ["senior"]
    if work_span >= 3:
        return ["mid-level"]
    return ["entry-level"]


def _apply_profile_search_prefs(state: dict, profile: dict | None,
                                  *, force_refresh: bool = False) -> bool:
    """Fill search-pref fields on *state* from a freshly-extracted *profile*.

    Two modes:
      * ``force_refresh=False`` (default) — only fills EMPTY fields. Used on
        the very first upload when the prefs are still at their fresh-state
        defaults; preserves whatever the user typed in by hand.
      * ``force_refresh=True`` — overwrites pref values from the profile.
        Used when the *primary* resume changes (clear "use this resume's
        signals now" intent) or when an extraction completes for the
        primary resume (the fresh extraction IS the latest signal). The
        cost is that purely-manual customisations get clobbered on the
        next re-scan; this matches the expected behaviour that uploading
        a new resume should refresh job-search location / education /
        experience-level chips together.

    Returns True if anything was changed. The caller is responsible for
    persisting the state.
    """
    if not profile:
        return False
    changed = False

    def _is_blank(val) -> bool:
        if val is None:
            return True
        if isinstance(val, str):
            return not val.strip()
        if isinstance(val, list):
            return len(val) == 0
        return False

    def _set(key: str, value) -> None:
        nonlocal changed
        if value is None or _is_blank(value):
            return
        if force_refresh or _is_blank(state.get(key)):
            if state.get(key) != value:
                state[key] = value
                changed = True

    # job_titles ← profile.target_titles
    titles: list[str] = []
    for t in (profile.get("target_titles") or []):
        if isinstance(t, dict):
            t = t.get("title") or ""
        t = str(t).strip()
        if t:
            titles.append(t)
    if titles:
        _set("job_titles", ", ".join(titles[:5]))

    # location ← profile.location
    loc = str((profile.get("location") or "")).strip()
    if loc:
        _set("location", loc)

    # education_filter ← inferred from profile.education (highest degree).
    derived_edu = _infer_edu_filter_from_profile(profile)
    if derived_edu:
        _set("education_filter", derived_edu)

    # experience_levels ← inferred from summary + recent roles + date ranges.
    derived_exp = _infer_experience_levels_from_profile(profile)
    if derived_exp:
        _set("experience_levels", derived_exp)

    return changed


def _sync_primary_scalars(record=None, *, force_prefs_refresh: bool = False):
    """Mirror the primary resume's scalar fields (text / filename / profile)
    onto session state, and optionally refresh the job-search prefs from
    the new primary's profile. Pass ``force_prefs_refresh=True`` whenever
    the caller represents a "primary changed" event (set-primary, last
    primary deleted) — the fresh resume IS the latest signal of who the
    user is, so location / experience / education chips should overwrite
    the prefs cached from the previous primary.
    """
    pr = record or _get_primary_resume()
    if pr:
        _S["resume_text"] = pr["text"]
        _S["latex_source"] = pr.get("latex_source")
        _S["resume_filename"] = pr["filename"]
        _S["profile"] = pr.get("profile")
        if pr.get("profile"):
            _S["done"].add(1)
            # Mirror the profile-derived search prefs onto the bound state so
            # the SettingsPage picks up titles/location/education immediately
            # after a primary switch (not just after a fresh extraction).
            _apply_profile_search_prefs(
                _S.current(), pr["profile"],
                force_refresh=force_prefs_refresh,
            )
            # Refresh persisted scores for the authenticated user — the
            # /api/jobs/feed query LEFT JOINs `user_job_scores` and sorts
            # by that column when present, so a primary change with no
            # rescore would leave the feed ordered by the previous resume.
            _kick_user_scoring_for_bound_user(pr["profile"])
        else:
            _S["done"].discard(1)
    else:
        _S["resume_text"] = None
        _S["latex_source"] = None
        _S["resume_filename"] = None
        _S["profile"] = None
        _S["done"].discard(1)


def _kick_user_scoring_for_bound_user(profile: dict | None) -> None:
    """Spawn a full rescore for the currently-bound authenticated user.

    Safe to call from any request handler — it's a no-op when there's no
    authenticated user (anonymous sessions don't have persistent scores;
    the live `/api/jobs/feed` rerank still ranks the page in real time).

    Also evicts the lazy in-memory score cache (`_SCORE_CACHE`) for this
    user so the next `/api/jobs/score-batch` poll recomputes against the
    new profile instead of returning a 1-hour-stale row computed against
    the previous primary resume. Mirrors the same invalidation pattern
    in `_run_extraction_bg`'s success path — without it, switching the
    primary resume rewrites `user_job_scores` but the per-card lazy
    scores keep showing the previous resume's numbers for up to an hour.
    """
    user = _S.get("user") or {}
    uid = user.get("id") if isinstance(user, dict) else None
    if uid:
        evicted = _score_cache_invalidate_user(uid)
        if evicted:
            print(
                f"[score-cache] evicted {evicted} entries for user={uid[:8]} "
                f"after primary-resume change",
                flush=True,
            )
        _kick_user_scoring(uid, profile, only_new=False)


def _serialize_resume(r: dict) -> dict:
    p = r.get("profile") or {}
    is_extracting = r["id"] in (_S.get("extracting_ids") or set())
    full_profile = {k: v for k, v in p.items() if not k.startswith("_")} if p else None
    # Surface the original-file URL only when the bytes still exist on disk.
    # Legacy records persisted before the upload-store landed have
    # original_path = None or point at a file that's already been cleaned up
    # — in either case the SPA falls back to the text-only preview.
    original_url = ""
    original_kind = ""
    rel = r.get("original_path") or ""
    if rel:
        full = (OUTPUT_DIR / rel).resolve()
        try:
            full.relative_to(OUTPUT_DIR.resolve())
        except ValueError:
            full = None
        if full and full.exists() and full.is_file():
            original_url = _output_url(full)
            original_kind = full.suffix.lstrip(".").lower()
    # Generated preview PDF — used for .txt / .docx / .tex uploads (and as
    # an extra fallback for .pdf). The renderer is best-effort, so this is
    # only set when the file actually exists on disk.
    preview_pdf_url = ""
    prel = r.get("preview_pdf_path") or ""
    if prel:
        pfull = (OUTPUT_DIR / prel).resolve()
        try:
            pfull.relative_to(OUTPUT_DIR.resolve())
        except ValueError:
            pfull = None
        if pfull and pfull.exists() and pfull.is_file():
            preview_pdf_url = _output_url(pfull)
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
        "original_url": original_url,
        "original_kind": original_kind,
        "preview_pdf_url": preview_pdf_url,
        # Drives the tailoring renderer choice in /api/resume/tailor.
        "source_format": r.get("source_format") or original_kind or None,
    }


def _kick_extraction(record: dict, *, force: bool = False) -> None:
    """Mark *record* as extracting on the bound session and spawn the
    background thread. Centralizes the thread-launch boilerplate that
    upload/demo/text-edit/primary-switch endpoints all need."""
    _S["extracting_ids"].add(record["id"])
    threading.Thread(
        target=_run_extraction_bg,
        args=(record, _S.current(), _S.session_id()),
        kwargs={"force": force},
        daemon=True,
    ).start()


# ── Persistent user_job_scores plumbing ────────────────────────────────────
# Tracks which users currently have a full background scoring run in flight
# so we coalesce duplicate kicks (rapid /api/resume/upload, multiple primary
# switches, scheduler tick racing the user click) into a single worker.
_USER_SCORING_LOCKS: dict[str, threading.Lock] = {}
_USER_SCORING_LOCKS_LOCK = threading.Lock()


def _user_scoring_lock(user_id: str) -> threading.Lock:
    with _USER_SCORING_LOCKS_LOCK:
        lock = _USER_SCORING_LOCKS.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _USER_SCORING_LOCKS[user_id] = lock
        return lock


def _kick_user_scoring(user_id: str | None, profile: dict | None,
                        *, only_new: bool = False, max_jobs: int = 30000) -> None:
    """Spawn a daemon thread that scores every (or every new) live job
    against *profile* and writes results into ``user_job_scores``.

    Coalesces concurrent kicks per user via ``_USER_SCORING_LOCKS`` — if a
    full rescore is already running for this user, the second caller is a
    no-op. Newly-arrived jobs since the in-flight rescore started will be
    picked up by the periodic ``_user_scoring_tick`` below.
    """
    if not user_id or not profile:
        return
    if _session_store is None:
        return
    lock = _user_scoring_lock(user_id)

    def _run() -> None:
        if not lock.acquire(blocking=False):
            # Another rescore is in flight; skip silently.
            return
        try:
            from pipeline import user_scoring as _us
            try:
                from pipeline import ingest as _ingest
                wl = getattr(_ingest, "_WRITE_LOCK", None)
            except Exception:
                wl = None
            try:
                with _session_store.connect() as conn:
                    summary = _us.score_jobs_for_user(
                        conn, user_id, profile,
                        write_lock=wl, only_new=only_new, max_jobs=max_jobs,
                    )
                print(
                    f"[user-scoring] user={user_id[:8]} only_new={only_new} "
                    f"scored={summary.get('scored')} skipped={summary.get('skipped')} "
                    f"hash={summary.get('profile_hash')} "
                    f"elapsed={summary.get('elapsed_s')}s",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[user-scoring] WARNING: scoring failed for {user_id[:8]}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr, flush=True,
                )
        finally:
            lock.release()

    threading.Thread(target=_run, daemon=True, name=f"user-scoring-{user_id[:8]}").start()


def _run_extraction_bg(record: dict, state: dict, session_id: str | None,
                       force: bool = False) -> None:
    """Background resume extraction.

    Critical correctness invariant: the persisted save at the end MUST
    operate on the LATEST state from storage, not the stale `state` dict
    the caller passed in. Otherwise this thread silently undoes any
    /api/reset, /api/resume/{id} delete, or rapid re-upload that
    happened during the LLM call. Concretely: the thread captured a
    reference to `state` at spawn time; by the time extraction returns
    seconds-to-minutes later, that dict may no longer represent the
    user's current session — but blindly saving it would clobber the DB.
    """
    import traceback as _tb
    _bind_thread_state(state, session_id)
    rid = record["id"]
    lock = _session_lock(session_id)

    # Mark as extracting on the dict the spawner can still observe (so the
    # next /api/state poll shows the spinner) AND persist that to the DB
    # so anyone reading the store also sees we're working.
    with lock:
        ids = state.get("extracting_ids")
        if not isinstance(ids, set):
            ids = set(ids or [])
            state["extracting_ids"] = ids
        ids.add(rid)
        _save_bound_state(state, session_id)

    # Run the actual LLM call OUTSIDE the lock — it's slow and we don't
    # want to block other requests on the same session.
    extraction_result = None
    extraction_error: str | None = None
    try:
        prov = _make_provider()
        preferred = [
            t.strip()
            for t in (state.get("job_titles") or "").split(",")
            if t.strip() and t.strip().lower() != "engineer"
        ]
        extraction_result = phase1_ingest_resume(
            record["text"], prov,
            preferred_titles=preferred or None,
            force=force,
        )
    except Exception as e:
        extraction_error = str(e)
        print(f"[resume extraction] {rid} failed: {_tb.format_exc()}", flush=True)

    # Apply the result against the LATEST persisted state. Skip the write
    # entirely if the resume is gone (deleted, or wiped by /api/reset).
    with lock:
        latest = _load_session_state(session_id) if session_id else state
        target = next(
            (r for r in (latest.get("resumes") or []) if r.get("id") == rid),
            None,
        )
        if target is None:
            print(
                f"[resume extraction] {rid} dropped — resume no longer in session "
                f"(reset or deletion during extraction)",
                flush=True,
            )
            # Also clear the stale extracting flag from the in-memory state
            # the spawner is still holding, in case it's still consulted.
            ids = state.get("extracting_ids")
            if isinstance(ids, set):
                ids.discard(rid)
            return

        if extraction_result is not None:
            target["profile"] = extraction_result
            target["updated_at"] = datetime.now().isoformat()
            target.pop("extract_error", None)
            # Re-render the preview PDF now that we have a structured
            # profile — the post-extraction render produces a much nicer
            # layout (sectioned, bulleted, with skills) than the raw-text
            # block we generated at upload time.
            try:
                target["preview_pdf_path"] = _render_preview_pdf(target)
            except Exception as exc:
                print(f"[preview pdf] post-extraction re-render failed: "
                      f"{type(exc).__name__}: {exc}", flush=True)
            # When the freshly-extracted resume is the primary, push every
            # scalar derived from it (profile, resume_text, latex_source,
            # filename, done flag) so the Profile page reflects the new
            # data without the user having to click anything.
            if target.get("primary"):
                latest["resume_text"] = target["text"]
                latest["latex_source"] = target.get("latex_source")
                latest["resume_filename"] = target["filename"]
                latest["profile"] = extraction_result
                done = latest.get("done")
                if not isinstance(done, set):
                    done = set(done or [])
                done.add(1)
                latest["done"] = done
                # Fresh extraction on the primary resume — refresh ALL the
                # Re-derive search prefs from the freshly-extracted profile.
                # force_refresh=True because a fresh primary upload (or a
                # Re-scan click) signals "this resume is now the truth" —
                # leaving stale prefs from a prior primary in place produces
                # job postings mis-aimed at the old persona.
                _apply_profile_search_prefs(
                    latest, extraction_result, force_refresh=True
                )
                # Persist per-(user, job) scores against the freshly extracted
                # profile so /api/jobs/feed can ORDER BY score DESC. Runs in
                # a daemon thread; coalesced per-user via `_user_scoring_lock`
                # so a flurry of re-extractions doesn't pile up workers.
                uid = (latest.get("user") or {}).get("id")
                if uid and extraction_result:
                    # Wipe the lazy in-memory score cache for this user so
                    # the next /api/jobs/score-batch poll recomputes against
                    # the new profile instead of serving the 1-hour-stale
                    # row from the previous extraction. Without this, the
                    # SPA showed "Re-scan complete!" but the JD scores were
                    # frozen for an hour because the cache wins over the
                    # fresh user_job_scores write below.
                    evicted = _score_cache_invalidate_user(uid)
                    if evicted:
                        print(
                            f"[score-cache] evicted {evicted} entries for user={uid[:8]} "
                            f"after fresh extraction",
                            flush=True,
                        )
                    _kick_user_scoring(uid, extraction_result, only_new=False)
        elif extraction_error is not None:
            target["extract_error"] = extraction_error

        # Drop our id from extracting_ids in the latest state.
        ids_latest = latest.get("extracting_ids")
        if not isinstance(ids_latest, set):
            ids_latest = set(ids_latest or [])
        ids_latest.discard(rid)
        latest["extracting_ids"] = ids_latest

        _save_bound_state(latest, session_id)

        # Mirror the change into the spawner's in-memory dict so any
        # subsequent reads from this thread (or an unfortunate cached
        # reference) see consistent values. Safe: we hold the lock.
        for k in ("resumes", "resume_text", "latex_source", "resume_filename",
                  "profile", "done", "extracting_ids",
                  "job_titles", "location", "education_filter",
                  "experience_levels"):
            if k in latest:
                state[k] = latest[k]


# ── Provider factory ──────────────────────────────────────────────────────────

# Test seam: when set, _make_provider returns this object instead of constructing
# a real provider. Lets tests inject a FakeProvider without monkeypatching the
# function or pre-populating session state. None in production.
_PROVIDER_OVERRIDE = None


def _make_provider():
    if _PROVIDER_OVERRIDE is not None:
        return _PROVIDER_OVERRIDE
    from pipeline.providers import DemoProvider, AnthropicProvider, OllamaProvider
    mode = _S.get("mode", "ollama")
    if mode == "demo":
        return DemoProvider()
    if mode == "ollama":
        return OllamaProvider(model=_S.get("ollama_model", DEFAULT_OLLAMA_MODEL))
    # CLI uses OAuth keychain — no API key needed.
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
    # Belt-and-suspenders gate: defends against `mode='anthropic'` sneaking past
    # the /api/config check (e.g. saved while on Pro, then downgraded).
    # Non-permitted users get bounced with `plan_required` so the SPA can show
    # an upgrade prompt.
    if state and state.get("mode") == "anthropic":
        user = (state.get("user") or {})
        if not _can_use_claude(user):
            yield _sse({
                "type": "error",
                "phase": phase,
                "message": "Claude is a Pro-tier feature. Upgrade to use Claude.",
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
    _phase_logs[(session_id, phase)] = log_q
    lock = _session_lock(session_id)

    # Open the server-side progress buffer BEFORE spawning the worker so
    # /api/state.running_phases reports the in-flight phase the moment the
    # SSE generator yields its first event. The worker's finally always
    # closes the buffer (even if the SSE client navigates away mid-phase).
    _phase_progress_open(session_id, phase)

    class _ProgressLogCapture(_LogCapture):
        """`_LogCapture` plus a fan-out into the per-session progress
        buffer. Lets a returning user see the same recent log lines the
        original SSE client saw — even if they closed the tab and the
        SSE generator received GeneratorExit before the worker finished.
        """
        def write(self, text):
            n = super().write(text)
            if text and text.strip():
                _phase_progress_log(session_id, phase, text)
            return n

    def _worker():
        import sys
        old_stdout = sys.stdout
        try:
            if state is not None:
                _bind_thread_state(state, session_id)
            sys.stdout = _ProgressLogCapture(old_stdout, log_q)
            val = fn()
            result_q.put(("ok", val))
        except Exception as exc:
            result_q.put(("err", str(exc)))
        finally:
            sys.stdout = old_stdout
            # Always tear down the progress buffer in the WORKER's finally,
            # not the generator's — the worker outlives the SSE connection
            # when the user navigates away mid-phase. Closing this on the
            # generator's GeneratorExit would clear the buffer while the
            # work is still in progress, defeating the whole point.
            _phase_progress_close(session_id, phase)

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
        # Client disconnected mid-stream. The worker is a daemon thread and
        # finishes on its own; we don't have a cooperative-cancel hook into
        # the phase functions, so just let it run to completion. The
        # progress buffer + final state save still happen via _worker's
        # finally block, so a returning user sees the same outcome.
        raise
    finally:
        _release_phase(session_id, phase)
        _phase_logs.pop((session_id, phase), None)


def _build_tailored_item(*, job: dict, tailored: dict, resume_ref: str,
                          profile: dict, score=None,
                          status: str = "Tailored", notes: str = "",
                          files: dict | None = None) -> dict:
    """Build the SPA-facing item dict for a single tailored resume.

    Handles both legacy v1 dicts and v2 (TailoredResume) dicts. v2 entries
    include the full structured resume + diff markers under ``v2`` so the
    SPA can render the green-highlighted preview iframe.

    `files` is the {tex, pdf, docx, html_preview, base, template_id, ...}
    dict returned by `_save_tailored_resume`. When set, we expose
    ``html_preview_url`` + template metadata to the client.
    """
    profile = profile or {}
    tailored = tailored or {}
    files = files or {}
    is_v2 = tailored.get("schema_version") == 2

    profile_skills_lower = {
        str(s).lower().strip()
        for s in (profile.get("top_hard_skills") or []) if s and str(s).strip()
    }

    # ── Flatten skills + experience bullets into the legacy SPA shape ───────
    if is_v2:
        skills: list[str] = []
        for cat in tailored.get("skills") or []:
            for it in cat.get("items") or []:
                if it.get("text"):
                    skills.append(str(it["text"]))
        bullets_full: list[dict] = []
        for r in tailored.get("experience") or []:
            blist = [b.get("text", "") for b in (r.get("bullets") or []) if b.get("text")]
            if r.get("title") or blist:
                title = r.get("title") or ""
                if r.get("company"):
                    title = f"{title} — {r['company']}" if title else r["company"]
                bullets_full.append({"role": title, "bullets": blist})
    else:
        skills_raw = tailored.get("skills_reordered") or []
        skills = [s.get("skill", str(s)) if isinstance(s, dict) else str(s) for s in skills_raw]
        bullets_full = []
        for entry in (tailored.get("experience_bullets") or []):
            if not isinstance(entry, dict):
                continue
            bullets = [str(b) for b in (entry.get("bullets") or []) if str(b).strip()]
            if entry.get("role") or bullets:
                bullets_full.append({"role": entry.get("role", ""), "bullets": bullets})

    reqs = [str(r).strip() for r in (job.get("requirements") or []) if r and str(r).strip()]
    keyword_comparison = []
    for req in reqs[:14]:
        req_lower = req.lower()
        on_resume = any(
            s == req_lower or s in req_lower or req_lower in s
            for s in profile_skills_lower
        )
        keyword_comparison.append({
            "keyword": req,
            "on_resume": bool(on_resume),
            "action": "keep" if on_resume else "add",
        })

    ats_before = int(tailored.get("ats_score_before") or 0)
    ats_after = int(tailored.get("ats_score_after") or 0)
    item: dict = {
        "co":               job.get("company", "") or job.get("co", ""),
        "role":             job.get("title", "")   or job.get("role", ""),
        "loc":              job.get("location", "") or job.get("loc", ""),
        "score":            score if score is not None else job.get("score", 0),
        "status":           status,
        "notes":            notes,
        "resume_file":      resume_ref or "",
        "ats_before":       ats_before,
        "ats_after":        ats_after,
        "ats_delta":        ats_after - ats_before,
        "ats_gaps":         [
            s.get("skill", str(s)) if isinstance(s, dict) else str(s)
            for s in (tailored.get("ats_keywords_missing") or [])
        ][:10],
        "skills":           skills[:18],
        "experience_bullets": bullets_full,
        "keyword_comparison": keyword_comparison,
        "section_order":    list(tailored.get("section_order") or []),
        "has_cl":           bool(tailored.get("cover_letter")),
        "cover_letter":     str(tailored.get("cover_letter") or "")[:2000],
    }

    if is_v2:
        item["schema_version"] = 2
        item["v2"] = {
            "name":     tailored.get("name", ""),
            "email":    tailored.get("email", ""),
            "phone":    tailored.get("phone", ""),
            "linkedin": tailored.get("linkedin", ""),
            "github":   tailored.get("github", ""),
            "location": tailored.get("location", ""),
            "website":  tailored.get("website", ""),
            "summary":  tailored.get("summary"),
            "skills":   tailored.get("skills") or [],
            "experience": tailored.get("experience") or [],
            "projects":   tailored.get("projects") or [],
            "education":  tailored.get("education") or [],
            "ats_keywords_added": tailored.get("ats_keywords_added") or [],
        }
        for bucket in ("awards", "certifications", "publications", "activities",
                       "leadership", "volunteer", "coursework", "languages",
                       "custom_sections"):
            if tailored.get(bucket):
                item["v2"][bucket] = tailored[bucket]

    if files.get("html_preview") and files.get("base") is not None:
        # files["html_preview"] is just a filename; the resume_ref already
        # contains the session-relative path prefix (sessions/{sid}/) when
        # set, so the URL reuses that prefix.
        prefix = ""
        if resume_ref:
            # resume_ref like "sessions/abc/Foo_Resume_X.pdf" → "sessions/abc/"
            slash = resume_ref.rfind("/")
            if slash >= 0:
                prefix = resume_ref[: slash + 1]
        item["html_preview_url"] = f"/output/{prefix}{files['html_preview']}"
        # Clean / final-version downloads — generated alongside the diff artifacts
        # so the user can attach the all-black PDF to applications without the
        # green diff highlights bleeding through. Kept separate from the diff
        # PDF served via `resume_file` so the preview iframe still shows changes.
        if files.get("pdf_final"):
            item["final_pdf_url"] = f"/output/{prefix}{files['pdf_final']}"
        if files.get("docx_final"):
            item["final_docx_url"] = f"/output/{prefix}{files['docx_final']}"
        if files.get("tex_final"):
            item["final_tex_url"] = f"/output/{prefix}{files['tex_final']}"
    if files.get("template_id"):
        item["template_id"] = files["template_id"]
        if files.get("template_confidence") is not None:
            item["template_confidence"] = files["template_confidence"]
    if files.get("docx"):
        item["docx_file"] = files["docx"]

    return item


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
        profile = _S.get("profile") or {}
        items = []
        for a in apps:
            jk = a.get("id") or a.get("title", "")
            td = tmap.get(jk, {})
            items.append(_build_tailored_item(
                job=td.get("job") or a,
                tailored=td.get("tailored") or {},
                resume_ref=a.get("resume_version", ""),
                profile=profile,
                score=a.get("score", 0),
                status=a.get("status", ""),
                notes=a.get("notes", ""),
            ))
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
        # ``val`` is the dict returned by phase6_update_tracker — month +
        # columns + rows + summary. We pass it through verbatim so the SPA
        # can render the spreadsheet inline. ``tracker_path`` is dropped
        # from the wire payload because the web flow doesn't write a file.
        if isinstance(val, dict):
            return {
                "month":   val.get("month") or "",
                "columns": val.get("columns") or [],
                "rows":    val.get("rows") or [],
                "summary": val.get("summary") or {},
            }
        # Back-compat shim: legacy phase 6 returned a Path. Surface an
        # empty tracker rather than crashing the SSE serializer.
        return {"month": "", "columns": [], "rows": [], "summary": {}}
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

def _can_use_claude(auth_user: dict | None) -> bool:
    """Single source of truth for `mode='anthropic'` access.

    Returns True iff the user is permitted AND the CLI subprocess is reachable.
    Used by:
      • _load_session_state (coerce non-permitted off anthropic)
      • update_config (402 plan_required)
      • _run_phase_sse (reject early in SSE start frame)
      • resume_tailor (reject early in POST handler)

    Permission = (is_developer OR plan_tier=='pro') AND CLI healthy.
    Health is toggled by app startup hook + 5-min ticker (Task 12).
    """
    from pipeline.providers import _CLI_HEALTHY
    if not _CLI_HEALTHY:
        return False
    if not auth_user:
        return False
    if auth_user.get("is_developer"):
        return True
    # Default to "pro" during the everyone-is-Pro testing phase. New signups
    # are inserted with plan_tier='pro' (session_store.create_user); a missing
    # field here means legacy/in-flight data, which should be treated as Pro.
    return (auth_user.get("plan_tier") or "pro").lower() == "pro"


def _require_auth_user(request: Request) -> dict:
    """Reject unauthenticated callers. Prevents ghost profiles from anonymous uploads."""
    auth_user = _auth_token_lookup(request.cookies.get(_AUTH_COOKIE, ""))
    if not auth_user:
        raise HTTPException(401, "Sign in required")
    return auth_user


_PREVIEWABLE_RESUME_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".tex"}


def _primary_format_profile() -> dict | None:
    """Cached layout fingerprint of the primary resume (or None if absent).
    Used by every tailoring path so generated PDFs mirror the user's source
    layout — column count, font sizes, accent."""
    pr = _get_primary_resume()
    if not pr:
        return None
    fp = pr.get("format_profile")
    return fp if isinstance(fp, dict) and fp else None


def _detect_pdf_format_profile(suffix: str, content: bytes) -> dict:
    """Best-effort PDF layout fingerprint.  Only runs for .pdf uploads;
    other suffixes return ``{}`` (the renderer's defaults will apply)."""
    if (suffix or "").lower() != ".pdf":
        return {}
    import tempfile
    try:
        from pipeline.pdf_format import detect_format_profile
    except ImportError:
        return {}
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            return detect_format_profile(tmp_path) or {}
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"[pdf format] fingerprint failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return {}


def _render_preview_pdf(record: dict) -> str | None:
    """Render a polished PDF preview of the resume, persist it under
    ``uploads/{id}_preview.pdf``, and return the OUTPUT_DIR-relative path.

    Strategy by source format:
      • .tex (or any record with a ``latex_source``) → run pdflatex on the
        actual LaTeX so the preview shows the user's real layout, fonts,
        and macros. Falls through to reportlab only if pdflatex isn't on
        PATH or the compile fails.
      • everything else (.txt, .md, .docx, plain-text uploads) → reportlab
        render of the extracted profile + plaintext block.

    Real PDF uploads keep their original (the iframe prefers ``original_url``
    when it's a PDF); this helper is the fallback for every other suffix.
    Returns ``None`` if no backend can produce a PDF.
    """
    try:
        rid = record.get("id")
        if not rid:
            return None
        dest_dir = _session_output_dir() / "uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{rid}_preview.pdf"

        latex_source = record.get("latex_source")
        if latex_source:
            try:
                from pipeline.latex import compile_latex_to_pdf
                if compile_latex_to_pdf(latex_source, dest) and dest.exists():
                    return dest.resolve().relative_to(OUTPUT_DIR.resolve()).as_posix()
            except Exception as exc:
                print(f"[preview pdf] LaTeX compile failed for {rid!r}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
            # pdflatex unavailable or compile errored — fall through to the
            # reportlab path so the user still sees something.

        # Use whatever profile we have. Before extraction completes the
        # profile is empty — _render_resume_pdf_reportlab handles that by
        # falling back to the raw resume text block.
        profile = record.get("profile") or {}
        text = record.get("text") or ""
        if not text.strip() and not profile:
            return None
        ok = _render_resume_pdf_reportlab(
            dest, profile, tailored={}, job={}, resume_text=text,
            format_profile=record.get("format_profile") or None,
        )
        if not ok:
            return None
        return dest.resolve().relative_to(OUTPUT_DIR.resolve()).as_posix()
    except Exception as exc:
        print(f"[preview pdf] {record.get('id')!r} render failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _persist_uploaded_resume(record_id: str, suffix: str, content: bytes) -> str | None:
    """Save the uploaded bytes under output/sessions/{sid}/uploads/{id}{ext}
    and return the path relative to OUTPUT_DIR (posix style).

    Returns None if the suffix isn't in the previewable set (we don't store
    binaries we can't safely serve back via /output/{path}, which has its own
    suffix allowlist). The filename uses the record id rather than the
    user-supplied filename to neutralise traversal / collision concerns.
    """
    s = (suffix or "").lower()
    if s not in _PREVIEWABLE_RESUME_SUFFIXES:
        return None
    dest_dir = _session_output_dir() / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{record_id}{s}"
    dest.write_bytes(content)
    try:
        return dest.resolve().relative_to(OUTPUT_DIR.resolve()).as_posix()
    except ValueError:
        # Defensive: if relative-to fails (different drive on Windows), drop
        # the persisted copy and fall back to text-only preview.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return None


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
    # Persist the original bytes AFTER record id is known so the file name
    # binds to the record. Stored under the bound session's output dir so
    # /output/sessions/{sid}/... auth-gating already covers access control.
    record["original_path"] = _persist_uploaded_resume(record["id"], suffix, content)
    # Capture the source PDF's visual fingerprint so any subsequent render
    # (preview + every tailored output) can mirror the user's actual
    # layout — column count, font sizes, accent color. Only meaningful
    # for .pdf uploads; everything else returns {} and the renderer keeps
    # its built-in defaults.
    record["format_profile"] = _detect_pdf_format_profile(suffix, content)
    # Render a polished preview PDF immediately. For .pdf uploads the iframe
    # prefers `original_url` so this is just an extra fallback; for .txt /
    # .docx / .tex / .md it's THE preview the user sees on the Resume page.
    record["preview_pdf_path"] = _render_preview_pdf(record)
    resumes = _S.setdefault("resumes", [])

    # First upload → becomes primary; subsequent uploads are non-primary
    is_first = len(resumes) == 0
    record["primary"] = is_first
    resumes.append(record)

    if is_first:
        _sync_primary_scalars(record)

    _kick_extraction(record)
    return {"ok": True, "filename": fname, "length": len(text), "id": record["id"], "extracting": True}

@app.post("/api/resume/demo")
def load_demo_resume(request: Request):
    _require_auth_user(request)
    text = _build_demo_resume()
    record = _new_resume_record("demo_resume.txt", text, None)
    record["preview_pdf_path"] = _render_preview_pdf(record)
    resumes = _S.setdefault("resumes", [])
    is_first = len(resumes) == 0
    record["primary"] = is_first
    resumes.append(record)
    if is_first:
        _sync_primary_scalars(record)
    _kick_extraction(record)
    return {"ok": True, "filename": "demo_resume.txt", "id": record["id"], "extracting": True}

# ── Config ────────────────────────────────────────────────────────────────────

@app.post("/api/config")
async def update_config(req: Request):
    auth_user = _require_auth_user(req)
    body = await req.json()
    # Mode whitelist — `demo` was retired in favor of always-on Ollama. The
    # `anthropic` value is still accepted in the schema so developers can
    # exercise the in-progress Claude integration, but customer access is
    # gated below ("coming soon" until Claude launches publicly).
    if body.get("mode") not in (None, "ollama", "anthropic"):
        raise HTTPException(400, f"Invalid mode: {body.get('mode')!r} (expected 'ollama' or 'anthropic')")
    plan = (auth_user or {}).get("plan_tier") or "pro"  # everyone-is-Pro testing phase
    is_dev = _is_underlying_dev_request(req)
    # Claude is a Pro-tier feature (or dev). Non-permitted users get a 402
    # plan_required so the SPA can show an upgrade prompt.
    if body.get("mode") == "anthropic" and not _can_use_claude(auth_user):
        return JSONResponse(
            {"ok": False,
             "error": "Claude is a Pro-tier feature. Upgrade to use Claude.",
             "code": "plan_required"},
            status_code=402,
        )
    # Cloud Ollama models (Ollama Turbo proxied through the Pi) require Pro.
    # Local Ollama models stay free.
    if (body.get("ollama_model")
            and str(body["ollama_model"]).lower().endswith("cloud")
            and plan != "pro" and not is_dev):
        return JSONResponse(
            {"ok": False, "error": "Cloud models require the Pro plan", "code": "plan_required"},
            status_code=402,
        )
    for k in (
        "mode", "ollama_model",
        "threshold", "job_titles", "location",
        "max_apps", "max_scrape_jobs", "days_old",
        "cover_letter", "blacklist", "whitelist",
        "experience_levels", "education_filter",
        "include_unknown_education", "include_unknown_experience",
        "citizenship_filter",
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
    """Prefer real Phase-3 scoring when present; fall back to the live index.

    The displayed `jobs` list ALWAYS surfaces the top-N by score regardless of
    threshold/filter status. With sparse JD bodies in the index, threshold=75
    can produce zero passes — leaving the user staring at an empty Dashboard.
    Filling the list from passed→manual→below→filtered means the page is
    never empty when the pipeline actually produced output. Status badges
    (auto/manual/below/filtered) still come from filter_status so the UI
    can color the cards correctly.
    """
    if scored:
        # Sort the whole scored list, but bias passing jobs to the top by status.
        rank_order = {"passed": 0, "below_threshold": 1}
        def _rank_key(j):
            fs = j.get("filter_status") or ""
            bucket = rank_order.get(fs, 2 if fs.startswith("filtered_") else 3)
            return (bucket, -(j.get("score") or 0))
        ordered = sorted(scored, key=_rank_key)
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
                    "reason": j.get("filter_reason", "") or j.get("reasoning", ""),
                }
                for j in ordered[:30]
            ],
        }
    return _scored_summary_from_index(profile)


@app.get("/api/state")
def get_state(request: Request):

    # Dev mode now requires an authenticated user with the is_developer flag.
    is_dev = _is_dev_request(request)
    _raw_auth_cookie = request.cookies.get(_AUTH_COOKIE, "")
    auth_user = _auth_token_lookup(_raw_auth_cookie)

    # One fresh DB read per /api/state poll when authenticated. Reused for
    # both the dev_simulating check below AND the billing customer flag,
    # so we don't pay for two SELECTs per poll.
    fresh_user = None
    if auth_user and auth_user.get("id") and _user_store is not None:
        try:
            fresh_user = _user_store.get_user_by_id(auth_user["id"])
        except Exception:
            fresh_user = None

    # dev_simulating: the underlying user IS a developer but is currently viewing
    # the app as a customer via the force_customer_mode toggle. Lets the frontend
    # show an "Exit customer mode" pill so the dev isn't trapped without nav.
    dev_simulating = False
    if _S.get("force_customer_mode"):
        if fresh_user and bool(fresh_user.get("is_developer")):
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
        "ollama_model": _S.get("ollama_model", DEFAULT_OLLAMA_MODEL),
        "threshold": _S.get("threshold", 75),
        "job_titles": _S.get("job_titles", ""),
        "location": _S.get("location", ""),
        "max_apps": _S.get("max_apps", 10),
        "max_scrape_jobs": _S.get("max_scrape_jobs", 50),
        "days_old": _S.get("days_old", 30),
        "cover_letter": _S.get("cover_letter", False),
        "blacklist": _S.get("blacklist", ""),
        "whitelist": _S.get("whitelist", ""),
        "experience_levels": _S.get("experience_levels", []),
        "education_filter": _S.get("education_filter", []),
        "include_unknown_education": _S.get("include_unknown_education", True),
        "include_unknown_experience": _S.get("include_unknown_experience", True),
        "citizenship_filter": _S.get("citizenship_filter", "all"),
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
        # Phase 6 / 7 in-page artifacts. ``tracker_data`` carries the full
        # spreadsheet payload (columns + rows + summary) so the SPA can render
        # it inline without a file download. ``report`` is the markdown text
        # generated by the LLM in Phase 7 — also rendered inline via the
        # frontend Markdown component.
        "tracker_data": _S.get("tracker_data"),
        "report":       _S.get("report") or "",
        "output_files": files,
        # Auth / session
        "is_dev": is_dev,
        "dev_simulating": dev_simulating,
        "runtime": dict(_RUNTIME),

        # Plan tier (mirrors is_developer end-to-end). Free is the default for any
        # unauthenticated visitor or new account; the Stripe webhook flips it to
        # 'pro' when a subscription becomes active.
        "plan_tier": (auth_user or {}).get("plan_tier", "free"),
        "is_pro": (auth_user or {}).get("plan_tier") == "pro",

        # Billing UI hints. ``billing_configured`` lets the SPA hide the upgrade
        # button entirely when the server has no Stripe key set (e.g. self-host
        # deployments). ``has_billing_customer`` gates the Manage-subscription
        # button for users who haven't yet completed Checkout.
        "billing_configured": bool(_STRIPE_PRICE_ID_PRO_MONTHLY) and _stripe_billing.is_configured(),
        "has_billing_customer": bool((fresh_user or {}).get("stripe_customer_id")),

        # Only return a user when the auth token in the request cookie is valid.
        # Falling back to _S.get("user") (session state) lets stale sessions appear
        # logged-in even after the token is gone, causing silent 401s on every
        # write endpoint while the frontend shows no sign-in option.
        "user": auth_user,
        "resumes": [_serialize_resume(r) for r in (_S.get("resumes") or [])],

        # UI state
        "liked_ids": list(_S.get("liked_ids") or []),
        "hidden_ids": list(_S.get("hidden_ids") or []),
        "dev_tweaks": _S.get("dev_tweaks") or {},

        # Phases currently executing on the server. Lets the Agent page
        # rehydrate "running" state when the user navigates away and back —
        # without this, the local React `running` flag is lost on unmount
        # and the UI lies about pipeline activity.
        "running_phases": _phase_progress_snapshot(_S.session_id()),
    }

@app.post("/api/reset")
def reset_state(request: Request):
    _require_auth_user(request)
    sid = _S.session_id()
    # Preserve auth + provider/UI prefs so the user stays logged in and configured
    preserved = {
        k: _S.get(k) for k in (
            "user", "dev_tweaks", "mode", "ollama_model", "light_mode",
            "force_customer_mode",
        )
    }

    # Hold the per-session lock for the entire clear+persist so a concurrent
    # extraction thread can't race in between (its end-of-run save reloads
    # state from the DB while holding this same lock, sees the cleared
    # resumes list, and bails — but only if our save lands first).
    with _session_lock(sid):
        state = _S.current()
        fresh = _default_state()
        state.clear()
        state.update(fresh)
        for k, v in preserved.items():
            if v is not None:
                state[k] = v
        # Persist the cleared state inline rather than waiting for the
        # post-response middleware save. The middleware save happens AFTER
        # the response is returned, which leaves a window where a polling
        # /api/state read can see the still-uncleared DB row.
        if sid:
            user = state.get("user") or {}
            if (user.get("id") or user.get("email")) and _session_store is not None:
                _session_store.save_state(sid, state)
            else:
                _memory_sessions[sid] = state

    # Wipe generated output files for this session (resumes, trackers, reports).
    # rmtree+mkdir is simpler than per-file unlink and tolerates Windows file
    # handles still in use mid-extraction.
    import shutil
    out_dir = _session_output_dir()
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Drop any persisted user_job_scores so the next resume upload computes
    # against a clean slate. The /api/jobs/feed endpoint LEFT JOINs this
    # table and would otherwise keep sorting by the now-stale previous-
    # profile match. Best-effort; the table may not exist on older deploys.
    auth_user = preserved.get("user") or {}
    uid = auth_user.get("id") if isinstance(auth_user, dict) else None
    if uid and _session_store is not None:
        try:
            from pipeline import user_scoring as _us
            with _session_store.connect() as conn:
                _us.delete_user_scores(conn, uid)
        except Exception as exc:
            print(f"[reset] delete_user_scores failed for {uid[:8]}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        # Also wipe the lazy in-memory score cache so a stale 1-hour-old
        # row doesn't outlive the persisted-scores deletion above.
        _score_cache_invalidate_user(uid)
    return {"ok": True}


# ── Pipeline reset (non-destructive) ─────────────────────────────────────────
#
# Distinct from `/api/reset` (Settings page → "Reset all data") which is the
# scorched-earth flow that wipes the resume, profile, and every generated
# file. `/api/pipeline/reset` only clears the run-specific outputs (jobs,
# scoring, applications, tracker, report) so the user can start a fresh
# pipeline without re-uploading their resume or losing the documents they've
# already produced. Generated files are PRESERVED — the user manages them
# from the Documents page.

# Phase-result keys that get wiped on a pipeline reset. Phase 1 (profile
# extraction) is intentionally NOT in this list — the profile is upstream of
# the pipeline and a reset shouldn't force the user to wait for re-extraction.
_PIPELINE_RESULT_KEYS = (
    "jobs", "scored", "applications",
    "tracker_path", "tracker_data", "report",
)


@app.post("/api/pipeline/reset")
def reset_pipeline(request: Request):
    """Clear pipeline run data only.

    Preserves: resume, profile, settings, generated documents, auth, and
    all UI state (liked/hidden ids, dev tweaks, light mode). Use this when
    the user wants to re-run discovery → scoring → tailoring against a
    fresh starting point without losing their account or files.

    Refuses with 409 while a phase is currently executing — clearing state
    out from under a running worker thread would corrupt the result the
    worker is about to write.
    """
    _require_auth_user(request)
    sid = _S.session_id()

    progress = _phase_progress_snapshot(sid)
    if progress:
        running = ", ".join(f"phase {p['phase']}" for p in progress)
        raise HTTPException(
            409,
            f"Cannot reset while {running} is running. Wait for it to finish or reload the page.",
        )

    with _session_lock(sid):
        for k in _PIPELINE_RESULT_KEYS:
            _S[k] = None
        _S["tailored_map"] = {}

        # Drop phases 2..7 from done/error/elapsed. Keep phase 1 because the
        # profile is upstream of the pipeline and we never invalidated it.
        done = _S.get("done")
        if not isinstance(done, set):
            done = set(done or [])
        for p in range(2, 8):
            done.discard(p)
        _S["done"] = done

        for bucket_key in ("error", "elapsed"):
            bucket = _S.get(bucket_key) or {}
            for p in list(bucket.keys()):
                try:
                    if int(p) >= 2:
                        bucket.pop(p, None)
                except (TypeError, ValueError):
                    continue
            _S[bucket_key] = bucket

        # Arm Phase 2 to force a fresh ingestion tick on its next run.
        # Without this, "Reset run" would clear the cached jobs but the NEXT
        # phase-2 search would hit the same persistent SQLite index with the
        # same query and return identical top-N results. The flag is
        # consumed (single-shot) inside /api/phase/2/run.
        _S["force_phase2_next_run"] = True

        # Persist inline — same reasoning as /api/reset: avoid the race
        # between the post-response middleware save and a /api/state poll.
        if sid:
            state = _S.current()
            user = state.get("user") or {}
            if (user.get("id") or user.get("email")) and _session_store is not None:
                _session_store.save_state(sid, state)
            else:
                _memory_sessions[sid] = state

    # Wipe the legacy server-wide phase-2 cache files. These pre-date the
    # SQLite job index but still get consulted by some code paths; leaving
    # them in place means a reset can return the same stale snapshot. Best-
    # effort — missing files are not an error.
    for cache in (RESOURCES_DIR / "sample_jobs_quick.json",
                  RESOURCES_DIR / "sample_jobs_deep.json"):
        cache.unlink(missing_ok=True)

    return {"ok": True, "preserved": "resume, profile, settings, documents"}


# ── Documents API ────────────────────────────────────────────────────────────
#
# The Documents page lets users manage every artifact the pipeline produces
# — tailored resumes (.tex / .pdf), trackers (.xlsx), run reports (.md),
# cover letters (.txt), etc. The static /output/sessions/{sid}/{name} route
# already handles downloads (with auth + session-scoping); these endpoints
# add list / edit / rename / delete on top.

# Suffixes the Documents page is allowed to surface and mutate. Mirrors the
# allowlist on /output/{path} but excludes types that are inputs (we never
# manage uploaded resumes here — that's the Resume page) or that aren't
# user-facing (databases, lock files).
_DOCUMENT_SUFFIXES = {
    ".pdf", ".tex", ".docx", ".doc", ".txt", ".md",
    ".xlsx", ".xls", ".csv",
}
# Subset of the above that the SPA can edit in-place via a textarea. Binary
# formats (PDF / Excel / Word) are download-or-delete-only.
_DOCUMENT_EDITABLE_SUFFIXES = {".tex", ".txt", ".md", ".csv"}
# Sub-folders inside the session output dir that the Documents page should
# NOT traverse (uploads/ is the originals stash for the Resume page).
_DOCUMENT_HIDDEN_DIRS = {"uploads"}


def _classify_document(name: str) -> str:
    """Group a filename into a UI-facing kind. The classifier is
    deliberately tolerant — anything we can't confidently bucket lands
    in 'other' and the SPA renders it under a catch-all section."""
    n = name.lower()
    if n.endswith((".xlsx", ".xls", ".csv")) and "tracker" in n:
        return "tracker"
    flat = n.replace("_", "").replace("-", "")
    if "coverletter" in flat:
        return "cover_letter"
    if n.endswith((".tex", ".pdf")) and ("_resume_" in n or n.startswith("resume_")):
        return "resume"
    if n.endswith(".md") and ("report" in n or "run-report" in n):
        return "report"
    return "other"


def _safe_document_path(name: str, *, must_exist: bool = True) -> Path:
    """Resolve `name` against the bound session's output dir and refuse
    anything that escapes it, or that isn't on the document suffix
    allowlist. Pass `must_exist=False` for the destination of a rename
    (the file isn't there yet — that's the point)."""
    if not name or not isinstance(name, str):
        raise HTTPException(400, "Document name is required")
    # Reject path components / hidden files / traversal up front so a
    # malicious name can't hit the resolve() codepath.
    if "/" in name or "\\" in name or name.startswith(".") or ".." in name:
        raise HTTPException(400, "Invalid document name")
    out_dir = _session_output_dir().resolve()
    target = (out_dir / name).resolve()
    try:
        target.relative_to(out_dir)
    except ValueError:
        raise HTTPException(400, "Document is outside the session directory") from None
    if target.suffix.lower() not in _DOCUMENT_SUFFIXES:
        raise HTTPException(400, f"Document type {target.suffix!r} is not allowed")
    # Guard against accidentally targeting one of our hidden sub-dirs.
    parts = {p.lower() for p in target.relative_to(out_dir).parts[:-1]}
    if parts & _DOCUMENT_HIDDEN_DIRS:
        raise HTTPException(400, "Cannot manage documents inside this folder")
    if must_exist and (not target.exists() or not target.is_file()):
        raise HTTPException(404, "Document not found")
    return target


def _serialize_document(p: Path) -> dict:
    stat = p.stat()
    return {
        "name":     p.name,
        "kind":     _classify_document(p.name),
        "size_kb":  round(stat.st_size / 1024, 1),
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "url":      _output_url(p),
        "ext":      p.suffix.lstrip(".").lower(),
        "editable": p.suffix.lower() in _DOCUMENT_EDITABLE_SUFFIXES,
    }


@app.get("/api/documents")
def documents_list(request: Request):
    """List every artifact the pipeline has produced for the bound session.

    Excludes the `uploads/` sub-folder (those are originals managed by
    the Resume page) and any file whose suffix isn't on the allowlist.
    Sorted newest-first by mtime so freshly-tailored resumes lead.
    """
    _require_auth_user(request)
    out_dir = _session_output_dir()
    items: list[dict] = []
    if out_dir.exists():
        for p in out_dir.iterdir():
            if not p.is_file():
                continue
            if p.name.startswith("."):
                continue
            if p.suffix.lower() not in _DOCUMENT_SUFFIXES:
                continue
            try:
                items.append(_serialize_document(p))
            except OSError:
                continue
    items.sort(key=lambda d: d.get("modified") or "", reverse=True)
    return {"documents": items, "total": len(items)}


@app.get("/api/documents/{name}/content")
def document_content(name: str, request: Request):
    """Return the plaintext body of an editable document. 400s for binary
    formats — the SPA only offers an editor for the editable suffixes."""
    _require_auth_user(request)
    target = _safe_document_path(name)
    if target.suffix.lower() not in _DOCUMENT_EDITABLE_SUFFIXES:
        raise HTTPException(400, f"Cannot edit {target.suffix} files in-app")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(500, f"Could not read {target.name}: {exc}") from exc
    return {"name": target.name, "content": content,
            "editable": True, "ext": target.suffix.lstrip(".").lower()}


@app.post("/api/documents/{name}/content")
async def document_save_content(name: str, request: Request):
    """Persist edits to an editable document. The content is written
    atomically so a crashed write can't half-update the file."""
    _require_auth_user(request)
    body = await request.json()
    text = body.get("content")
    if not isinstance(text, str):
        raise HTTPException(400, "content must be a string")
    target = _safe_document_path(name)
    if target.suffix.lower() not in _DOCUMENT_EDITABLE_SUFFIXES:
        raise HTTPException(400, f"Cannot edit {target.suffix} files in-app")
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(500, f"Could not save {target.name}: {exc}") from exc
    return {"ok": True, "name": target.name,
            "modified": datetime.fromtimestamp(target.stat().st_mtime).isoformat(timespec="seconds")}


@app.post("/api/documents/{name}/rename")
async def document_rename(name: str, request: Request):
    """Rename a document within the session's output dir. Source must
    exist; destination must not. The new name is path-validated through
    the same guard as the source, so the rename can never escape the
    session directory or land on a forbidden suffix."""
    _require_auth_user(request)
    body = await request.json()
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "New name is required")
    src = _safe_document_path(name)
    dst = _safe_document_path(new_name, must_exist=False)
    if dst.exists():
        raise HTTPException(409, f"A document named {new_name!r} already exists")
    try:
        src.rename(dst)
    except OSError as exc:
        raise HTTPException(500, f"Could not rename: {exc}") from exc
    return {"ok": True, "name": dst.name}


@app.delete("/api/documents/{name}")
def document_delete(name: str, request: Request):
    """Permanently delete a document. The /output/sessions/{sid}/{name}
    download URL becomes 404 immediately — the SPA's Documents page
    just removes the row from its local state."""
    _require_auth_user(request)
    target = _safe_document_path(name)
    try:
        target.unlink()
    except OSError as exc:
        raise HTTPException(500, f"Could not delete {target.name}: {exc}") from exc
    return {"ok": True, "name": target.name}


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
            # Testing phase: empty/null plan_tier falls back to "pro" so a
            # row that somehow lost its value (e.g. legacy NULL after the
            # column was added) doesn't downgrade the user on login.
            "plan_tier": user.get("plan_tier") or "pro",
        }
        app_session_id = _switch_to_user_session(user, auth_user)
        token = secrets.token_urlsafe(32)
        _auth_token_save(token, auth_user)
    except Exception as exc:
        import traceback as _tb
        _tb.print_exc()
        return JSONResponse({"ok": False, "error": f"Login failed: {exc}"}, status_code=500)
    resp = JSONResponse({"ok": True, "user": {"email": user["email"]}})
    resp.set_cookie(_AUTH_COOKIE, token, httponly=True, samesite="lax", secure=_COOKIE_SECURE)
    resp.set_cookie(_STATE_COOKIE, app_session_id, httponly=True, samesite="lax", secure=_COOKIE_SECURE)
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
    if not _is_valid_email(email):
        return JSONResponse({"ok": False, "error": "Please enter a valid email address."})
    if _user_store is None:
        return JSONResponse({"ok": False, "error": "Auth store unavailable"})
    try:
        if _user_store.get_user_by_email(email):
            return JSONResponse({"ok": False, "error": "An account with this email already exists"})
        pw_hash = hash_password(password)
        user_id = _user_store.create_user(email, pw_hash)
        # Re-fetch from the DB so plan_tier reflects whatever the column
        # default is (currently 'pro' during the everyone-is-Pro testing
        # phase — see pipeline/migrations.py + session_store.create_user).
        # Hardcoding "free" here was a stale leftover that immediately
        # overrode the DB default and got cached into auth_tokens.
        new_user = _user_store.get_user_by_id(user_id) or {}
        auth_user = {
            "id": user_id,
            "email": email,
            "is_developer": bool(new_user.get("is_developer")),
            "plan_tier": new_user.get("plan_tier") or "pro",
        }
        app_session_id = _start_session({"user": auth_user}, user_id=user_id)
        token = secrets.token_urlsafe(32)
        _auth_token_save(token, auth_user)
    except Exception as exc:
        import traceback as _tb
        _tb.print_exc()
        return JSONResponse({"ok": False, "error": f"Signup failed: {exc}"}, status_code=500)
    resp = JSONResponse({"ok": True, "user": {"email": email}})
    resp.set_cookie(_AUTH_COOKIE, token, httponly=True, samesite="lax", secure=_COOKIE_SECURE)
    resp.set_cookie(_STATE_COOKIE, app_session_id, httponly=True, samesite="lax", secure=_COOKIE_SECURE)
    resp.headers[_SESSION_SWITCH_HEADER] = app_session_id
    return resp

@app.post("/api/auth/logout")
def auth_logout(request: Request):
    # Drop the bearer token from the DB. Failures here are non-fatal: even
    # if the row can't be deleted, we still want the response to clear the
    # cookies so the client side stops sending them.
    token = request.cookies.get(_AUTH_COOKIE, "")
    if token:
        try:
            _auth_token_delete(token)
        except Exception:
            import traceback as _tb
            _tb.print_exc()

    # Allocate a fresh anonymous session-id. If anything blows up in the
    # session layer we still respond OK with cleared cookies — the
    # primary contract of /api/auth/logout is "make the client signed out",
    # not "create a perfect new session".
    app_session_id = ""
    try:
        app_session_id = _start_session() or ""
    except Exception:
        import traceback as _tb
        _tb.print_exc()
        app_session_id = uuid.uuid4().hex

    resp = JSONResponse({"ok": True})
    # IMPORTANT: delete_cookie must mirror the path / samesite / secure /
    # httponly attributes the cookie was originally SET with. If they
    # don't match, browsers ignore the delete and keep the original
    # cookie alive — the user appears "still logged in" even after a 200.
    # We also explicitly overwrite with an expired value as a belt-and-
    # suspenders so older or non-spec-compliant clients still drop it.
    resp.delete_cookie(
        _AUTH_COOKIE, path="/", samesite="lax",
        secure=_COOKIE_SECURE, httponly=True,
    )
    resp.set_cookie(
        _AUTH_COOKIE, "", max_age=0, expires=0, path="/",
        samesite="lax", secure=_COOKIE_SECURE, httponly=True,
    )
    # Same defensive deletion of any dev-impersonation cookie so the user
    # can't accidentally remain in someone else's session post-logout.
    resp.delete_cookie(
        _DEV_IMPERSONATE_COOKIE, path="/", samesite="lax",
        secure=_COOKIE_SECURE, httponly=True,
    )
    resp.set_cookie(
        _STATE_COOKIE, app_session_id, httponly=True,
        samesite="lax", secure=_COOKIE_SECURE,
    )
    if app_session_id:
        resp.headers[_SESSION_SWITCH_HEADER] = app_session_id
    return resp

@app.get("/api/auth/google")
def auth_google(request: Request):
    try:
        redirect_uri = str(request.url_for("auth_google_callback"))
        url, state = get_google_auth_url(redirect_uri)
        _S["google_oauth_state"] = state
        # Persist explicitly: GET requests skip the post-response middleware
        # save (see session_state_middleware) so the OAuth state would
        # otherwise vanish before the callback can verify it.
        _save_bound_state(_S.current(), _S.session_id())
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
                # Re-fetch so `user.plan_tier` reflects the column default
                # ('pro' during the testing phase) instead of falling
                # through to the "free" hardcoded fallback below.
                user = _user_store.get_user_by_id(user_id) or {
                    "id": user_id, "email": email, "google_id": google_id,
                }
            auth_user = {
                "id": user["id"],
                "email": user["email"],
                "name": name,
                "is_developer": bool(user.get("is_developer")),
                "plan_tier": user.get("plan_tier") or "pro",
            }
            app_session_id = _switch_to_user_session(user, auth_user)
        else:
            auth_user = {"email": email, "name": name, "is_developer": False, "plan_tier": "pro"}
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
        resp.set_cookie(_AUTH_COOKIE, token, httponly=True, samesite="lax", secure=_COOKIE_SECURE)
        resp.set_cookie(_STATE_COOKIE, app_session_id, httponly=True, samesite="lax", secure=_COOKIE_SECURE)
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
def get_profile(request: Request):
    _require_auth_user(request)
    return _S.get("profile") or {}

@app.post("/api/profile")
async def save_profile(request: Request):
    _require_auth_user(request)
    body = await request.json()
    if _S.get("profile"):
        _S["profile"].update(body)
    else:
        _S["profile"] = body
    return {"ok": True}

@app.post("/api/profile/extract")
async def extract_profile(request: Request):
    _require_auth_user(request)
    body = await request.json()
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

    _kick_extraction(target, force=force)
    return {"ok": True, "extracting": True, "resume_id": target["id"]}


@app.get("/api/profile/diagnose")
def diagnose_profile(request: Request, id: str = ""):
    """Diagnostic: run the heuristic extractor on the active resume and
    return its raw output WITHOUT calling the LLM. Used to debug why a
    Profile-page section came back empty — hit this endpoint and look at
    each section to see exactly what the regex/parsing pass found.
    """
    _require_auth_user(request)
    target = _get_resume_by_id(id) if id else _get_primary_resume()
    if not target:
        raise HTTPException(400, "No resume loaded")
    text = target.get("text") or ""
    if not text.strip():
        return {
            "ok": False,
            "error": "Resume preview text is empty — PDF extraction may have failed.",
            "filename": target.get("filename", ""),
            "text_length": 0,
        }

    from pipeline.profile_extractor import scan_profile, heuristic_summary
    from pipeline.providers import DemoProvider

    p = scan_profile(text)
    sections = DemoProvider._split_sections(text)
    section_summary = {
        k: {"line_count": len(v), "preview": (v[0] if v else "")[:120]}
        for k, v in sections.items()
    }
    return {
        "ok": True,
        "filename": target.get("filename", ""),
        "text_length": len(text),
        "summary": heuristic_summary(p),
        "sections_detected": section_summary,
        "fields": {
            "name": p.get("name"),
            "email": p.get("email"),
            "phone": p.get("phone"),
            "linkedin": p.get("linkedin"),
            "github": p.get("github"),
            "location": p.get("location"),
            "summary": p.get("summary"),
            "target_titles": p.get("target_titles"),
            "top_hard_skills": p.get("top_hard_skills"),
            "top_soft_skills": p.get("top_soft_skills"),
            "education": p.get("education"),
            "experience": p.get("experience"),
            "research_experience": p.get("research_experience"),
            "projects": p.get("projects"),
            "resume_gaps": p.get("resume_gaps"),
        },
    }


# ── Resume extra endpoints ────────────────────────────────────────────────────

@app.get("/api/resume/content")
def resume_content(request: Request, id: str = ""):
    _require_auth_user(request)
    if id:
        r = _get_resume_by_id(id)
        return {"text": r["text"] if r else ""}
    pr = _get_primary_resume()
    return {"text": pr["text"] if pr else ""}

@app.post("/api/resume/text")
async def resume_save_text(request: Request):
    _require_auth_user(request)
    body = await request.json()
    text = (body.get("text") or "").strip()
    rid = body.get("id", "")
    if not text:
        raise HTTPException(400, "Resume text cannot be empty")
    now = datetime.now().isoformat()
    r = _get_resume_by_id(rid) if rid else _get_primary_resume()
    if not r:
        raise HTTPException(404, "Resume not found")

    # Detect pasted LaTeX source — if the body looks like .tex (preamble,
    # \section, \begin{document}, or just dense backslash use) store it as
    # latex_source so the preview iframe can compile the real layout via
    # pdflatex. Plaintext from latex_to_plaintext drives downstream
    # skill/profile extraction. Mirrors the .txt/.md branch in _read_resume
    # so file-upload and paste paths now agree on LaTeX handling.
    from pipeline.latex import detect_latex, latex_to_plaintext
    if detect_latex(text):
        r["latex_source"] = text
        r["text"] = latex_to_plaintext(text)
    else:
        r["latex_source"] = None
        r["text"] = text
    r["updated_at"] = now
    r["profile"] = None

    # Re-render the preview PDF immediately so the user sees the compiled
    # LaTeX (or the reportlab fallback) without waiting for the background
    # extraction to finish — Ollama on the Pi can take 10-60 s.
    try:
        r["preview_pdf_path"] = _render_preview_pdf(r)
    except Exception as exc:
        print(f"[paste preview] render failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)

    if r.get("primary"):
        _S["resume_text"] = r["text"]
        _S["latex_source"] = r.get("latex_source")
        _S["done"].discard(1)
        _S["profile"] = None
    # Re-run extraction so the edited / pasted resume is fully analyzed.
    _kick_extraction(r, force=True)
    return {"ok": True, "id": r["id"], "extracting": True}

@app.delete("/api/resume/{resume_id}")
def resume_delete(resume_id: str, request: Request):
    _require_auth_user(request)
    resumes = _S.get("resumes") or []
    target = _get_resume_by_id(resume_id)
    if not target:
        raise HTTPException(404, "Resume not found")
    was_primary = target.get("primary", False)
    # Unlink the persisted upload binary, if any. Best-effort; failures don't
    # block the delete because the record itself going away is what matters.
    rel = target.get("original_path") or ""
    if rel:
        try:
            full = (OUTPUT_DIR / rel).resolve()
            full.relative_to(OUTPUT_DIR.resolve())
            full.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    _S["resumes"] = [r for r in resumes if r["id"] != resume_id]
    if was_primary:
        # Pick next resume as primary, or clear everything if none left
        remaining = _S["resumes"]
        if remaining:
            remaining[0]["primary"] = True
            # Primary changed (deletion fallback) — refresh search prefs from
            # the new primary's profile so location / experience / education
            # chips track the new resume rather than the deleted one.
            _sync_primary_scalars(remaining[0], force_prefs_refresh=True)
            _S["done"].discard(1)
        else:
            for k in ("resume_text", "latex_source", "resume_filename", "profile"):
                _S[k] = None
            _S["done"].discard(1)
    return {"ok": True}

@app.post("/api/resume/primary/{resume_id}")
def resume_set_primary(resume_id: str, request: Request):
    _require_auth_user(request)
    target = _get_resume_by_id(resume_id)
    if not target:
        raise HTTPException(404, "Resume not found")
    for r in (_S.get("resumes") or []):
        r["primary"] = r["id"] == resume_id
    # Sync scalar fields and global profile from the new primary. If the new
    # primary already has an extracted profile, _sync_primary_scalars copies
    # name/email/education/projects/etc. into _S["profile"] so the Profile
    # page reflects the new resume immediately. force_prefs_refresh=True
    # because the user explicitly switched primary — that's an unambiguous
    # signal to refresh the job-search-pref chips from the new resume.
    _sync_primary_scalars(target, force_prefs_refresh=True)

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
        _kick_extraction(target)

    return {"ok": True, "extracting": needs_extraction or not target.get("profile")}

@app.post("/api/resume/rename/{resume_id}")
async def resume_rename(resume_id: str, req: Request):
    _require_auth_user(req)
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


@app.post("/api/resume/{resume_id}/render-preview")
async def resume_render_preview(resume_id: str, request: Request):
    """Generate (or regenerate) a polished preview PDF for *resume_id*.

    Used as a back-fill for legacy records that were uploaded before the
    auto-render landed.  The SPA calls this when it notices a resume has
    text content but no ``preview_pdf_url``.  Idempotent: re-rendering
    overwrites the existing preview file.
    """
    _require_auth_user(request)
    record = _get_resume_by_id(resume_id)
    if not record:
        raise HTTPException(404, "Resume not found")

    # Back-fill the format fingerprint for legacy records that were
    # uploaded before format detection landed: if we still have the
    # original PDF on disk and no cached fingerprint, run detection now.
    if not record.get("format_profile") and (record.get("original_path") or "").endswith(".pdf"):
        try:
            pdf_path = (OUTPUT_DIR / record["original_path"]).resolve()
            if pdf_path.exists():
                from pipeline.pdf_format import detect_format_profile
                record["format_profile"] = detect_format_profile(pdf_path) or {}
        except Exception as exc:
            print(f"[pdf format] backfill failed for {resume_id!r}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)

    rel = _render_preview_pdf(record)
    if not rel:
        raise HTTPException(500, "Preview render failed — reportlab may be missing.")
    record["preview_pdf_path"] = rel
    record["updated_at"] = datetime.now().isoformat()
    return {"ok": True, "preview_pdf_url": _output_url(OUTPUT_DIR / rel)}


def _resolve_source_format() -> tuple[str | None, Path | None]:
    """Inspect the primary resume record to decide which renderer path the
    tailoring endpoint should hand to ``_save_tailored_resume``.

    Returns ``(source_format, source_bytes_path)``:
      • ``source_format`` is one of "tex" | "docx" | "pdf" | "txt" | "md" | None
      • ``source_bytes_path`` is the absolute path to the original upload bytes
        (only meaningful for the docx in-place renderer; others reuse
        latex_source / format_profile already in session state)

    Backfills missing ``source_format`` from the original upload's suffix —
    works for legacy resume records uploaded before this metadata was added.
    """
    primary = _get_primary_resume() or {}
    src = primary.get("source_format")
    if not src and primary.get("original_path"):
        suffix = Path(primary["original_path"]).suffix.lower().lstrip(".")
        if suffix in ("tex", "docx", "pdf", "txt", "md"):
            src = suffix
            primary["source_format"] = src
    bytes_path: Path | None = None
    if primary.get("original_path"):
        candidate = OUTPUT_DIR / primary["original_path"]
        if candidate.exists():
            bytes_path = candidate
    return src, bytes_path


@app.post("/api/resume/tailor/analyze")
async def resume_tailor_analyze(request: Request):
    """Step 1 of the two-step tailoring flow: classify JD keywords as
    must-have / nice-to-have and report which are already on the resume.

    Heuristic-only — no LLM call. Returns:
      {
        must_have:    [{keyword, present, suggested_section}],
        nice_to_have: [{keyword, present, suggested_section}],
        ats_score_current: int,
        estimated_after:   int,
      }
    """
    _require_auth_user(request)
    profile = _S.get("profile") or {}
    if not profile:
        raise HTTPException(400, "Upload a resume first.")
    body = await request.json()
    job_id = (body.get("job_id") or "").strip()
    if not job_id:
        raise HTTPException(400, "job_id is required")
    job = _find_job_by_id(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id!r}")

    requirements = [str(r).strip() for r in (job.get("requirements") or []) if str(r).strip()]
    skills = [str(s).strip() for s in (profile.get("top_hard_skills") or []) if str(s).strip()]
    resume_text = _S.get("resume_text") or ""

    half = max(1, len(requirements) // 2)
    must = requirements[:half]
    nice = requirements[half:]

    haystack = " ".join(skills + [resume_text]).lower()

    def _classify(kws: list[str]) -> list[dict]:
        out: list[dict] = []
        for kw in kws:
            present = kw.lower() in haystack
            out.append({
                "keyword": kw,
                "present": present,
                "suggested_section": "skills" if not present else "experience",
            })
        return out

    must_classified = _classify(must)
    nice_classified = _classify(nice)
    missing_count = sum(
        1 for c in must_classified + nice_classified if not c["present"]
    )
    current = _ats_score(resume_text + " " + " ".join(skills), requirements)
    estimated = min(100, current + missing_count * 4)

    return {
        "must_have":         must_classified,
        "nice_to_have":      nice_classified,
        "ats_score_current": current,
        "estimated_after":   estimated,
    }


@app.post("/api/resume/tailor")
async def resume_tailor(request: Request):
    """Per-job, on-demand resume tailoring (TailoredResume v2).

    Step 2 of the jobright.ai-style flow: the user has already reviewed the
    keyword list via ``/tailor/analyze`` and submits selected_keywords here.
    Callers without a review pass omit ``selected_keywords`` and we default
    to the heuristic's "all must-haves missing from resume" behavior.

    Body: { job_id, cover_letter?, selected_keywords?: [str] }
    Returns: same item shape as ``GET /api/phase/4/run``.items[] so the SPA
    reuses <TailoredResumeCard/> + the new green-highlight preview.
    """
    auth_user = _require_auth_user(request)

    profile = _S.get("profile") or {}
    if not profile:
        raise HTTPException(400, "Upload a resume first — no profile to tailor against.")

    if _S.get("mode") == "anthropic" and not _can_use_claude(auth_user):
        return JSONResponse(
            {"ok": False,
             "error": "Claude is a Pro-tier feature. Upgrade to use Claude.",
             "code": "plan_required"},
            status_code=402,
        )

    body = await request.json()
    job_id = (body.get("job_id") or "").strip()
    if not job_id:
        raise HTTPException(400, "job_id is required")
    include_cover = bool(body.get("cover_letter", _S.get("cover_letter", False)))
    selected_keywords = [
        str(k).strip() for k in (body.get("selected_keywords") or [])
        if str(k).strip()
    ]

    job = _find_job_by_id(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id!r}")

    try:
        prov = _make_provider()
    except Exception as exc:
        raise HTTPException(500, f"Provider unavailable: {exc}") from exc

    source_format, source_bytes_path = _resolve_source_format()

    try:
        tailored = phase4_tailor_resume(
            job, profile, _S.get("resume_text", ""), prov,
            include_cover_letter=include_cover,
            selected_keywords=selected_keywords,
            source_format=source_format,
        )
        resume_files = _save_tailored_resume(
            job, tailored, profile,
            _S.get("latex_source"),
            resume_text=_S.get("resume_text", ""),
            output_dir=_session_output_dir(),
            format_profile=_primary_format_profile(),
            source_format=source_format,
            source_bytes_path=source_bytes_path,
        )
    except Exception as exc:
        raise HTTPException(500, f"Tailoring failed: {type(exc).__name__}: {exc}") from exc

    # Pick the primary downloadable artifact for the user.
    # IMPORTANT: prefer the *_final* variants (clean / no green highlights)
    # over the diff variants. The user wants the downloaded file to be the
    # version they'd actually send to an employer — green highlights belong
    # only in the in-site iframe preview, not in the attached PDF.
    # Order:
    #   1. Clean DOCX (in-place editable, what users prefer for further edits)
    #   2. Clean PDF  (template-lib & in-place fallbacks)
    #   3. Clean LaTeX
    #   4. Any diff variant as a last resort (older callers / rendering failures)
    resume_file = (
        resume_files.get("docx_final")
        or resume_files.get("pdf_final")
        or resume_files.get("tex_final")
        or resume_files.get("docx")
        or resume_files.get("pdf")
        or resume_files.get("tex")
        or ""
    )
    resume_ref = ""
    if resume_file:
        resume_path = _session_output_dir() / resume_file
        resume_ref = Path(_output_url(resume_path)).as_posix().removeprefix("/output/")

    jk = job.get("id") or job.get("title", "")
    tmap = _S.get("tailored_map") or {}
    tmap[jk] = {"job": job, "tailored": tailored, "resume_file": resume_ref}
    _S["tailored_map"] = tmap

    item = _build_tailored_item(
        job=job,
        tailored=tailored,
        resume_ref=resume_ref,
        profile=profile,
        score=job.get("score", 0),
        status="Tailored",
        notes="",
        files=resume_files,
    )
    return {"item": item}


# ── Jobs actions ─────────────────────────────────────────────────────────────

@app.post("/api/jobs/action")
async def jobs_action(request: Request):
    _require_auth_user(request)
    body = await request.json()
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
    """Look up a job by id across the session's known job lists, then fall
    back to the persistent ingested-jobs store.

    Frontend ids come in two flavors:
      • ``f"{company}|{title}"`` for in-session demo / scored jobs that lack
        a stable id;
      • a hash from ``pipeline.job_repo._job_id(canonical_url)`` for jobs
        served by ``GET /api/jobs/feed`` (the persistent store).
    Both are handled.
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
    # Fall back to the persistent job_postings store. /api/jobs/feed serves
    # jobs from here; their ids are hashes of canonical_url, not co|title,
    # so the in-session pools above will never match them.
    if _session_store is not None:
        try:
            from pipeline import job_repo
            with _session_store.connect() as conn:
                rec = job_repo.get_job(conn, job_id)
            if rec:
                return rec
        except Exception as exc:
            print(f"[ask atlas] job_repo lookup failed for {job_id!r}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
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
    _require_auth_user(req)
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

    mode = _S.get("mode", "ollama")
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


# ── Atlas — career-wide chat (broad context, streaming) ──────────────────────


def _build_atlas_career_prompt(state: dict) -> str:
    """Build a system prompt that gives Atlas the user's full career snapshot:
    profile, target titles, all discovered jobs (capped), top-scored matches,
    submitted applications, and the most recent run-report excerpt.

    Atlas's job is to be a strategist for THIS user's overall search — not a
    single job. Be specific, cite real numbers, name real companies.
    """
    def _short(text: str, n: int = 1200) -> str:
        text = (text or "").strip()
        return (text[:n] + "…") if len(text) > n else text

    profile = state.get("profile") or {}
    jobs = state.get("jobs") or []
    scored = state.get("scored") or []
    apps = state.get("applications") or []
    report = state.get("report") or ""

    p_lines: list[str] = []
    for label, key in [
        ("Name", "name"), ("Email", "email"), ("Location", "location"),
        ("Target titles", "target_titles"),
        ("Top hard skills", "top_hard_skills"),
        ("Top soft skills", "top_soft_skills"),
        ("Education", "education"),
        ("Years of experience", "years_experience"),
        ("Resume gaps", "resume_gaps"),
    ]:
        v = profile.get(key)
        if not v:
            continue
        if isinstance(v, (list, tuple, set)):
            items = []
            for entry in list(v)[:8]:
                if isinstance(entry, dict):
                    items.append(entry.get("title") or entry.get("name") or entry.get("school") or str(entry))
                else:
                    items.append(str(entry))
            p_lines.append(f"{label}: {', '.join(s for s in items if s)}")
        else:
            p_lines.append(f"{label}: {v}")
    profile_block = "\n".join(p_lines) or "(no profile loaded)"

    # Compact per-job line: role, score (if scored), short URL host.
    def _job_line(j: dict, with_score: bool = False) -> str:
        co = (j.get("company") or j.get("co") or "").strip()
        role = (j.get("title") or j.get("role") or "").strip()
        loc = (j.get("location") or j.get("loc") or "").strip()
        score = j.get("score")
        bits = [f"{co} — {role}" if co or role else "(unknown role)"]
        if loc:
            bits.append(loc)
        if with_score and score is not None:
            bits.append(f"score {score}")
        return " · ".join(bits)

    discovered_total = len(jobs)
    discovered_block = (
        f"Total discovered: {discovered_total}\n"
        + "\n".join(f"  • {_job_line(j)}" for j in jobs[:14])
    ) if jobs else "(none discovered yet — Phase 2 not run)"

    passed = sorted(
        [j for j in scored if (j.get("filter_status") == "passed")],
        key=lambda x: x.get("score", 0),
        reverse=True,
    )
    if passed:
        scored_block = (
            f"Passed scoring: {len(passed)} of {len(scored)}\n"
            + "\n".join(f"  • {_job_line(j, with_score=True)}" for j in passed[:10])
        )
    else:
        scored_block = "(no scored matches yet — Phase 3 not run)"

    if apps:
        applied = [a for a in apps if a.get("status") == "Applied"]
        manual  = [a for a in apps if a.get("status") == "Manual Required"]
        skipped = [a for a in apps if a.get("status") == "Skipped"]
        apps_block = (
            f"Applied: {len(applied)} · Manual: {len(manual)} · Skipped: {len(skipped)}\n"
            + "\n".join(
                f"  • [{a.get('status', '?')}] {_job_line(a, with_score=True)}"
                + (f"  conf={a.get('confirmation')}" if a.get("confirmation") and a.get("confirmation") != "N/A" else "")
                for a in apps[:12]
            )
        )
    else:
        apps_block = "(no applications submitted yet — Phase 5 not run)"

    report_block = _short(str(report or ""), n=1500) or "(no run report yet — Phase 7 not run)"

    threshold = state.get("threshold") or 75
    # Default matches `_default_state()` — every user lands on Ollama; only
    # devs flip to Anthropic. Falling back to "anthropic" gave the LLM
    # incorrect provider context in the system prompt.
    mode = state.get("mode") or "ollama"

    return (
        "You are Atlas, the user's personal career strategist and job-search companion. "
        "You have direct visibility into their resume profile, the jobs they've discovered, "
        "how those jobs were scored against their skills, which applications they've submitted, "
        "and the most recent pipeline run-report. Speak directly to the user (\"you\").\n\n"
        "Your job is to be the world's expert on THIS user's job search. Be specific: cite real "
        "companies and roles from the data below by name; quote their actual top skills; reference "
        "their score threshold and applied/manual counts when relevant. Avoid generic career advice "
        "that could apply to anyone. If you don't have the data to answer well (e.g. a phase hasn't "
        "run yet), say so plainly and suggest which phase to run.\n\n"
        "Tone: direct, warm but not gushing, brief. 2 to 5 short paragraphs unless the user asks for "
        "a list. Use plain text. Never invent facts not in the data below.\n\n"
        f"Score threshold for auto-eligibility: {threshold}/100.   Provider mode: {mode}.\n\n"
        "==================== USER PROFILE ====================\n"
        f"{profile_block}\n"
        "================== DISCOVERED JOBS (Phase 2) =========\n"
        f"{discovered_block}\n"
        "================== TOP-SCORED MATCHES (Phase 3) ======\n"
        f"{scored_block}\n"
        "================== APPLICATIONS (Phase 5) ============\n"
        f"{apps_block}\n"
        "================== RUN REPORT (Phase 7, excerpt) =====\n"
        f"{report_block}\n"
        "======================================================="
    )


def _stream_provider_chat(provider, system: str, messages: list, max_tokens: int = 1024):
    """Yield text deltas for any of our three provider types."""
    from pipeline.providers import AnthropicProvider, OllamaProvider, DemoProvider

    if isinstance(provider, AnthropicProvider):
        from pipeline.providers import _run_cli_stream, _collapse_messages
        prompt = _collapse_messages(messages)
        if not prompt:
            return
        try:
            for text in _run_cli_stream(prompt, system=system or None):
                if text:
                    yield text
        except Exception as e:
            # Surface the failure as a single final yield so the SSE consumer
            # sees an error message rather than a silent truncation.
            yield f"\n\n[Atlas error: {type(e).__name__}: {e}]"
        return

    if isinstance(provider, OllamaProvider):
        try:
            from openai import OpenAI
        except ImportError:
            yield "(openai package missing — pip install openai to enable Ollama chat)"
            return
        oc = OpenAI(base_url=f"{provider.OLLAMA_URL}/v1", api_key="ollama", timeout=180)
        msgs: list = []
        if system:
            msgs.append({"role": "system", "content": str(system)})
        for m in (messages or []):
            if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip():
                msgs.append({"role": m["role"], "content": str(m["content"])})
        if len(msgs) == (1 if system else 0):
            return
        resp = oc.chat.completions.create(
            model=provider.model, messages=msgs,
            max_tokens=max_tokens, stream=True,
        )
        for chunk in resp:
            try:
                delta = chunk.choices[0].delta.content if chunk.choices else None
            except (AttributeError, IndexError):
                delta = None
            if delta:
                yield delta
        return

    if isinstance(provider, DemoProvider):
        # Demo mode has no live LLM — fall back to the canned reply, streamed word-by-word.
        text = provider.chat(system, messages, max_tokens=max_tokens)
        for word in text.split():
            yield word + " "
        return

    # Unknown provider — fall back to a single non-streamed reply.
    yield provider.chat(system, messages, max_tokens=max_tokens)


@app.post("/api/atlas/chat/stream")
async def atlas_chat_stream(req: Request):
    """Stream a career-wide Atlas reply via SSE.

    Body: { message: str, history?: [{role, content}, ...] }
    Emits: data: {type:"delta",text:"..."} repeatedly, then {type:"done"} or {type:"error"}.
    """
    _require_auth_user(req)
    body = await req.json()
    message = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not message:
        raise HTTPException(400, "Empty message")

    state_snapshot = _S.current()
    session_id = _S.session_id()
    system = _build_atlas_career_prompt(state_snapshot)

    trimmed: list[dict] = []
    for turn in history[-12:]:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            trimmed.append({"role": role, "content": content})
    trimmed.append({"role": "user", "content": message})

    mode = state_snapshot.get("mode", "ollama")

    def _gen():
        # Re-bind state inside the generator's thread (StreamingResponse runs the
        # body on a worker), matching the pattern used by the phase SSE helpers.
        _bind_thread_state(state_snapshot, session_id)
        yield _sse({"type": "start", "mode": mode})
        try:
            provider = _make_provider()
            empty = True
            for delta in _stream_provider_chat(provider, system, trimmed, max_tokens=1024):
                if delta:
                    empty = False
                    yield _sse({"type": "delta", "text": delta})
            if empty:
                yield _sse({"type": "delta", "text": "(no response — provider returned nothing)"})
            yield _sse({"type": "done", "mode": mode})
        except Exception as exc:
            yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
        titles = [t for t in (pr.get("target_titles") or []) if t and str(t).strip()]
        # Fallback when the extractor produced no target_titles (heuristic-
        # only profile, low-end Ollama, etc.). Pull verbatim titles from
        # work_experience so the title alignment signal isn't 0% for every
        # job just because the title-rules didn't fire on a niche resume.
        if not titles:
            for bucket in ("work_experience", "experience", "research_experience"):
                for row in (pr.get(bucket) or []):
                    t = (row.get("title") if isinstance(row, dict) else "") or ""
                    t = str(t).strip()
                    if t:
                        titles.append(t)
                    if len(titles) >= 6:
                        break
                if len(titles) >= 6:
                    break
        # User-stated job_titles also count as targets — they're the
        # explicit search preferences from the Settings page.
        stated = [t.strip() for t in (_S.get("job_titles") or "").split(",") if t.strip()]
        for s in stated:
            if s not in titles:
                titles.append(s)
        return {
            "target_titles": titles,
            "top_hard_skills": pr.get("top_hard_skills") or [],
            "location": pr.get("location") or "",
            "experience_levels": _S.get("experience_levels") or [],
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
    _require_auth_user(request)
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
    # Fall-through default for "include unknowns" flags is True. Most
    # ingested rows have unknown education AND unknown experience because
    # the source didn't carry a clean tag — a strict IN squashes them all
    # and produces "0 results" surprises (e.g. searching "marketing" with
    # the Internship chip returned nothing because internship marketing
    # roles overwhelmingly carry no experience_level tag).
    include_unknown_edu = _truthy(qs.get("include_unknown") or "1")
    include_unknown_exp = _truthy(qs.get("include_unknown_exp") or "1")
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
        include_unknown_education=include_unknown_edu,
        include_unknown_experience=include_unknown_exp,
        # `industry` is a comma-separated list of job_category labels. The chip
        # UI ships canonical labels (engineering, sales, healthcare, …) so this
        # is the same shape as `exp`.
        job_categories=_csv(qs.get("industry")),
    )
    profile_for_search = _profile_for_search()
    profile_complete = _profile_is_meaningful(profile_for_search)
    auth_user = _S.get("user") or {}
    feed_uid = auth_user.get("id") if isinstance(auth_user, dict) else None
    with _session_store.connect() as conn:
        page = search(conn=conn, filters=filters, profile=profile_for_search,
                      cursor=qs.get("cursor") or None, limit=limit,
                      user_id=feed_uid)
    # Drop hidden ids client-side intent applied here too.
    hidden = _S.get("hidden_ids") or set()
    visible = [j for j in page.jobs if j.id not in hidden]
    return {
        "jobs": [_dto_to_json(j, profile_complete) for j in visible],
        "next_cursor": page.next_cursor,
        "total_estimate": page.total_estimate,
        # Surface profile-completeness so the SPA can render a
        # "complete your profile to see real matches" banner instead of
        # showing fake-looking scores. Reads as an out-of-band signal —
        # neither the cursor nor the job objects depend on it.
        "profile_complete": profile_complete,
    }


def _profile_is_meaningful(profile: dict | None) -> bool:
    """Does the profile have enough signal to score jobs against?

    A "meaningful" profile has at least 2 hard skills, OR at least 1 target
    title, OR at least 1 work/research experience entry with a real title
    or company. The work-experience branch was added so an LLM that
    failed to populate `target_titles` (low-end Ollama, heuristic-only
    extract) doesn't completely zero out scoring even though the resume
    clearly has career signal. Below that, every score collapses to
    text-search relevance — which produced the "blank resume reads 68%
    on a senior hardware role" failure mode.
    """
    if not profile:
        return False
    skills = profile.get("top_hard_skills") or []
    titles = profile.get("target_titles") or []
    has_work = any(
        r and isinstance(r, dict) and (r.get("title") or r.get("company"))
        for bucket in ("work_experience", "experience", "research_experience")
        for r in (profile.get(bucket) or [])
    )
    return has_work \
        or len([s for s in skills if s and str(s).strip()]) >= 2 \
        or len([t for t in titles if t and str(t).strip()]) >= 1


def _dto_to_json(j, profile_complete: bool = True) -> dict:
    """Adapt a JobDTO to the wire shape the SPA already speaks (matches the
    keys used by ``state.scored_summary.jobs``: ``id, co, role, loc, score,
    skills, url, status``). Extra fields are added for richer cards.

    When ``profile_complete=False``, the score is set to None — the SPA
    renders a neutral "—" with a "Complete profile" hint instead of a
    misleading number derived purely from text-search relevance against an
    empty profile.
    """
    has_jd = bool((j.description or "").strip()) or bool(j.requirements)
    return {
        "id":      j.id,
        "co":      j.company,
        "role":    j.title,
        "loc":     j.location,
        "score":   round(j.score * 100) if profile_complete else None,
        "skills":  ", ".join(j.requirements[:6]),
        "url":     j.url,
        "remote":  j.remote,
        "salary":  j.salary_range,
        "exp":     j.experience_level,
        "edu":     j.education_required,
        "cit":     j.citizenship_required,
        "posted":  j.posted_at,
        "source":  j.source,
        # Honesty signal — when False the score was computed from title alone
        # (no requirements list, no description body). The SPA renders a small
        # "title-only" badge so users know the score is preliminary; lazy
        # description fetches in Phase 3 + SPA detail clicks promote rows out
        # of this state over time.
        "has_jd":  has_jd,
        "status":  "passed",
    }


# ── Lazy per-card scoring ─────────────────────────────────────────────────────
# In-memory, per-(user_id, job_id), 1 h TTL. Capped at 5000 entries (LRU
# evict). Scores recompute cheaply once descriptions are cached, so we
# don't persist them to disk — the user explicitly asked for transient
# storage that auto-expires.

_SCORE_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_SCORE_CACHE_LOCK = threading.Lock()
_SCORE_CACHE_TTL = 60 * 60        # 1 h
_SCORE_CACHE_MAX = 5000


def _score_cache_get(key: tuple[str, str]) -> dict | None:
    now = time.time()
    with _SCORE_CACHE_LOCK:
        entry = _SCORE_CACHE.get(key)
        if entry and (now - entry[0]) < _SCORE_CACHE_TTL:
            return entry[1]
        if entry:
            _SCORE_CACHE.pop(key, None)
    return None


def _score_cache_set(key: tuple[str, str], value: dict) -> None:
    now = time.time()
    with _SCORE_CACHE_LOCK:
        if len(_SCORE_CACHE) >= _SCORE_CACHE_MAX:
            # Drop the oldest 10% — amortized eviction.
            victims = sorted(_SCORE_CACHE.items(), key=lambda kv: kv[1][0])[
                : max(50, _SCORE_CACHE_MAX // 10)
            ]
            for k, _ in victims:
                _SCORE_CACHE.pop(k, None)
        _SCORE_CACHE[key] = (now, value)


def _score_cache_invalidate_user(user_id: str | None) -> int:
    """Wipe every cached score row for ``user_id``. Called on profile
    re-extraction (Re-scan & re-verify on the resume deep-dive page) and
    on /api/reset so the SPA doesn't keep serving 1-hour-stale scores
    that were computed against the previous profile. Returns the count
    of evicted rows (logged for observability)."""
    if not user_id:
        return 0
    with _SCORE_CACHE_LOCK:
        keys_to_drop = [k for k in _SCORE_CACHE.keys() if k[0] == user_id]
        for k in keys_to_drop:
            _SCORE_CACHE.pop(k, None)
    return len(keys_to_drop)


@app.post("/api/jobs/score-batch")
async def jobs_score_batch(request: Request):
    """Score a batch of jobs against the caller's profile. Used by the
    SPA's lazy-scoring path: the JobsPage IntersectionObserver queues up
    job IDs as cards scroll into view, then flushes the queue here.

    Body: ``{"job_ids": ["abc", "def", ...]}`` — capped at 30 per call.
    Response: ``{"scores": [{"id":"abc", "score":78, "has_jd":true, ...},
    {"id":"def", "score":null, "has_jd":false, "reason":"no description"}]}``.

    For each id:
      1. Look up in ``job_postings``. 404 if missing → ``has_jd=false``.
      2. If description is empty, lazy-fetch via
         ``pipeline.job_details.fetch_full_description``. Cached in-memory
         by job_details.py for 1 h. NOT persisted to disk per the user's
         "transient cache" requirement.
      3. If still no description: refuse to score (return ``score=null,
         has_jd=false``). The SPA shows a "preview score" badge.
      4. Otherwise run ``compute_skill_coverage`` against the user profile,
         compose the rubric score, return + cache for 1 h.
    """
    auth_user = _require_auth_user(request)
    user_id = (auth_user or {}).get("id") or "anon"
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    job_ids = body.get("job_ids") or []
    if not isinstance(job_ids, list):
        raise HTTPException(400, "job_ids must be a list")
    job_ids = [str(j) for j in job_ids if j and str(j).strip()][:30]
    if not job_ids:
        return {"scores": []}

    profile = _S.get("profile") or {}
    if not _profile_is_meaningful(profile):
        # Profile is empty or essentially empty (≤1 skill, no target titles)
        # — every score against an empty profile would be derived from
        # text-search relevance alone, producing fake-looking numbers like
        # "68% match" on a senior hardware role from a blank resume. Return
        # null with a clear reason so the SPA can render a "complete your
        # profile" hint instead.
        return {"scores": [
            {"id": jid, "score": None, "has_jd": False,
             "reason": "incomplete profile — add hard skills or a target title to enable scoring"}
            for jid in job_ids
        ], "profile_complete": False}

    from pipeline import job_repo, job_details, providers

    # Profile-strength multiplier: caps job scores when the underlying
    # resume is thin / template-y (placeholder skills + stub work entry).
    # Without this, a template-shaped profile would still read "40% match"
    # on every job whose title contains the placeholder. `max(0.2, ...)`
    # keeps a small ordering signal alive even for very weak profiles.
    strength = max(0.2, providers.profile_strength(profile))

    # Pull the rows we need in one query.
    if _session_store is None:
        return {"scores": [{"id": jid, "score": None, "has_jd": False,
                            "reason": "session store unavailable"} for jid in job_ids]}
    with _session_store.connect() as conn:
        rows = job_repo.bulk_get_by_ids(conn, job_ids)
    by_id = {r["id"]: r for r in rows}

    out: list[dict] = []
    for jid in job_ids:
        # 1. Cache hit — return immediately without touching the DB or
        #    re-scoring. Only valid when the same user re-requests the
        #    same job within the TTL window.
        cached = _score_cache_get((user_id, jid))
        if cached is not None:
            out.append({**cached, "id": jid, "cached": True})
            continue

        rec = by_id.get(jid)
        if not rec:
            out.append({"id": jid, "score": None, "has_jd": False,
                        "reason": "job not found in index"})
            continue

        # 2. Ensure a description is available — try DB, then lazy fetch.
        desc = (rec.get("description") or "").strip()
        if not desc:
            try:
                fetched = job_details.fetch_full_description(
                    rec.get("url") or "", rec.get("source") or "",
                )
                desc = (fetched or {}).get("description", "").strip()
            except Exception:
                desc = ""
        # 3. Refuse to score without something to score against.
        if not desc and not rec.get("requirements"):
            payload = {
                "id": jid, "score": None, "has_jd": False,
                "reason": "no description available — score requires job content",
            }
            _score_cache_set((user_id, jid), payload)
            out.append(payload)
            continue

        # 4. Score. compute_skill_coverage is deterministic; it reads
        #    title + requirements + description and returns coverage in
        #    [0, 1] plus matched / missing skill lists.
        job_for_scoring = {
            "id": jid,
            "title": rec.get("title", ""),
            "requirements": rec.get("requirements") or [],
            "description": desc,
        }
        try:
            coverage, matched, missing = providers.compute_skill_coverage(
                job_for_scoring, profile,
            )
        except Exception as exc:
            print(f"[score-batch] coverage failed for {jid!r}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            payload = {"id": jid, "score": None, "has_jd": bool(desc),
                       "reason": "scoring error"}
            _score_cache_set((user_id, jid), payload)
            out.append(payload)
            continue

        # Title alignment vs. the candidate's target titles. The same titles
        # fallback `_profile_for_search` builds (resume work history + Settings
        # job_titles) so a profile without LLM-extracted `target_titles` still
        # gets a real signal here. Token-overlap gives partial credit (e.g.
        # "FPGA Engineer" target × "Hardware Engineer" job = 0.5) instead of
        # the previous all-or-nothing match.
        title_lower = (rec.get("title") or "").lower()
        title_targets = [str(t).strip().lower()
                          for t in (profile.get("target_titles") or []) if t]
        if not title_targets:
            for bucket in ("work_experience", "experience", "research_experience"):
                for row in (profile.get(bucket) or []):
                    t = (row.get("title") if isinstance(row, dict) else "") or ""
                    t = str(t).strip().lower()
                    if t:
                        title_targets.append(t)
                    if len(title_targets) >= 5:
                        break
                if len(title_targets) >= 5:
                    break
            for t in (_S.get("job_titles") or "").split(","):
                t = t.strip().lower()
                if t and t not in title_targets:
                    title_targets.append(t)
        title_hit = 0.0
        for t in title_targets:
            t_tokens = [tk for tk in t.split() if len(tk) > 2]
            if not t_tokens:
                continue
            hits = sum(1 for tk in t_tokens if tk in title_lower)
            if hits == len(t_tokens):
                title_hit = 1.0
                break
            if hits:
                title_hit = max(title_hit, hits / len(t_tokens))
        # Location & seniority: real comparison against the user's profile
        # location and configured experience_levels rather than a flat 0.5.
        prof_loc = str(profile.get("location") or "").strip().lower()
        job_loc = str(rec.get("location") or "").lower()
        loc_first = prof_loc.split(",")[0].strip() if prof_loc else ""
        if rec.get("remote") and prof_loc:
            loc_score = 0.9
        elif loc_first and loc_first in job_loc:
            loc_score = 1.0
        elif prof_loc and any(p.strip() in job_loc for p in prof_loc.split(",") if p.strip()):
            loc_score = 0.7
        elif rec.get("remote"):
            loc_score = 0.6
        elif not prof_loc:
            loc_score = 0.5
        else:
            loc_score = 0.25

        exp_prefs = _S.get("experience_levels") or profile.get("experience_levels") or []
        if isinstance(exp_prefs, str):
            exp_prefs = [exp_prefs]
        job_exp = (rec.get("experience_level") or "").lower().strip()
        if not exp_prefs:
            sen_score = 0.5
        elif not job_exp or job_exp == "unknown":
            sen_score = 0.6  # unknown-tagged rows shouldn't penalise — most ingested rows are untagged
        elif job_exp in [str(e).lower() for e in exp_prefs]:
            sen_score = 1.0
        else:
            sen_score = 0.2
        loc_seniority = round((loc_score + sen_score) / 2, 3)

        score_pct = int(round((
            providers.RUBRIC_WEIGHTS["required_skills"] * coverage
            + providers.RUBRIC_WEIGHTS["industry"] * title_hit
            + providers.RUBRIC_WEIGHTS["location_seniority"] * loc_seniority
        ) * strength))
        payload = {
            "id":            jid,
            "score":         max(0, min(100, score_pct)),
            "has_jd":        True,
            "matched":       matched[:6],
            "missing":       missing[:6],
            "coverage":      round(float(coverage), 3),
            "title_match":   round(float(title_hit), 3),
            "loc_seniority": round(float(loc_seniority), 3),
            "profile_strength": round(float(strength), 3),
        }
        _score_cache_set((user_id, jid), payload)
        out.append(payload)

    return {"scores": out}


@app.get("/api/jobs/facets")
def jobs_facets(request: Request):
    """Return facet buckets (label, value, count) for one dimension of the job
    index. Powers the Industry / Location / Company filter chips on the Jobs
    page — the frontend renders a curated default list and live-searches this
    endpoint when the user types into the chip's search box.

    PUBLIC endpoint (no auth gate). Facets return pure aggregate catalog
    metadata — "how many jobs are in London" / "how many at Stripe" — no
    PII, no per-user data, nothing a signed-up user wouldn't already see.
    Gating this previously caused two real problems:
      (1) cold-load 401 race when the SPA polled before /api/state had
          resolved the auth cookie ("GET /api/jobs/facets … 401 Unauthorized"
          spam in journalctl), and
      (2) the landing page couldn't show real "we have N jobs in your city"
          stats to unauthenticated visitors.

    Query params:
      kind   — 'industry' | 'location' | 'company' (required)
      q      — case-insensitive substring filter on the bucket label
      limit  — max buckets to return (default 25, max 200)
    """
    if _session_store is None:
        return {"kind": "", "buckets": []}
    qs = request.query_params
    kind = (qs.get("kind") or "").strip().lower()
    if kind not in ("industry", "location", "company"):
        raise HTTPException(400, "kind must be 'industry', 'location', or 'company'")
    q = (qs.get("q") or "").strip().lower()
    try:
        limit = max(1, min(200, int(qs.get("limit") or 25)))
    except ValueError:
        limit = 25

    # Each branch issues a single GROUP BY against `job_postings`. The columns
    # are already covered by the `job_category`, `location`, and `company`
    # indexes on the table, so this stays cheap even on a 100k-row index.
    if kind == "industry":
        sql = (
            "SELECT COALESCE(NULLIF(LOWER(TRIM(job_category)), ''), 'general') AS bucket, "
            "COUNT(*) AS n "
            "FROM job_postings WHERE deleted = 0 "
        )
        params: list = []
        if q:
            sql += "AND LOWER(COALESCE(job_category, 'general')) LIKE ? "
            params.append(f"%{q}%")
        sql += "GROUP BY bucket ORDER BY n DESC, bucket ASC LIMIT ?"
        params.append(limit)
    elif kind == "location":
        # Use the first comma-separated city/region token as the bucket so a
        # location facet doesn't fragment "San Francisco, CA, US" away from
        # "San Francisco, CA". Empty/null locations bucket as "Unknown".
        sql = (
            "SELECT COALESCE(NULLIF(TRIM(SUBSTR(location, 1, "
            "  CASE WHEN INSTR(location || ',', ',') > 0 "
            "       THEN INSTR(location || ',', ',') - 1 "
            "       ELSE LENGTH(location) END)), ''), 'Unknown') AS bucket, "
            "COUNT(*) AS n "
            "FROM job_postings WHERE deleted = 0 "
        )
        params = []
        if q:
            sql += "AND LOWER(location) LIKE ? "
            params.append(f"%{q}%")
        sql += "GROUP BY bucket ORDER BY n DESC, bucket ASC LIMIT ?"
        params.append(limit)
    else:  # company
        sql = (
            "SELECT COALESCE(NULLIF(TRIM(company), ''), 'Unknown') AS bucket, "
            "COUNT(*) AS n "
            "FROM job_postings WHERE deleted = 0 "
        )
        params = []
        if q:
            sql += "AND LOWER(company) LIKE ? "
            params.append(f"%{q}%")
        sql += "GROUP BY bucket ORDER BY n DESC, bucket ASC LIMIT ?"
        params.append(limit)

    with _session_store.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    buckets = [{"value": r[0], "count": int(r[1])} for r in rows if r[0]]
    return {"kind": kind, "buckets": buckets}


@app.get("/api/jobs/{job_id}/details")
def jobs_details(job_id: str, request: Request):
    """Full per-job detail payload for the JobDetailView sub-page.

    Composes:
      • full description re-fetched from the upstream source's per-job API
        (greenhouse / lever / ashby / workable) and parsed into
        responsibilities / required / preferred / benefits buckets.
      • a Wikipedia-derived company summary + image when available.

    The fetch is cached for 1 h per job (24 h per company) so opening the
    same posting twice doesn't re-hit the upstream APIs.

    Returns ``has_description=False`` when the source isn't supported or
    the upstream fetch failed — the SPA renders a "view original posting"
    fallback. Wikipedia data is best-effort and returns empty strings on
    miss; the SPA falls back to the curated lookup links.
    """
    _require_auth_user(request)

    # Resolve the job — try the persistent index first (handles the common
    # case of a feed-served job), then fall back to the in-session pools
    # for jobs that came in via Phase 2/3 with a different id shape.
    rec = None
    if _session_store is not None:
        try:
            from pipeline import job_repo
            with _session_store.connect() as conn:
                rec = job_repo.get_job(conn, job_id)
        except Exception as exc:
            print(f"[jobs/details] job_repo.get_job failed for {job_id!r}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
    if rec is None:
        rec = _find_job_by_id(job_id)
    if not rec:
        raise HTTPException(404, f"Job not found: {job_id!r}")

    # Normalize across the two shapes ('url' from job_repo, 'application_url'
    # from session pools).
    canonical_url = (rec.get("url") or rec.get("application_url") or "").strip()
    source        = (rec.get("source") or "").strip()
    company       = (rec.get("company") or rec.get("co") or "").strip()

    try:
        from pipeline import job_details as _details
        payload = _details.get_job_details(job_id, canonical_url, source, company)
        # Persist the freshly-fetched description back into the job_postings
        # index so future Phase 3 scoring reads it directly without paying
        # the per-job API call again. Only writes when the row currently has
        # nothing — prior populations from the source-listing path or earlier
        # detail clicks are preserved. Idempotent + best-effort.
        desc_text = (payload.get("description") or "").strip()
        if desc_text and _session_store is not None:
            try:
                with _session_store.connect() as _conn:
                    _conn.execute(
                        "UPDATE job_postings SET description = ? "
                        "WHERE id = ? AND (description IS NULL OR description = '')",
                        (desc_text[:16000], job_id),
                    )
                    _conn.commit()
            except Exception as exc:
                print(f"[jobs/details] description persist failed for {job_id!r}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
    except Exception as exc:
        # Surface a structured failure rather than a 500 — the SPA shows the
        # "view original posting" fallback regardless.
        print(f"[jobs/details] get_job_details failed for {job_id!r}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        payload = {
            "description":              "",
            "lead_paragraph":           "",
            "responsibilities":         [],
            "required_qualifications":  [],
            "preferred_qualifications": [],
            "benefits":                 [],
            "has_description":          False,
            "fetched_at":               None,
            "company_summary":          "",
            "company_short_description": "",
            "company_image":            "",
            "company_wiki_url":         "",
            "fetch_error":              f"{type(exc).__name__}: {exc}",
        }

    # Echo back the resolved meta the SPA already had — this lets the
    # detail view render the source-platform tag even before the rest of
    # the payload renders, and confirms which row we matched on a stale id.
    payload["job_id"]        = job_id
    payload["canonical_url"] = canonical_url
    payload["source"]        = source
    payload["company"]       = company
    return payload


@app.get("/api/jobs/source-status")
def jobs_source_status(request: Request):
    """Per-source health snapshot for the dev page. Dev-only — exposes
    ingestion metrics, error strings, and per-source row counts that
    aren't appropriate for an unauthenticated visitor.
    """
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access required")
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
    """Force-tick one source (or all if body is empty). Restricted to dev.

    Single-source path runs synchronously (with a 30 s cap) so the caller
    sees the result. Force-all path fires-and-forgets in a daemon thread —
    18 sources × HTTP timeouts × the SQLite write lock can take 30-60 s
    in aggregate, and the SPA's "Refresh" button is itself fire-and-forget
    on this endpoint, so blocking the response thread serves nobody.
    """
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access required")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    name = (body or {}).get("source") or None
    from pipeline import ingest as _job_ingest

    if name:
        # Specific source: run synchronously off the event loop, capped at 30 s.
        import asyncio as _asyncio
        try:
            results = await _asyncio.wait_for(
                _asyncio.to_thread(_job_ingest.force_run, name, 30.0),
                timeout=35.0,
            )
        except _asyncio.TimeoutError:
            results = [{"source": name, "ok": False, "error": "request timeout (>35 s)"}]
        return {"ok": True, "results": results}

    # Force all: kick a background thread and return immediately. The thread
    # honors its own 60 s wall-clock cap inside force_run.
    threading.Thread(
        target=_job_ingest.force_run,
        kwargs={"source_name": None, "wall_clock_timeout": 60.0},
        daemon=True,
        name="force-run-all",
    ).start()
    return {"ok": True, "started": True,
            "note": "Running all sources in background — poll /api/jobs/source-status to track."}


# ── Feedback ──────────────────────────────────────────────────────────────────

@app.post("/api/feedback")
async def submit_feedback(request: Request):
    _require_auth_user(request)
    body = await request.json()
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
    _DEV_EMAILS = {"jonnyliu4@gmail.com", "saosithisak@gmail.com"}
    auth_user = _auth_token_lookup(request.cookies.get(_AUTH_COOKIE, ""))
    if auth_user and auth_user.get("id") and _user_store is not None:
        fresh = _user_store.get_user_by_id(auth_user["id"])
        if fresh and bool(fresh.get("is_developer")):
            return True
    if auth_user and auth_user.get("email", "").lower() in _DEV_EMAILS:
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

@app.get("/api/dev/logs")
def dev_logs(request: Request, since: int = 0, limit: int = 500):
    """Recent server stdout / stderr / logging records.

    Backs the Dev Ops "Server log" terminal panel.  Returns at most
    *limit* most-recent records whose ``seq`` is greater than *since*
    so the SPA can ask "what's new" without re-fetching the entire
    ring on every poll.
    """
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    try:
        limit = max(1, min(int(limit) or 500, _LOG_RING_MAX))
    except (TypeError, ValueError):
        limit = 500
    try:
        since = int(since) if since else 0
    except (TypeError, ValueError):
        since = 0
    with _LOG_RING_LOCK:
        snapshot = list(_LOG_RING)
        latest_seq = _LOG_SEQ
    fresh = [r for r in snapshot if r.get("seq", 0) > since]
    if len(fresh) > limit:
        fresh = fresh[-limit:]
    return {
        "logs":       fresh,
        "latest_seq": latest_seq,
        "max_buffer": _LOG_RING_MAX,
        "total":      len(snapshot),
    }


@app.get("/api/dev/logs/stream")
def dev_logs_stream(request: Request, since: int = 0):
    """SSE feed of new log records.  One ``log`` event per record plus a
    ``ping`` every 15 s to keep the connection alive."""
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    try:
        since = int(since) if since else 0
    except (TypeError, ValueError):
        since = 0
    q: "queue.Queue[dict]" = queue.Queue(maxsize=2000)
    def _gen():
        with _LOG_RING_LOCK:
            backlog = [r for r in list(_LOG_RING) if r.get("seq", 0) > since]
            _LOG_SUBSCRIBERS.append(q)
        try:
            for r in backlog:
                yield "event: log\ndata: " + json.dumps(r) + "\n\n"
            last_ping = time.time()
            while True:
                try:
                    rec = q.get(timeout=1.5)
                    yield "event: log\ndata: " + json.dumps(rec) + "\n\n"
                except queue.Empty:
                    pass
                if time.time() - last_ping > 15:
                    last_ping = time.time()
                    yield "event: ping\ndata: {}\n\n"
        finally:
            try:
                _LOG_SUBSCRIBERS.remove(q)
            except ValueError:
                pass
    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/dev/metrics")
def dev_metrics(request: Request, with_processes: int = 0):
    """Fast-path system metrics for the Dev Ops page.

    Separate from /api/dev/overview because that handler iterates every
    session row and is too heavy to call once a second.  This endpoint
    only reads cached psutil samples + uptime, so it can drive a live
    htop-style CPU view at a 1-2s cadence without churn.

    `with_processes=1` opts into the heavier per-process snapshot
    (top-by-CPU + top-by-memory). The Server tab uses it; the Overview
    tab leaves it off so the standard 2-second tick stays cheap.
    """
    if not _is_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    payload = {
        "server_started_at": _SERVER_STARTED_AT.isoformat(timespec="seconds"),
        "server_uptime_s":   round(_server_uptime_seconds(), 1),
        # Host-level uptime distinguishes "uvicorn just restarted" from
        # "the box has been up for weeks" — the process timer resets on
        # every `systemctl restart`, the host one doesn't.
        **_os_uptime_payload(),
        "cpu":               _cpu_snapshot(),
        "memory":            _memory_snapshot(),
        "cpu_temp":          _cpu_temperature(),
    }
    if with_processes:
        payload["processes"] = _top_processes()
    return payload


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
                DB_PATH.stat().st_size / 1e6, 2
            ) if DB_PATH.exists() else 0,
            "disk_free_gb": round(disk.free / 1e9, 1),
            "tweaks": _S.get("dev_tweaks") or {},
            # Server-process uptime — wall-clock seconds since uvicorn
            # boot.  Surfaced as both seconds (for the SPA's live tick)
            # and an ISO timestamp (for absolute reference).
            "server_started_at": _SERVER_STARTED_AT.isoformat(timespec="seconds"),
            "server_uptime_s":   round(_server_uptime_seconds(), 1),
            **_os_uptime_payload(),
            # Live system metrics — htop-style per-core breakdown plus a
            # quick memory headline.  Cheap; cached server-side for 800ms.
            "cpu":      _cpu_snapshot(),
            "memory":   _memory_snapshot(),
            "cpu_temp": _cpu_temperature(),
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
    response.set_cookie(_DEV_IMPERSONATE_COOKIE, session_id, httponly=True, samesite="lax", secure=_COOKIE_SECURE)
    return response

@app.post("/api/dev/session/stop-impersonating")
def dev_stop_impersonate(request: Request):
    response = JSONResponse({"ok": True})
    # Mirror the attrs used in dev_impersonate's set_cookie above.
    response.delete_cookie(
        _DEV_IMPERSONATE_COOKIE, path="/", samesite="lax",
        secure=_COOKIE_SECURE, httponly=True,
    )
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
    db_path = str(DB_PATH)

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
    import pipeline.providers as _prov
    return {
        "runtime": dict(_RUNTIME),
        "env": {
            "ollama_url": OLLAMA_URL,
            "default_ollama_model": DEFAULT_OLLAMA_MODEL,
            "smtp_configured": all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS")),
            "cli_healthy": _prov._CLI_HEALTHY,
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


def _apply_plan_change(user_id: str, tier: str) -> None:
    """Persist a plan_tier flip and propagate it across the auth caches.

    Single source of truth for "user just upgraded / downgraded": writes the
    DB column, refreshes every persisted ``auth_tokens.user_json`` row, and
    updates the in-memory ``_AUTH_SESSIONS_FALLBACK`` so a token issued
    before the SQLite store came up still picks up the new tier on the next
    request.
    """
    if _user_store is None:
        return
    _user_store.set_user_plan_tier(user_id, tier)
    try:
        _user_store.refresh_user_plan_in_tokens(user_id, tier)
    except Exception:
        import traceback as _tb
        _tb.print_exc()
    for payload in _AUTH_SESSIONS_FALLBACK.values():
        if payload.get("id") == user_id:
            payload["plan_tier"] = tier


@app.post("/api/dev/users/{user_id}/plan")
async def dev_users_set_plan(user_id: str, request: Request):
    """Manually grant or revoke Pro for a user. Useful for ops escapes when
    the Stripe webhook is offline or for granting comp Pro to testers."""
    if not _is_underlying_dev_request(request):
        raise HTTPException(403, "Developer access denied")
    body = await request.json()
    tier = (body.get("tier") or "").strip()
    if tier not in ("free", "pro"):
        raise HTTPException(400, f"Invalid tier: {tier!r} (expected 'free' or 'pro')")
    if _user_store is None:
        raise HTTPException(503, "User store unavailable")
    _apply_plan_change(user_id, tier)
    return {"ok": True, "user_id": user_id, "plan_tier": tier}


# ── Billing (Stripe) ──────────────────────────────────────────────────────────
#
# Three endpoints + one webhook. The user upgrade flow is:
#   1. SPA POSTs /api/billing/checkout      → returns {url}
#   2. Browser → Stripe Checkout (hosted)   → user pays
#   3. Browser → success_url (back to /app#plans?upgraded=1)
#   4. Stripe → POST /api/webhooks/stripe   → flips plan_tier=pro server-side
#
# The success redirect alone never grants Pro — the webhook is the source of
# truth. Until the webhook fires, /api/state still reports plan_tier=free.
# Typical lag is < 5 seconds.

_STRIPE_PRICE_ID_PRO_MONTHLY = (os.environ.get("STRIPE_PRICE_ID_PRO_MONTHLY") or "").strip()
_STRIPE_WEBHOOK_SECRET = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()
_PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")


def _public_base_url(request: Request) -> str:
    """Resolve the public origin for Checkout / Portal redirects.

    Prefer the ``PUBLIC_BASE_URL`` env var — when running behind Tailscale
    Funnel or a proxy the inbound URL FastAPI sees can be the internal
    address (e.g. ``http://localhost:8000``), which Stripe would happily
    redirect users to and produce a broken back-button experience. The env
    override is the contract.
    """
    if _PUBLIC_BASE_URL:
        return _PUBLIC_BASE_URL
    base = str(request.base_url).rstrip("/")
    return base or "http://localhost:8000"


def _billing_unavailable() -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": "Billing is not configured on this server."},
        status_code=503,
    )


@app.post("/api/billing/checkout")
async def billing_checkout(request: Request):
    """Create a Stripe Checkout Session for the Pro monthly subscription
    and return the redirect URL. The caller's browser does the navigation —
    the SPA is intentionally not loading any Stripe.js, so this is a pure
    server-issued redirect."""
    auth_user = _require_auth_user(request)

    if not _stripe_billing.is_configured():
        return _billing_unavailable()
    if not _STRIPE_PRICE_ID_PRO_MONTHLY:
        return JSONResponse(
            {"ok": False,
             "error": "STRIPE_PRICE_ID_PRO_MONTHLY is not set — run scripts/setup_stripe.py."},
            status_code=503,
        )
    if _user_store is None:
        return _billing_unavailable()

    user = _user_store.get_user_by_id(auth_user["id"])
    if not user:
        raise HTTPException(404, "User not found")

    # Already Pro? Send them to the portal instead so they can manage the
    # existing subscription rather than creating a duplicate. Default fallback
    # is "pro" (everyone-is-Pro testing phase) so a missing plan_tier blocks
    # accidental double-subscription rather than allowing it.
    if (user.get("plan_tier") or "pro") == "pro":
        return JSONResponse(
            {"ok": False, "error": "Already on Pro — use the portal to manage the subscription.",
             "code": "already_pro"},
            status_code=409,
        )

    base = _public_base_url(request)
    success_url = f"{base}/app#plans?upgraded=1&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base}/app#plans?cancel=1"

    try:
        customer_id = _stripe_billing.ensure_customer(_user_store, user)
        session = _stripe_billing.create_checkout_session(
            customer_id=customer_id,
            client_reference_id=user["id"],
            price_id=_STRIPE_PRICE_ID_PRO_MONTHLY,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except Exception as exc:
        import traceback as _tb
        _tb.print_exc()
        return JSONResponse(
            {"ok": False, "error": f"Could not start checkout: {type(exc).__name__}: {exc}"},
            status_code=502,
        )
    return {"ok": True, "url": session["url"], "id": session["id"]}


@app.post("/api/billing/portal")
async def billing_portal(request: Request):
    """Return a Stripe Customer Portal URL for the authenticated user.
    Requires a previously created customer — i.e. the user has gone through
    Checkout at least once. Free users with no customer record get a 404
    so the SPA can hide the button."""
    auth_user = _require_auth_user(request)

    if not _stripe_billing.is_configured():
        return _billing_unavailable()
    if _user_store is None:
        return _billing_unavailable()

    user = _user_store.get_user_by_id(auth_user["id"])
    if not user:
        raise HTTPException(404, "User not found")
    customer_id = (user.get("stripe_customer_id") or "").strip()
    if not customer_id:
        raise HTTPException(404, "No Stripe customer for this user yet")

    base = _public_base_url(request)
    return_url = f"{base}/app#plans"
    try:
        session = _stripe_billing.create_portal_session(customer_id=customer_id, return_url=return_url)
    except Exception as exc:
        import traceback as _tb
        _tb.print_exc()
        return JSONResponse(
            {"ok": False, "error": f"Could not open portal: {type(exc).__name__}: {exc}"},
            status_code=502,
        )
    return {"ok": True, "url": session["url"]}


def _resolve_user_for_event(event_obj: dict, customer_id: str | None) -> dict | None:
    """Map a Stripe event payload back to one of our users.

    Tries (in order):
      1. ``client_reference_id`` on the Checkout Session
      2. ``metadata.user_id`` on the Subscription
      3. lookup by persisted ``stripe_customer_id``

    Returns None when the event genuinely isn't ours — e.g. a customer
    created out-of-band in the Stripe dashboard.
    """
    if _user_store is None:
        return None
    cri = event_obj.get("client_reference_id") if isinstance(event_obj, dict) else None
    if cri:
        u = _user_store.get_user_by_id(cri)
        if u:
            return u
    md = (event_obj.get("metadata") or {}) if isinstance(event_obj, dict) else {}
    md_uid = md.get("user_id") if isinstance(md, dict) else None
    if md_uid:
        u = _user_store.get_user_by_id(md_uid)
        if u:
            return u
    if customer_id:
        u = _user_store.get_user_by_stripe_customer(customer_id)
        if u:
            return u
    return None


@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Stripe webhook endpoint. Authenticated by HMAC signature only — NEVER
    by cookie. Idempotent: every handler is an UPDATE, so duplicate
    deliveries are no-ops.

    Events we act on:
      - ``checkout.session.completed`` — link customer/subscription IDs to user.
        Does NOT flip plan_tier (subscription may still be ``incomplete``).
      - ``customer.subscription.created`` / ``.updated`` — drive plan_tier from
        ``status``. ``active`` / ``trialing`` / ``past_due`` → ``pro``;
        anything else → ``free``.
      - ``customer.subscription.deleted`` — flip to ``free`` and clear
        subscription_id, but keep customer_id so the user can resubscribe.

    Everything else is logged and returned 200 so Stripe doesn't retry.
    """
    if not _STRIPE_WEBHOOK_SECRET:
        return JSONResponse(
            {"ok": False, "error": "STRIPE_WEBHOOK_SECRET is not set"},
            status_code=503,
        )
    payload_bytes = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = _stripe_billing.verify_webhook(payload_bytes, sig_header, _STRIPE_WEBHOOK_SECRET)
    except _stripe_billing.WebhookVerifyError as exc:
        print(f"[stripe webhook] verify failed: {exc}", file=sys.stderr)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    etype = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    customer_id = obj.get("customer") if isinstance(obj.get("customer"), str) else None

    if _user_store is None:
        # Acknowledge so Stripe stops retrying; nothing we can persist.
        print(f"[stripe webhook] {etype}: no user_store, dropping", file=sys.stderr)
        return {"ok": True, "type": etype, "ignored": True}

    try:
        if etype == "checkout.session.completed":
            user = _resolve_user_for_event(obj, customer_id)
            if not user:
                print(f"[stripe webhook] {etype}: could not resolve user", file=sys.stderr)
                return {"ok": True, "type": etype, "ignored": True}
            sub_id = obj.get("subscription") if isinstance(obj.get("subscription"), str) else None
            if customer_id and not user.get("stripe_customer_id"):
                _user_store.set_user_stripe_customer(user["id"], customer_id)
            if sub_id:
                _user_store.set_user_subscription(user["id"], sub_id, tier=None)
            print(
                f"[stripe webhook] checkout.session.completed: user={user['id']} "
                f"customer={customer_id} subscription={sub_id} (plan_tier deferred)",
                file=sys.stderr,
            )

        elif etype in ("customer.subscription.created", "customer.subscription.updated"):
            user = _resolve_user_for_event(obj, customer_id)
            if not user:
                print(f"[stripe webhook] {etype}: could not resolve user", file=sys.stderr)
                return {"ok": True, "type": etype, "ignored": True}
            status = obj.get("status") or ""
            sub_id = obj.get("id") or ""
            new_tier = "pro" if _stripe_billing.subscription_active(status) else "free"
            _user_store.set_user_subscription(user["id"], sub_id or None, tier=new_tier)
            try:
                _user_store.refresh_user_plan_in_tokens(user["id"], new_tier)
            except Exception:
                import traceback as _tb
                _tb.print_exc()
            for payload in _AUTH_SESSIONS_FALLBACK.values():
                if payload.get("id") == user["id"]:
                    payload["plan_tier"] = new_tier
            print(
                f"[stripe webhook] {etype}: user={user['id']} status={status} → tier={new_tier}",
                file=sys.stderr,
            )

        elif etype == "customer.subscription.deleted":
            user = _resolve_user_for_event(obj, customer_id)
            if not user:
                return {"ok": True, "type": etype, "ignored": True}
            _user_store.set_user_subscription(user["id"], None, tier="free")
            try:
                _user_store.refresh_user_plan_in_tokens(user["id"], "free")
            except Exception:
                import traceback as _tb
                _tb.print_exc()
            for payload in _AUTH_SESSIONS_FALLBACK.values():
                if payload.get("id") == user["id"]:
                    payload["plan_tier"] = "free"
            print(
                f"[stripe webhook] subscription.deleted: user={user['id']} → free",
                file=sys.stderr,
            )

        else:
            # invoice.paid, invoice.payment_failed, etc. — Stripe handles
            # dunning, we just log so it shows up in server output.
            print(f"[stripe webhook] {etype}: no-op", file=sys.stderr)
    except Exception as exc:
        # Returning 500 makes Stripe retry — but a buggy handler can retry
        # forever. Log the trace, return 200, so it goes to the dead-letter
        # tab in the Stripe dashboard for manual inspection.
        import traceback as _tb
        _tb.print_exc()
        print(f"[stripe webhook] {etype}: handler raised {exc}", file=sys.stderr)
        return {"ok": False, "type": etype, "error": str(exc)}

    return {"ok": True, "type": etype}


def _clear_phases_after(phase: int):
    """Clear results and state from phase+1 onwards."""
    if phase < 1:
        _clear_phases_after(1)
        return
    clear_map = {
        1: ("jobs", "scored", "applications", "tracker_path", "tracker_data", "report"),
        2: ("scored", "applications", "tracker_path", "tracker_data", "report"),
        3: ("applications", "tracker_path", "tracker_data", "report"),
        4: ("applications", "tracker_path", "tracker_data", "report"),
        5: ("tracker_path", "tracker_data", "report"),
        6: ("report",),
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
    # Empty fallbacks: phase2_discover_jobs / _resolve_effective_titles will
    # fall back to profile.target_titles for empty `titles`, and an empty
    # location is treated as "no location filter" by SearchFilters. We no
    # longer pin generic "Engineer" / "United States" placeholders here —
    # they leaked into the SQL filter even when the resume said otherwise.
    titles = [
        t.strip()
        for t in (_S.get("job_titles") or "").split(",")
        if t.strip()
    ]
    loc = (_S.get("location") or "").strip()
    params = request.query_params if request else {}
    deep = str(params.get("deep", "")).lower() in ("1", "true", "yes")
    append = str(params.get("append", "")).lower() in ("1", "true", "yes")
    force_live = str(params.get("force", "")).lower() in ("1", "true", "yes") or append
    # Consume the one-shot flag set by /api/pipeline/reset. The whole point of
    # Reset Run is to find NEW jobs — without this, the next search hits the
    # same persistent index with the same query and returns identical top-N
    # rows. Pop+set ensures the flag fires exactly once: the next phase-2 run
    # after the reset gets fresh ingestion, subsequent runs do not.
    if _S.get("force_phase2_next_run"):
        force_live = True
        _S["force_phase2_next_run"] = False

    def _fn():
        prov = _make_provider()
        offset = len(_S.get("jobs") or []) if append else 0
        # Push every user-declared filter into the DB-level query so the
        # cap (max_scrape_jobs) reflects post-filter results, not pre-.
        bl = tuple(c.strip() for c in (_S.get("blacklist") or "").split(",") if c.strip())
        wl = tuple(c.strip() for c in (_S.get("whitelist") or "").split(",") if c.strip())
        result = phase2_discover_jobs(
            _S["profile"], titles, loc, prov,
            use_simplify=_S.get("use_simplify", True),
            max_jobs=_S.get("max_scrape_jobs", 50),
            days_old=_S.get("days_old", 30),
            education_filter=_S.get("education_filter") or None,
            include_unknown_education=_S.get("include_unknown_education", True),
            deep_search=deep,
            force_live=force_live,
            offset=offset,
            blacklist=bl,
            whitelist=wl,
            citizenship_filter=_S.get("citizenship_filter") or "all",
            experience_levels=_S.get("experience_levels") or None,
        )
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
            _S["jobs"], _S["profile"], prov, min_score=50,
            experience_levels=_S.get("experience_levels") or None,
            include_unknown_experience=_S.get("include_unknown_experience", True),
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
                    format_profile=_primary_format_profile(),
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
        # Phase 5 is a *curation* step, not an auto-applier. We surface the
        # top N high-confidence picks (N = llm_score_limit, the same limit
        # the user already tunes on the Settings page for LLM scoring) with
        # tailored resumes from Phase 4 attached, ready for manual submission.
        # Real auto-submission lived in phase5_simulate_submission, which was
        # a randomized stub — removing it here so the user isn't misled by
        # fake "Applied" rows in the tracker.
        apps    = _S["applications"] or []
        limit   = max(1, int(_S.get("llm_score_limit", 10) or 10))
        already = _load_existing_applications(_session_output_dir())

        ranked = sorted(apps, key=lambda j: float(j.get("score") or 0), reverse=True)

        results: list = []
        picked = 0
        for job in ranked:
            key = (str(job.get("company", "")).lower(), str(job.get("title", "")).lower())
            if key in already:
                results.append({
                    **job,
                    "status": "Skipped",
                    "confirmation": "N/A",
                    "notes": "Already applied — skipped",
                })
                continue
            if picked < limit:
                picked += 1
                results.append({
                    **job,
                    "status": "Manual Required",
                    "confirmation": "N/A",
                    "notes": f"High-confidence pick — top {picked} of {limit}",
                })
            else:
                results.append({
                    **job,
                    "status": "Skipped",
                    "confirmation": "N/A",
                    "notes": f"Below top {limit} picks",
                })

        skipped_total = len(results) - picked
        print(f"  🎯 Top {picked} high-confidence picks (limit={limit})", flush=True)
        if skipped_total:
            print(f"  ⏭️  {skipped_total} below cutoff or already applied", flush=True)

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
        # write_file=False → no .xlsx is generated; the tracker lives entirely
        # in session state and renders as an in-page spreadsheet on the SPA.
        apps   = _S["applications"] or []
        result = phase6_update_tracker(apps, write_file=False)
        _S["tracker_data"] = result
        _S["tracker_path"] = None       # no file artifact under this flow
        return result

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
        # write_file=False → no .md report is generated; the SPA renders the
        # markdown text inline. SMTP notification (when configured) still fires.
        prov   = _make_provider()
        apps   = _S["applications"] or []
        report = phase7_run_report(apps, None, prov, output_dir=None, write_file=False)
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
def phase2_cache_clear(request: Request):
    # Auth-gate: the cache files (sample_jobs_quick.json / sample_jobs_deep.json)
    # are SERVER-WIDE under RESOURCES_DIR — wiping them affects every user's
    # next Phase 2 run. Letting an anonymous visitor delete them was a bug.
    _require_auth_user(request)
    sid = _S.session_id()
    for cache in (RESOURCES_DIR / "sample_jobs_quick.json", RESOURCES_DIR / "sample_jobs_deep.json"):
        cache.unlink(missing_ok=True)
    # Hold the per-session lock for the entire state mutation so a concurrent
    # extraction / phase worker can't observe a half-wiped state between
    # individual `_S[k] = None` assignments.
    with _session_lock(sid):
        for k in ("jobs", "scored", "applications", "tracker_path"):
            _S[k] = None
        _S["tailored_map"] = {}
        for phase in (2, 3, 4, 5, 6, 7):
            _S["done"].discard(phase)
            _S["error"].pop(phase, None)
    return {"ok": True}


# ── Ollama helpers ────────────────────────────────────────────────────────────


# Tracks any in-flight Ollama model pull (server-side) so the UI can render a
# live progress indicator and other users see the same status. OLLAMA_URL and
# DEFAULT_OLLAMA_MODEL are hoisted near the top of the file because
# _default_state() references DEFAULT_OLLAMA_MODEL at module-init time.
_OLLAMA_PULLS: dict[str, dict] = {}
_OLLAMA_PULL_LOCK = threading.Lock()


def _ollama_models_snapshot() -> dict:
    """Best-effort: list the models currently pulled on the server's Ollama."""
    import urllib.request as _ur
    import json as _json
    try:
        resp = _ur.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3)
        data = _json.loads(resp.read().decode())
        models = data.get("models", []) or []
        return {
            "running": True,
            "names": [m.get("name", "") for m in models],
            "bases": {n.split(":")[0] for n in (m.get("name", "") for m in models)},
            "models": [
                {
                    "name":    m.get("name", ""),
                    "size_gb": round(m.get("size", 0) / 1e9, 1),
                    "family":  m.get("details", {}).get("family", ""),
                    "params":  m.get("details", {}).get("parameter_size", ""),
                }
                for m in models
            ],
        }
    except Exception as exc:
        return {"running": False, "names": [], "bases": set(), "models": [], "error": str(exc)}


def _is_model_pulled(model: str, snapshot: dict | None = None) -> bool:
    snap = snapshot if snapshot is not None else _ollama_models_snapshot()
    if not snap.get("running"):
        return False
    if model in snap["names"]:
        return True
    base = model.split(":")[0]
    return base in snap["bases"]


def _ollama_pull_via_api(model: str):
    """Pull a model via Ollama's HTTP /api/pull — no `ollama` CLI needed.

    Yields parsed JSON progress dicts as Ollama emits them. Designed to be
    consumed both by the SSE endpoint (live UI updates) and the background
    boot-time auto-puller (status tracking only).
    """
    import urllib.request as _ur
    import json as _json
    body = _json.dumps({"name": model, "stream": True}).encode()
    req = _ur.Request(
        f"{OLLAMA_URL}/api/pull",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    # No timeout — model pulls can run for many minutes.
    with _ur.urlopen(req) as resp:
        for line in resp:
            line = line.strip()
            if not line:
                continue
            try:
                yield _json.loads(line.decode())
            except _json.JSONDecodeError:
                continue


def _begin_background_pull(model: str) -> dict:
    """Start a background pull for `model` if one isn't already running.

    Returns the (possibly already-existing) pull-state dict so callers can
    poll progress.
    """
    with _OLLAMA_PULL_LOCK:
        existing = _OLLAMA_PULLS.get(model)
        if existing and existing.get("status") in ("starting", "pulling"):
            return existing
        state = {
            "model":    model,
            "status":   "starting",
            "started":  time.time(),
            "percent":  0,
            "stage":    "",
            "error":    None,
            "completed": None,
        }
        _OLLAMA_PULLS[model] = state

    def _runner():
        try:
            state["status"] = "pulling"
            for evt in _ollama_pull_via_api(model):
                # Ollama emits {"status": "pulling manifest" / "downloading X" /
                # "verifying digest" / "success", ...} with optional
                # "completed"/"total" byte counters.
                state["stage"] = str(evt.get("status") or "")
                completed = evt.get("completed")
                total = evt.get("total")
                if isinstance(completed, (int, float)) and isinstance(total, (int, float)) and total > 0:
                    state["percent"] = max(0, min(100, int(completed / total * 100)))
                if "error" in evt and evt["error"]:
                    raise RuntimeError(str(evt["error"]))
                if state["stage"] == "success":
                    state["percent"] = 100
                    break
            state["status"] = "done"
            state["completed"] = time.time()
        except Exception as exc:
            state["status"] = "error"
            state["error"] = f"{type(exc).__name__}: {exc}"
            state["completed"] = time.time()

    threading.Thread(target=_runner, daemon=True, name=f"ollama-pull-{model}").start()
    return state


@app.on_event("startup")
def _ensure_default_ollama_model() -> None:
    """At app boot: if Ollama is reachable but the default model isn't pulled,
    kick off a background pull so users hitting Settings see the model ready
    (or at least 'pulling') instead of an empty dropdown."""

    def _boot_pull():
        # Wait briefly for the server to be ready and for transient network blips.
        time.sleep(2)
        snap = _ollama_models_snapshot()
        if not snap.get("running"):
            print(f"[ollama] not reachable at {OLLAMA_URL} — skipping auto-pull")
            return
        if _is_model_pulled(DEFAULT_OLLAMA_MODEL, snap):
            print(f"[ollama] default model {DEFAULT_OLLAMA_MODEL!r} already pulled")
            return
        print(f"[ollama] auto-pulling default model {DEFAULT_OLLAMA_MODEL!r} from {OLLAMA_URL}")
        _begin_background_pull(DEFAULT_OLLAMA_MODEL)

    threading.Thread(target=_boot_pull, daemon=True, name="ollama-boot-pull").start()


@app.get("/api/ollama/status")
def ollama_status():
    model = _S.get("ollama_model", DEFAULT_OLLAMA_MODEL) or DEFAULT_OLLAMA_MODEL
    snap = _ollama_models_snapshot()
    pulled = _is_model_pulled(model, snap)
    pull_state = _OLLAMA_PULLS.get(model)
    return {
        "running":   snap.get("running", False),
        "pulled":    pulled,
        "model":     model,
        "host":      OLLAMA_URL,
        "is_server": True,    # tells the SPA this is server-side, not user-local
        "available": sorted(snap.get("bases", set()) or []),
        "models":    snap.get("models", []),
        "pull":      pull_state,   # {status, percent, stage, error} if a pull is in progress
        "error":     snap.get("error"),
    }


@app.post("/api/ollama/ensure")
def ollama_ensure():
    """Idempotent: ensure the session-configured model is pulled. If it's
    already pulled, no-op. Otherwise kick off a background pull and return
    the pull-state so the UI can poll."""
    model = (_S.get("ollama_model") or DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL
    snap = _ollama_models_snapshot()
    if not snap.get("running"):
        return {"ok": False, "reason": "ollama_unreachable", "host": OLLAMA_URL, "error": snap.get("error")}
    if _is_model_pulled(model, snap):
        return {"ok": True, "model": model, "already_pulled": True}
    state = _begin_background_pull(model)
    return {"ok": True, "model": model, "already_pulled": False, "pull": state}


@app.get("/api/ollama/pull")
def ollama_pull():
    """Stream a model pull via Ollama's HTTP /api/pull — works wherever the
    Ollama daemon is reachable, no `ollama` CLI binary required on the server.
    Also registers the pull in the global tracker so other clients see status.
    """
    model = _S.get("ollama_model", DEFAULT_OLLAMA_MODEL) or DEFAULT_OLLAMA_MODEL

    # Reflect this pull in the shared tracker so /api/ollama/status sees it.
    with _OLLAMA_PULL_LOCK:
        _OLLAMA_PULLS[model] = {
            "model": model, "status": "pulling", "started": time.time(),
            "percent": 0, "stage": "starting", "error": None, "completed": None,
        }
    tracker = _OLLAMA_PULLS[model]

    def _stream():
        yield _sse({"type": "start", "model": model, "host": OLLAMA_URL})
        try:
            for evt in _ollama_pull_via_api(model):
                stage = str(evt.get("status") or "")
                completed = evt.get("completed")
                total = evt.get("total")
                pct = None
                if isinstance(completed, (int, float)) and isinstance(total, (int, float)) and total > 0:
                    pct = max(0, min(100, int(completed / total * 100)))
                # Mirror into shared tracker
                tracker["stage"] = stage
                if pct is not None:
                    tracker["percent"] = pct
                if "error" in evt and evt["error"]:
                    raise RuntimeError(str(evt["error"]))
                yield _sse({
                    "type": "log",
                    "line": stage + (f" — {pct}%" if pct is not None else ""),
                    "stage": stage,
                    "percent": pct,
                })
                if stage == "success":
                    break
            tracker["status"] = "done"
            tracker["percent"] = 100
            tracker["completed"] = time.time()
            yield _sse({"type": "done", "model": model})
        except GeneratorExit:
            # Client disconnected. Don't kill the in-flight HTTP pull —
            # it's safe to let Ollama keep going and complete in the background.
            raise
        except Exception as exc:
            tracker["status"] = "error"
            tracker["error"] = f"{type(exc).__name__}: {exc}"
            tracker["completed"] = time.time()
            yield _sse({"type": "error", "message": tracker["error"]})

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Port resolution: read JOBS_AI_PORT from env (loaded from .env at the
    # top of this file), default 8000 for production deploys (Pi systemd
    # unit). Local devs whose port 8000 is already taken (e.g. whisper)
    # can drop `JOBS_AI_PORT=8001` into their .env to override without
    # touching code.
    port = int(os.environ.get("JOBS_AI_PORT", "8000"))
    host = os.environ.get("JOBS_AI_HOST", "0.0.0.0")
    print(f"Jobs AI — starting on http://localhost:{port}")
    uvicorn.run("app:app", host=host, port=port, reload=False)
