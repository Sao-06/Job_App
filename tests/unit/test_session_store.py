"""Tests for session_store.SQLiteSessionStore — schema, peek, save, auth tokens."""
from pathlib import Path

import pytest

from app import _default_state
from session_store import SQLiteSessionStore, _hash_token, normalize_state

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return SQLiteSessionStore(tmp_path / "store.sqlite3", default_state_factory=_default_state)


# ── Schema / migrations ─────────────────────────────────────────────────────


class TestInitSchema:
    def test_tables_created(self, store):
        with store._connect() as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert {"users", "sessions", "session_state", "auth_tokens"}.issubset(tables)

    def test_users_has_is_developer_and_plan_tier_columns(self, store):
        with store._connect() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        assert "is_developer" in cols
        assert "plan_tier" in cols

    def test_init_schema_idempotent(self, store, tmp_path):
        # Re-opening the same DB must not error.
        SQLiteSessionStore(tmp_path / "store.sqlite3", default_state_factory=_default_state)
        SQLiteSessionStore(tmp_path / "store.sqlite3", default_state_factory=_default_state)


# ── Users + plan tier ───────────────────────────────────────────────────────


class TestUsers:
    def test_create_and_lookup(self, store):
        uid = store.create_user(email="a@b.com", password_hash="hash")
        u = store.get_user_by_email("a@b.com")
        assert u is not None
        assert u["id"] == uid
        # Testing phase: every new signup starts on Pro.
        assert u["plan_tier"] == "pro"
        assert u["is_developer"] is False

    def test_get_by_id(self, store):
        uid = store.create_user(email="a@b.com", password_hash="hash")
        u = store.get_user_by_id(uid)
        assert u is not None and u["email"] == "a@b.com"

    def test_get_by_google_id(self, store):
        store.create_user(email="g@b.com", google_id="goog-1")
        u = store.get_user_by_google_id("goog-1")
        assert u is not None and u["email"] == "g@b.com"

    def test_set_user_developer(self, store):
        uid = store.create_user(email="a@b.com", password_hash="hash")
        store.set_user_developer(uid, True)
        assert store.get_user_by_id(uid)["is_developer"] is True
        store.set_user_developer(uid, False)
        assert store.get_user_by_id(uid)["is_developer"] is False

    def test_set_plan_tier(self, store):
        uid = store.create_user(email="a@b.com", password_hash="hash")
        store.set_user_plan_tier(uid, "pro")
        assert store.get_user_by_id(uid)["plan_tier"] == "pro"

    def test_set_plan_tier_rejects_invalid(self, store):
        uid = store.create_user(email="a@b.com", password_hash="hash")
        with pytest.raises(ValueError):
            store.set_user_plan_tier(uid, "platinum")

    def test_list_users_returns_created_at(self, store):
        store.create_user(email="a@b.com", password_hash="h")
        store.create_user(email="b@b.com", password_hash="h")
        users = store.list_users()
        assert len(users) == 2
        assert all("created_at" in u for u in users)


# ── Auth tokens ─────────────────────────────────────────────────────────────


