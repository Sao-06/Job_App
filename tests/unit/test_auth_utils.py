"""Tests for auth_utils — bcrypt + Google OAuth dummy flow."""
import os

import pytest

import auth_utils

pytestmark = pytest.mark.unit


class TestPasswordHashing:
    def test_hash_round_trip(self):
        h = auth_utils.hash_password("hunter2")
        assert auth_utils.verify_password("hunter2", h) is True
        assert auth_utils.verify_password("wrong-password", h) is False

    def test_each_hash_is_unique_due_to_salt(self):
        h1 = auth_utils.hash_password("same-password")
        h2 = auth_utils.hash_password("same-password")
        assert h1 != h2
        assert auth_utils.verify_password("same-password", h1) is True
        assert auth_utils.verify_password("same-password", h2) is True

    def test_empty_hash_returns_false(self):
        assert auth_utils.verify_password("x", "") is False
        assert auth_utils.verify_password("x", None) is False


class TestGoogleAuthDummy:
    def test_dummy_url_when_no_client_id_and_dummy_enabled(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("GOOGLE_OAUTH_DEV_DUMMY", "1")
        url, state = auth_utils.get_google_auth_url("http://localhost:8000/cb")
        assert url == "/api/auth/google/callback?code=dummy_code&state=dummy_state"
        assert state == "dummy_state"

    def test_raises_without_dummy_flag(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_DEV_DUMMY", raising=False)
        with pytest.raises(RuntimeError, match="Google OAuth is not configured"):
            auth_utils.get_google_auth_url("http://localhost:8000/cb")

    def test_dummy_token_verification(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.setenv("GOOGLE_OAUTH_DEV_DUMMY", "1")
        info = auth_utils.verify_google_token("dummy_code", "http://localhost:8000/cb", "dummy_state")
        assert info["email"] == "dev@example.com"
        assert info["sub"] == "dummy_google_id"

    def test_dummy_rejected_when_flag_unset(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_DEV_DUMMY", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        # Falls through into the real flow which will fail without
        # google-auth-oauthlib being properly configured. Confirm we don't
        # silently accept the dummy code anyway.
        with pytest.raises(Exception):
            auth_utils.verify_google_token("dummy_code", "http://localhost:8000/cb", "dummy_state")
