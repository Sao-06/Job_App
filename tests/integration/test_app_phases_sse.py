"""Integration tests for the /api/phase/{N}/{run,rerun} SSE endpoints."""
import pytest

pytestmark = pytest.mark.integration


class TestPhase1SSE:
    def test_run_emits_done_frame(self, fastapi_client, patched_provider,
                                    seed_resume, read_sse_frames):
        client, _, _ = fastapi_client
        seed_resume()
        with client.stream("GET", "/api/phase/1/run") as resp:
            assert resp.status_code == 200
            frames = list(read_sse_frames(resp))
        types = [f.get("type") for f in frames]
        assert "start" in types
        assert "done" in types

    def test_run_without_resume_returns_400(self, fastapi_client, patched_provider):
        client, _, _ = fastapi_client
        r = client.get("/api/phase/1/run")
        assert r.status_code == 400


class TestPhasePlanGate:
    """The Anthropic-mode gate runs in two layers:

      1. Load-time coerce in _load_session_state (app.py:1252): when a
         free user's session has mode='anthropic', mode is flipped back
         to 'ollama' BEFORE the request handler sees it. This is the
         primary gate.
      2. Belt-and-suspenders SSE gate in _run_phase_sse (app.py:2172):
         if mode='anthropic' somehow reaches the phase handler, emit a
         plan_required error frame and bail.

      Because layer 1 fires on every state load, layer 2 is unreachable
      via the normal HTTP flow — it only fires if someone races a state
      write between load and phase execution. The original test of layer 2
      via /api/phase/1/run could never trigger that race deterministically,
      so it was asserting on a code path that production users never reach.

      This test now verifies BOTH layers directly:
      - layer 1 by reloading the state and checking the coerce ran;
      - layer 2 by calling _claude_gate_error directly with the same
        free-user dict the SSE handler would receive.
    """

    def test_load_coerce_demotes_anthropic_to_ollama_for_free_user(
        self, fastapi_client, tmp_db
    ):
        """Layer 1: free user with saved mode='anthropic' → coerced to 'ollama' on load."""
        import app as app_module

        client, user_id, token = fastapi_client
        tmp_db.set_user_plan_tier(user_id, "free")
        tmp_db.create_auth_token(token, user_id, {
            "id": user_id,
            "email": "tester@example.com",
            "is_developer": False,
            "plan_tier": "free",
        })
        app_module._AUTH_SESSIONS_FALLBACK.pop(token, None)

        sid = client.cookies.get("jobs_ai_session", "")
        store = app_module._session_store
        state = store.peek_state(sid) or app_module._default_state()
        state["mode"] = "anthropic"
        state["user"] = {
            "id": user_id,
            "email": "tester@example.com",
            "is_developer": False,
            "plan_tier": "free",
        }
        store.save_state(sid, state)

        # _load_session_state must coerce mode back to ollama for the free user.
        loaded = app_module._load_session_state(sid)
        assert loaded.get("mode") == "ollama", \
            f"Expected mode='ollama' after coerce, got {loaded.get('mode')!r}"

    def test_claude_gate_error_blocks_free_user_with_plan_required(self):
        """Layer 2: the SSE belt-and-suspenders gate function — given a free
        user dict, must return a 402 plan_required error envelope."""
        import app as app_module

        free_user = {
            "id": "test-user",
            "email": "tester@example.com",
            "is_developer": False,
            "plan_tier": "free",
        }
        err = app_module._claude_gate_error(free_user)
        assert err is not None, "Expected gate to block free user"
        assert err.get("status_code") == 402
        body = err.get("body") or {}
        assert body.get("code") == "plan_required", body


class TestPhaseConcurrencyCap:
    def test_second_run_is_rejected_when_first_in_flight(
        self, fastapi_client, patched_provider, seed_resume, read_sse_frames
    ):
        """Per-session lock: only one phase SSE may run at a time. The
        second concurrent /api/phase/N/run gets an error frame."""
        client, _, _ = fastapi_client
        seed_resume()
        import app as app_module
        sid = client.cookies.get("jobs_ai_session") or "test-sid"
        with app_module._session_running_guard:
            app_module._session_running_phases.setdefault(sid, set()).add(1)
        try:
            with client.stream("GET", "/api/phase/1/run") as resp:
                frames = list(read_sse_frames(resp, timeout=2.0))
        finally:
            with app_module._session_running_guard:
                app_module._session_running_phases.get(sid, set()).discard(1)
        err = [f for f in frames if f.get("type") == "error"]
        assert err
        assert "Another phase is already running" in (err[0].get("message") or "")


class TestMaintenanceFlag:
    def test_maintenance_blocks_phase_runs(self, fastapi_client, patched_provider,
                                              seed_resume, read_sse_frames, monkeypatch):
        client, _, _ = fastapi_client
        seed_resume()
        import app as app_module
        # monkeypatch.setitem auto-restores on teardown — no manual cleanup
        # required, so a failed assertion can't leak the flag into other tests.
        monkeypatch.setitem(app_module._RUNTIME, "maintenance", True)
        with client.stream("GET", "/api/phase/1/run") as resp:
            frames = list(read_sse_frames(resp, timeout=2.0))
        err = [f for f in frames if f.get("type") == "error"]
        assert err
        assert "maintenance" in (err[0].get("message") or "").lower()
