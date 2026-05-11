"""Integration tests for /api/state, /api/config, /api/reset."""
import pytest

pytestmark = pytest.mark.integration


# ── /api/state ──────────────────────────────────────────────────────────────


class TestState:
    def test_returns_full_payload_shape(self, fastapi_client):
        client, user_id, token = fastapi_client
        r = client.get("/api/state")
        assert r.status_code == 200
        data = r.json()
        # Top-level keys the SPA depends on.
        for k in ("done", "error", "elapsed", "has_resume", "mode",
                  "threshold", "job_titles", "location", "max_apps",
                  "blacklist", "whitelist", "experience_levels",
                  "is_dev", "user", "resumes", "applications"):
            assert k in data

    def test_user_present_when_authenticated(self, fastapi_client):
        client, user_id, _ = fastapi_client
        r = client.get("/api/state")
        user = r.json()["user"]
        assert user is not None
        assert user["email"] == "tester@example.com"
        # Testing phase: default fixture plan is Pro.
        assert user["plan_tier"] == "pro"

    def test_user_none_for_anonymous(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.get("/api/state")
        assert r.json()["user"] is None

    def test_default_mode_is_ollama(self, fastapi_client):
        client, _, _ = fastapi_client
        r = client.get("/api/state")
        assert r.json()["mode"] == "ollama"

    def test_plan_tier_mirrored(self, fastapi_client):
        client, _, _ = fastapi_client
        r = client.get("/api/state")
        d = r.json()
        # Testing phase: every user defaults to Pro.
        assert d["plan_tier"] == "pro"
        assert d["is_pro"] is True


# ── /api/config ─────────────────────────────────────────────────────────────


class TestConfig:
    # Keys that /api/state explicitly echoes back to the SPA. A handful of
    # whitelisted /api/config keys (e.g. force_customer_mode) are accepted
    # but not exposed in /api/state directly — they're tested separately.
    @pytest.mark.parametrize("key,value", [
        ("threshold", 80),
        ("job_titles", "FPGA Intern, Photonics Intern"),
        ("location", "Boston, MA"),
        ("max_apps", 5),
        ("max_scrape_jobs", 10),
        ("days_old", 14),
        ("cover_letter", True),
        ("blacklist", "Foo, Bar"),
        ("whitelist", "NVIDIA, Apple"),
        ("experience_levels", ["internship"]),
        ("education_filter", ["bachelors"]),
        ("include_unknown_education", False),
        ("citizenship_filter", "exclude_required"),
        ("use_simplify", False),
        ("llm_score_limit", 5),
        ("light_mode", True),
    ])
    def test_whitelisted_keys_persist(self, fastapi_client, key, value):
        client, _, _ = fastapi_client
        r = client.post("/api/config", json={key: value})
        assert r.status_code == 200
        # Verify it round-tripped through /api/state.
        s = client.get("/api/state").json()
        assert s.get(key) == value

    def test_force_customer_mode_accepted_by_config(self, fastapi_client):
        # force_customer_mode is whitelisted by /api/config but not echoed
        # back through /api/state. Just confirm the POST returns 200.
        client, _, _ = fastapi_client
        r = client.post("/api/config", json={"force_customer_mode": True})
        assert r.status_code == 200

    def test_unknown_keys_silently_dropped(self, fastapi_client):
        client, _, _ = fastapi_client
        r = client.post("/api/config", json={"hack_me": "yes"})
        assert r.status_code == 200
        s = client.get("/api/state").json()
        assert "hack_me" not in s

    def test_anthropic_mode_blocked_for_non_devs(self, fastapi_client):
        # Anthropic Claude is under active development — only developers can
        # select it (regardless of plan tier). Non-dev callers get 503 +
        # `coming_soon` so the SPA shows the right copy instead of an
        # "upgrade to Pro" upsell.
        client, _, _ = fastapi_client
        r = client.post("/api/config", json={"mode": "anthropic"})
        assert r.status_code == 503
        body = r.json()
        assert body.get("code") == "coming_soon"

    def test_demo_mode_rejected(self, fastapi_client):
        # `demo` was retired from the user-selectable mode whitelist —
        # DemoProvider still exists internally as the heuristic baseline /
        # Ollama-down fallback, but POST /api/config rejects it as 400.
        client, _, _ = fastapi_client
        r = client.post("/api/config", json={"mode": "demo"})
        assert r.status_code == 400

    def test_ollama_mode_allowed_for_free_tier(self, fastapi_client):
        client, _, _ = fastapi_client
        r = client.post("/api/config", json={"mode": "ollama"})
        assert r.status_code == 200

    def test_anthropic_mode_blocked_even_for_pro(self, fastapi_client, tmp_db):
        # Anthropic Claude is reserved for developers until launch — paying
        # Pro is no longer enough to access it. (When Claude ships, Pro users
        # get it included; until then this test asserts the gate.)
        client, user_id, token = fastapi_client
        tmp_db.set_user_plan_tier(user_id, "pro")
        tmp_db.create_auth_token(token, user_id, {
            "id": user_id, "email": "tester@example.com",
            "is_developer": False, "plan_tier": "pro",
        })
        r = client.post("/api/config", json={"mode": "anthropic"})
        assert r.status_code == 503
        assert r.json().get("code") == "coming_soon"

    def test_anthropic_mode_allowed_for_devs(self, dev_client):
        # Developers can still exercise the in-progress Claude integration so
        # they can test it before it ships to customers.
        client, _, _ = dev_client
        r = client.post("/api/config", json={"mode": "anthropic"})
        assert r.status_code == 200

    def test_unauthenticated_returns_401(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/config", json={"threshold": 70})
        assert r.status_code == 401


# ── /api/reset ──────────────────────────────────────────────────────────────


class TestReset:
    def test_resets_state(self, fastapi_client):
        client, _, _ = fastapi_client
        # Set a non-default value first.
        client.post("/api/config", json={"threshold": 90})
        assert client.get("/api/state").json()["threshold"] == 90
        # Reset.
        r = client.post("/api/reset")
        assert r.status_code == 200
        assert client.get("/api/state").json()["threshold"] == 75   # default

    def test_preserves_user(self, fastapi_client):
        client, _, _ = fastapi_client
        before = client.get("/api/state").json()["user"]
        client.post("/api/reset")
        after = client.get("/api/state").json()["user"]
        assert after is not None
        assert after["email"] == before["email"]

    def test_preserves_provider_settings(self, fastapi_client):
        client, _, _ = fastapi_client
        client.post("/api/config", json={"mode": "demo"})
        client.post("/api/config", json={"ollama_model": "mistral"})
        client.post("/api/reset")
        s = client.get("/api/state").json()
        assert s["mode"] == "demo"
        assert s["ollama_model"] == "mistral"

    def test_unauthenticated_returns_401(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/reset")
        assert r.status_code == 401