class TestAuthTokens:
    def test_token_stored_as_hash_not_plaintext(self, store):
        uid = store.create_user(email="a@b.com", password_hash="h")
        store.create_auth_token("plain-bearer-token", uid, {"id": uid})
        with store._connect() as conn:
            (stored,) = conn.execute("SELECT token FROM auth_tokens").fetchone()
        # Stored value is the SHA-256 hex digest, not the raw token.
        assert stored != "plain-bearer-token"
        assert stored == _hash_token("plain-bearer-token")
        assert len(stored) == 64

    def test_get_auth_user_round_trip(self, store):
        uid = store.create_user(email="a@b.com", password_hash="h")
        payload = {"id": uid, "email": "a@b.com", "plan_tier": "free"}
        store.create_auth_token("bearer-1", uid, payload)
        result = store.get_auth_user("bearer-1")
        assert result is not None
        assert result["email"] == "a@b.com"
        assert result["id"] == uid

    def test_delete_auth_token(self, store):
        uid = store.create_user(email="a@b.com", password_hash="h")
        store.create_auth_token("bearer-1", uid, {"id": uid})
        store.delete_auth_token("bearer-1")
        assert store.get_auth_user("bearer-1") is None

    def test_get_auth_user_returns_none_for_unknown_token(self, store):
        assert store.get_auth_user("not-a-real-token") is None

    def test_legacy_unhashed_tokens_purged_at_init(self, tmp_path):
        # A pre-migration row would have a non-hex 64-char token. Boot should
        # delete it, so a subsequent lookup returns None.
        path = tmp_path / "legacy.sqlite3"
        s1 = SQLiteSessionStore(path, default_state_factory=_default_state)
        uid = s1.create_user(email="a@b.com", password_hash="h")
        # Insert a legacy-style raw-token row by hand.
        with s1._connect() as conn:
            conn.execute(
                "INSERT INTO auth_tokens (token, user_id, user_json, created_at) "
                "VALUES (?, ?, ?, '2026-01-01')",
                ("not-a-sha256-digest", uid, "{}"),
            )
        # Re-init triggers the purge.
        s2 = SQLiteSessionStore(path, default_state_factory=_default_state)
        with s2._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0]
        assert count == 0


# ── Session state ───────────────────────────────────────────────────────────


class TestSessionState:
    def test_peek_returns_none_for_unknown(self, store):
        assert store.peek_state("never-seen") is None

    def test_peek_does_not_insert(self, store):
        # peek_state must NOT create a row — anonymous sessions should not
        # leave traces in the sessions table.
        store.peek_state("anon-1")
        with store._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM sessions WHERE id='anon-1'").fetchone()[0]
        assert count == 0

    def test_get_state_inserts_session_row(self, store):
        store.get_state("sid-1")
        with store._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM sessions WHERE id='sid-1'").fetchone()[0]
        assert count == 1

    def test_save_then_peek_round_trips(self, store):
        state = _default_state()
        state["mode"] = "anthropic"
        state["done"].add(1)
        state["done"].add(2)
        store.save_state("sid-2", state)
        loaded = store.peek_state("sid-2")
        assert loaded is not None
        assert loaded["mode"] == "anthropic"
        assert loaded["done"] == {1, 2}

    def test_normalize_state_re_hydrates_sets(self):
        raw = {"done": [1, 2], "liked_ids": [], "hidden_ids": ["x"], "extracting_ids": []}
        norm = normalize_state(raw)
        for k in ("done", "liked_ids", "hidden_ids", "extracting_ids"):
            assert isinstance(norm[k], set)

    def test_associate_session_with_user(self, store):
        uid = store.create_user(email="a@b.com", password_hash="h")
        store.get_state("sid-3")  # ensures session row exists
        store.associate_session_with_user("sid-3", uid)
        assert store.get_session_user_id("sid-3") == uid

    def test_get_user_sessions(self, store):
        uid = store.create_user(email="a@b.com", password_hash="h")
        store.get_state("sid-4")
        store.associate_session_with_user("sid-4", uid)
        store.get_state("sid-5")
        store.associate_session_with_user("sid-5", uid)
        sessions = store.get_user_sessions(uid)
        assert set(sessions) == {"sid-4", "sid-5"}

    def test_reset_state(self, store):
        s = _default_state()
        s["mode"] = "anthropic"
        store.save_state("sid-6", s)
        store.reset_state("sid-6")
        loaded = store.peek_state("sid-6")
        assert loaded["mode"] == "ollama"  # default

    def test_list_sessions_excludes_anonymous(self, store):
        # Tied-to-user session: visible.
        uid = store.create_user(email="a@b.com", password_hash="h")
        store.get_state("auth-sid")
        store.associate_session_with_user("auth-sid", uid)
        # Anonymous session: present in `sessions` but user_id NULL.
        store.get_state("anon-sid")
        listed = store.list_sessions()
        ids = {s["id"] for s in listed}
        assert "auth-sid" in ids
        assert "anon-sid" not in ids
