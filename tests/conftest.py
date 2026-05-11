"""
tests/conftest.py
─────────────────
Top-level pytest fixtures. Loaded once per test session, before any test
module is imported, so the env-var setup at the top of this file
neutralises the import-time side effects in pipeline/config.py and
pipeline/providers.py.

Critical fixture inventory:

* tmp_db          — a fresh SQLiteSessionStore on a tmp_path file
* fastapi_client  — TestClient with overridden _session_store / _user_store
                    plus a pre-created authenticated test user (cookie attached)
* dev_client      — like fastapi_client, but the user is promoted to developer
* patched_provider — sets app._PROVIDER_OVERRIDE to a FakeProvider
* fake_source     — clears pipeline.sources.registry._REGISTRY for the test
* freezer         — freezegun.freeze_time wrapper at a stable timestamp
* read_sse_frames — generator that decodes data: frames from a streaming response
* wait_extraction — polling helper for the resume bg-extraction thread
* seed_jobs       — bulk-seed the persistent job index with synthetic rows

Test-environment env vars are sticky for the whole pytest session because
``setdefault`` runs at module import. Tests that need the unset path
must explicitly ``monkeypatch.delenv``.
"""
from __future__ import annotations

import os

# Set BEFORE any project import — these are read at module import time:
#   * pipeline.config:111 reads JOBS_AI_SKIP_MIGRATION before deciding whether
#     to call migrate_db_path() (which would touch output/ on the real disk).
#   * app.py @startup hook reads JOBS_AI_DISABLE_INGESTION before kicking
#     off the 60-second parallel backfill.
os.environ.setdefault("JOBS_AI_SKIP_MIGRATION", "1")
os.environ.setdefault("JOBS_AI_DISABLE_INGESTION", "1")
# Localhost-safe Ollama URL so OllamaProvider construction never hits a real
# network when accidentally reached during tests; respx intercepts the call.
os.environ.setdefault("OLLAMA_URL", "http://ollama-test.local:11434")
# Dummy Stripe envs so is_configured()=True paths can be exercised; tests
# that want to verify the not-configured fork override these explicitly.
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy_for_unit_tests")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_dummy_for_unit_tests")

import json
import secrets
import sys
import time
from pathlib import Path

# Project root must be on sys.path so `import app`, `import session_store`,
# `import auth_utils` work from any test sub-directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest


# ── Cached bcrypt hash ───────────────────────────────────────────────────────
# bcrypt.hashpw is intentionally slow (~25-200 ms) so hashing the same test
# password on every fastapi_client setup adds up to ~10s across the full
# suite. Hash once at module load and reuse.
TEST_PASSWORD = "correct-horse-staple"


def _build_test_password_hash() -> str:
    from auth_utils import hash_password
    return hash_password(TEST_PASSWORD)


_TEST_PASSWORD_HASH = _build_test_password_hash()


# ── Fixtures available everywhere ────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path):
    """Build a SQLiteSessionStore on a fresh tmp file. Yields the store; the
    tmp_path teardown automatically deletes the underlying SQLite file.
    """
    from session_store import SQLiteSessionStore
    from app import _default_state
    db_path = tmp_path / "test.sqlite3"
    store = SQLiteSessionStore(db_path, default_state_factory=_default_state)
    yield store


def _build_authed_client(tmp_db, monkeypatch, *, is_developer: bool = False,
                         plan_tier: str = "pro"):
    """Shared builder for fastapi_client / dev_client / pro_client. Returns
    ``(client, user_id, token)`` with both auth and session cookies attached.

    Default plan_tier is "pro" — the app is currently in testing-phase mode
    where every user is upgraded to Pro automatically (see the migration in
    pipeline/migrations.py + create_user default).
    """
    import app as app_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "_session_store", tmp_db)
    monkeypatch.setattr(app_module, "_user_store", tmp_db)
    monkeypatch.setattr(app_module, "_memory_sessions", {})
    # Per-session lock + concurrent-phase trackers must also reset so a leak
    # from a prior test doesn't poison this one's plan-gate / lock checks.
    monkeypatch.setattr(app_module, "_session_locks", {})
    monkeypatch.setattr(app_module, "_session_running_phases", {})

    user_id = tmp_db.create_user(
        email="tester@example.com",
        password_hash=_TEST_PASSWORD_HASH,
    )
    if is_developer:
        tmp_db.set_user_developer(user_id, True)
    # Always sync the DB plan_tier to the requested tier so the users row and
    # the cached auth_user payload agree. Without this, tests that opt back
    # into the "free" tier would still see plan_tier='pro' from the column
    # default set by the migration.
    tmp_db.set_user_plan_tier(user_id, plan_tier)

    auth_user = {
        "id": user_id,
        "email": "tester@example.com",
        "is_developer": is_developer,
        "plan_tier": plan_tier,
    }
    token = secrets.token_urlsafe(32)
    tmp_db.create_auth_token(token, user_id, auth_user)

    client = TestClient(app_module.app)
    client.cookies.set("jobs_ai_auth", token)
    return client, user_id, token


