"""Integration tests for the auth endpoints."""
import pytest

pytestmark = pytest.mark.integration


class TestSignup:
    def test_creates_user_and_sets_cookie(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/auth/signup",
                         json={"email": "new@example.com",
                               "password": "secret-123"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        # Auth cookie was set.
        assert "jobs_ai_auth" in client.cookies

    def test_rejects_short_password(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/auth/signup",
                         json={"email": "x@example.com", "password": "abc"})
        body = r.json()
        assert body["ok"] is False

    def test_rejects_invalid_email(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/auth/signup",
                         json={"email": "not-an-email", "password": "secret-123"})
        body = r.json()
        assert body["ok"] is False
        assert "valid email" in body["error"].lower()

    def test_rejects_duplicate_email(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        # The fixture already created tester@example.com.
        r = client.post("/api/auth/signup",
                         json={"email": "tester@example.com",
                               "password": "secret-123"})
        body = r.json()
        assert body["ok"] is False
        assert "exists" in body["error"].lower()


class TestLogin:
    def test_correct_password(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/auth/login",
                         json={"email": "tester@example.com",
                               "password": "correct-horse-staple"})
        body = r.json()
        assert body["ok"] is True
        assert body["user"]["email"] == "tester@example.com"
        assert "jobs_ai_auth" in client.cookies

    def test_wrong_password(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/auth/login",
                         json={"email": "tester@example.com",
                               "password": "wrong-password"})
        body = r.json()
        assert body["ok"] is False

    def test_unknown_email(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/auth/login",
                         json={"email": "ghost@example.com",
                               "password": "anything"})
        body = r.json()
        assert body["ok"] is False

    def test_missing_fields(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/auth/login", json={"email": "", "password": ""})
        body = r.json()
        assert body["ok"] is False


class TestLogout:
    def test_clears_auth_cookie(self, fastapi_client):
        client, _, _ = fastapi_client
        # Pre: auth cookie present.
        assert "jobs_ai_auth" in client.cookies
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        # /api/state must now report no user.
        s = client.get("/api/state").json()
        assert s["user"] is None

    def test_logout_when_already_logged_out(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        # Should still respond 200 — idempotent.
        r = client.post("/api/auth/logout")
        assert r.status_code == 200


class TestGoogleOAuthDummy:
    def test_init_url_when_dummy_enabled(self, fastapi_client, monkeypatch):
        client, _, _ = fastapi_client
        client.cookies.clear()
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.setenv("GOOGLE_OAUTH_DEV_DUMMY", "1")
        r = client.get("/api/auth/google")
        body = r.json()
        # In dummy mode the init returns a relative-path URL pointing at the
        # callback with code=dummy_code.
        assert body["ok"] is True
        assert "dummy_code" in body["url"]


class TestRequireAuth:
    @pytest.mark.parametrize("path,method", [
        # /api/resume/upload requires a multipart file — FastAPI returns 422
        # at validation time, before middleware runs. Tested separately.
        ("/api/resume/demo", "POST"),
        ("/api/resume/content", "GET"),
        ("/api/profile", "GET"),
        ("/api/config", "POST"),
        ("/api/reset", "POST"),
        ("/api/jobs/action", "POST"),
        ("/api/feedback", "POST"),
    ])
    def test_returns_401_without_auth_cookie(self, fastapi_client, path, method):
        client, _, _ = fastapi_client
        client.cookies.clear()
        if method == "GET":
            r = client.get(path)
        else:
            r = client.post(path, json={})
        assert r.status_code == 401

    def test_resume_upload_requires_auth(self, fastapi_client):
        # Multipart POST with a real file — middleware should still 401
        # before the handler runs. (Note: FastAPI validates body BEFORE
        # middleware, so we need to send a file to get past that check.)
        client, _, _ = fastapi_client
        client.cookies.clear()
        files = {"file": ("r.txt", b"resume body", "text/plain")}
        r = client.post("/api/resume/upload", files=files)
        assert r.status_code == 401
