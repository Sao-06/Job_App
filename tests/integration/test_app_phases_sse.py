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
    def test_anthropic_phase_blocked_for_free_user(self, fastapi_client, patched_provider,
                                                       seed_resume, read_sse_frames):
        """Mode='anthropic' set on a session whose user later loses Pro must
        be caught by the belt-and-suspenders gate inside _run_phase_sse."""
        import app as app_module
        client, _, _ = fastapi_client
        seed_resume()
        sid = client.cookies.get("jobs_ai_session", "")
        # Resume seeding routes through _save_bound_state, which routes to
        # _memory_sessions when state["user"] hasn't been set (the fixture
        # creates an auth_token but doesn't go through login). Write directly
        # to where the state actually lives.
        state = app_module._memory_sessions.get(sid) or {}
        state["mode"] = "anthropic"
        app_module._memory_sessions[sid] = state
        with client.stream("GET", "/api/phase/1/run") as resp:
            frames = list(read_sse_frames(resp))
        plan_errs = [f for f in frames
                     if f.get("type") == "error" and f.get("code") == "plan_required"]
        assert plan_errs


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