@pytest.fixture
def fastapi_client(tmp_db, monkeypatch):
    """TestClient + tmp DB + an authenticated free-tier user."""
    client, user_id, token = _build_authed_client(tmp_db, monkeypatch)
    yield client, user_id, token
    client.close()


@pytest.fixture
def dev_client(tmp_db, monkeypatch):
    """TestClient + tmp DB + an authenticated developer."""
    client, user_id, token = _build_authed_client(
        tmp_db, monkeypatch, is_developer=True
    )
    yield client, user_id, token
    client.close()


@pytest.fixture
def patched_provider(monkeypatch):
    """Inject a FakeProvider via the _PROVIDER_OVERRIDE seam in app.py.
    Returns the FakeProvider instance so tests can mutate canned responses.
    """
    import app as app_module
    from tests.fakes import FakeProvider
    fake = FakeProvider()
    monkeypatch.setattr(app_module, "_PROVIDER_OVERRIDE", fake)
    yield fake


@pytest.fixture
def fake_source(monkeypatch):
    """Clear pipeline.sources.registry._REGISTRY for the test, return the
    FakeJobSource class so the test can register specific instances.
    """
    # pipeline/sources/__init__.py re-exports `registry` as a function name,
    # shadowing the module attribute even under `import ... as`. Reach the
    # actual module through sys.modules.
    import pipeline.sources.registry  # noqa: F401 — ensures module is loaded
    registry_mod = sys.modules["pipeline.sources.registry"]
    from tests.fakes import FakeJobSource
    monkeypatch.setattr(registry_mod, "_REGISTRY", {})
    yield FakeJobSource


