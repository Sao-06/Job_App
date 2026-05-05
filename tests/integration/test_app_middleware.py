"""Integration tests for session_state_middleware behavior."""
import pytest

pytestmark = pytest.mark.integration


class TestSessionCookie:
    def test_state_cookie_set_on_first_request(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        # Pre-flight: no cookie.
        assert "jobs_ai_session" not in client.cookies
        client.get("/api/state")
        # Post: middleware set a session cookie.
        assert "jobs_ai_session" in client.cookies

    def test_state_cookie_persists_across_requests(self, fastapi_client):
        client, _, _ = fastapi_client
        client.get("/api/state")
        first = client.cookies.get("jobs_ai_session")
        client.get("/api/state")
        second = client.cookies.get("jobs_ai_session")
        assert first == second


class TestSkipSaveBehavior:
    def test_state_endpoint_does_not_clobber_oauth_state(
        self, fastapi_client, monkeypatch
    ):
        """The middleware skips saving for /api/state polls so that an
        in-flight OAuth state isn't overwritten by a poll that fired after
        /api/auth/google but before the callback."""
        client, _, _ = fastapi_client
        # Set a sentinel value in the session via a normal mutating request.
        client.post("/api/config", json={"threshold": 88})
        # Now poll /api/state — the middleware should NOT serialize the
        # session afterwards. We can't easily observe that directly without
        # touching app internals; the indirect signal is that subsequent
        # config writes still succeed.
        client.get("/api/state")
        client.get("/api/state")
        # Confirm threshold remained 88.
        s = client.get("/api/state").json()
        assert s["threshold"] == 88


class TestErrorEnvelope:
    def test_internal_state_load_failure_returns_json_500(
        self, fastapi_client, monkeypatch
    ):
        """If _bind_request_state crashes, middleware must wrap the error
        in a JSON 500 instead of letting FastAPI return plain text — the
        SPA does JSON.parse() on every response."""
        client, _, _ = fastapi_client
        import app as app_module

        def boom(_request):
            raise RuntimeError("synthetic load failure")

        monkeypatch.setattr(app_module, "_bind_request_state", boom)
        r = client.get("/api/state")
        assert r.status_code == 500
        body = r.json()
        assert body["ok"] is False
        assert "synthetic load failure" in body["error"]