@pytest.fixture
def freezer():
    """Freeze datetime at a stable, far-enough-in-the-future-for-the-data
    timestamp so freshness scoring + posted_at filters are deterministic.
    """
    from freezegun import freeze_time
    with freeze_time("2026-05-05T12:00:00Z") as f:
        yield f


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to ``tests/fixtures/``. Use for resume / job / stripe payloads."""
    return Path(__file__).resolve().parent / "fixtures"


# ── Shared helpers (returned as fixtures so tests can request them) ─────────


@pytest.fixture
def wait_extraction():
    """Returns a callable that polls /api/state until the bg resume
    extraction thread has finished, then returns the latest state dict."""
    def _wait(client, *, timeout: float = 2.0, poll: float = 0.01) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = client.get("/api/state").json()
            resumes = s.get("resumes") or []
            if resumes and not any(r.get("extracting") for r in resumes):
                return s
            time.sleep(poll)
        return client.get("/api/state").json()
    return _wait


@pytest.fixture
def read_sse_frames():
    """Returns a callable that decodes ``data:`` JSON frames from a streaming
    httpx response. Stops on EOF or *timeout* seconds (whichever comes first).
    """
    def _read(response, *, timeout: float = 5.0):
        deadline = time.time() + timeout
        buffer = ""
        for chunk in response.iter_text():
            if time.time() > deadline:
                break
            if not chunk:
                continue
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                for line in frame.splitlines():
                    if line.startswith("data:"):
                        payload = line[len("data:"):].strip()
                        if not payload:
                            continue
                        try:
                            yield json.loads(payload)
                        except json.JSONDecodeError:
                            continue
    return _read


@pytest.fixture
def seed_jobs(tmp_db):
    """Bulk-seed the persistent job index. Returns a callable; calling it
    inserts *count* synthetic ``RawJob`` rows and returns their ids.
    """
    def _seed(count: int = 5, **overrides) -> list[str]:
        from pipeline import job_repo
        from pipeline.sources.base import infer_metadata
        from tests.fakes import make_raw_job

        rows = []
        for i in range(count):
            company = overrides.pop("company", None) or f"Company{i % 3}"
            title = overrides.pop("title", None) or f"Engineer {i}"
            url = overrides.pop("url", None) or f"https://example{i}.com/jobs/{i}"
            rows.append(make_raw_job(company=company, title=title, url=url, **overrides))
        with tmp_db.connect() as conn:
            prepared = [infer_metadata(r) for r in rows]
            job_repo.upsert_many(conn, prepared)
            urls = [r["application_url"] for r in rows]
            placeholders = ",".join("?" * len(urls))
            ids = [
                row[0] for row in conn.execute(
                    f"SELECT id FROM job_postings WHERE canonical_url IN ({placeholders})",
                    urls,
                )
            ]
        return ids
    return _seed


@pytest.fixture
def seed_resume(fastapi_client, patched_provider, wait_extraction):
    """Combo fixture: load the demo resume on the fastapi_client and wait
    for the bg extraction thread to settle. Returns the post-settle state.
    """
    def _seed():
        client, _, _ = fastapi_client
        client.post("/api/resume/demo")
        return wait_extraction(client)
    return _seed


import json as _json_for_fixtures
import os as _os_for_fixtures
import textwrap as _textwrap_for_fixtures


@pytest.fixture
def claude_cli_bin(tmp_path, monkeypatch):
    """Write a fake `claude` shell script to tmp_path and prepend it to PATH.

    The fake responds to a small set of canned argvs so providers tests can
    exercise _run_cli without spawning the real CLI.

    Usage:
        def test_x(claude_cli_bin):
            claude_cli_bin.set_response("Hello world")   # text mode
            claude_cli_bin.set_json({"ok": True})        # json mode
            claude_cli_bin.set_error("auth failed", exit=1)
            claude_cli_bin.set_stream(["chunk one", "chunk two"])
    """
    state_file = tmp_path / "claude_state.json"
    state_file.write_text(_json_for_fixtures.dumps({
        "mode": "text", "text": "OK", "exit": 0, "stderr": "",
        "stream_chunks": [], "delay_s": 0,
    }))

    # Build the embedded script body using a plain string + substitution
    # to avoid f-string brace-doubling confusion.
    _SCRIPT_TEMPLATE = _textwrap_for_fixtures.dedent("""\
        #!/usr/bin/env python3
        import json, sys, time
        state = json.loads(open(STATE_FILE_PATH).read())
        time.sleep(state.get("delay_s", 0))
        if state.get("exit", 0) != 0:
            sys.stderr.write(state.get("stderr", ""))
            sys.exit(state["exit"])
        # Find --output-format
        argv = sys.argv[1:]
        out_fmt = "text"
        if "--output-format" in argv:
            i = argv.index("--output-format")
            if i + 1 < len(argv):
                out_fmt = argv[i + 1]
        if out_fmt == "stream-json":
            for chunk in state.get("stream_chunks", []):
                sys.stdout.write(json.dumps({
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": chunk},
                    },
                }) + "\\n")
                sys.stdout.flush()
            sys.stdout.write(json.dumps({
                "type": "result", "subtype": "success", "total_cost_usd": 0.001,
            }) + "\\n")
            sys.exit(0)
        # text or json output: print whatever 'text' field holds
        sys.stdout.write(state["text"])
        sys.exit(0)
    """)
    script_body = _SCRIPT_TEMPLATE.replace("STATE_FILE_PATH", repr(str(state_file)))

    script = tmp_path / "claude"
    script.write_text(script_body)
    script.chmod(0o755)

    class _Helper:
        def set_response(self, text):
            d = _json_for_fixtures.loads(state_file.read_text())
            d.update({"mode": "text", "text": text, "exit": 0})
            state_file.write_text(_json_for_fixtures.dumps(d))

        def set_json(self, obj):
            self.set_response(_json_for_fixtures.dumps(obj))

        def set_error(self, stderr, exit=1):
            d = _json_for_fixtures.loads(state_file.read_text())
            d.update({"exit": exit, "stderr": stderr})
            state_file.write_text(_json_for_fixtures.dumps(d))

        def set_stream(self, chunks):
            d = _json_for_fixtures.loads(state_file.read_text())
            d.update({"mode": "stream", "stream_chunks": list(chunks), "exit": 0})
            state_file.write_text(_json_for_fixtures.dumps(d))

        def set_delay(self, seconds):
            d = _json_for_fixtures.loads(state_file.read_text())
            d.update({"delay_s": seconds})
            state_file.write_text(_json_for_fixtures.dumps(d))

    monkeypatch.setenv("PATH", str(tmp_path) + _os_for_fixtures.pathsep + _os_for_fixtures.environ["PATH"])
    monkeypatch.setenv("CLAUDE_BIN", str(script))
    monkeypatch.setenv("CLAUDE_CLI_MODEL", "sonnet")  # deterministic for tests
    return _Helper()
