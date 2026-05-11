"""End-to-end plan-gate behavior for the four mode gates + Ollama preservation.

Verifies the matrix:
  • mode='anthropic' (Claude CLI):
    - free user           → 402 plan_required
    - Pro user            → 200
    - developer user      → 200
    - _CLI_HEALTHY=False  → 402 even for Pro

  • mode='ollama' + local model:
    - free user                              → 200 (no regression)
    - any user                               → 200

  • mode='ollama' + *-cloud model:
    - free user                              → 402 (existing gate)
    - Pro user                               → 200
    - developer user                         → 200

  • Switching modes per-user:
    - Pro user can flip anthropic ↔ ollama ↔ ollama+cloud without errors

  • api_key sanitation:
    - api_key not in /api/state
    - api_key not leaking when POSTed to /api/config
    - /api/dev/runtime has no anthropic_key_present
"""
import pytest

pytestmark = pytest.mark.integration


# ── Helpers ───────────────────────────────────────────────────────────────────

def _downgrade_to_free(tmp_db, user_id, token, email="tester@example.com"):
    """Downgrade a user to free tier AND refresh the cached auth_token blob.

    New users default to plan_tier='pro' during the everyone-is-Pro testing
    phase (session_store.create_user sets the column default to 'pro').
    The auth-token lookup reads from the cached user_json, not the users table,
    so both must be updated together.
    """
    tmp_db.set_user_plan_tier(user_id, "free")
    tmp_db.create_auth_token(token, user_id, {
        "id": user_id,
        "email": email,
        "is_developer": False,
        "plan_tier": "free",
    })


# ── Anthropic (Claude CLI) gate ───────────────────────────────────────────────


def test_free_user_blocked_on_anthropic_mode(fastapi_client, tmp_db):
    """Free user POSTing mode='anthropic' to /api/config gets 402 plan_required."""
    client, user_id, token = fastapi_client
    _downgrade_to_free(tmp_db, user_id, token)
    r = client.post("/api/config", json={"mode": "anthropic"})
    assert r.status_code == 402, r.text
    body = r.json()
    assert body.get("code") == "plan_required", body


def test_pro_user_admitted_on_anthropic_mode(fastapi_client):
    """Pro user can select mode='anthropic' — fastapi_client defaults to Pro."""
    client, _, _ = fastapi_client
    r = client.post("/api/config", json={"mode": "anthropic"})
    assert r.status_code == 200, r.text


def test_dev_user_admitted_on_anthropic_mode(dev_client):
    """Developer can always select mode='anthropic' regardless of plan tier."""
    client, _, _ = dev_client
    r = client.post("/api/config", json={"mode": "anthropic"})
    assert r.status_code == 200, r.text


def test_pro_blocked_when_cli_unhealthy(fastapi_client, monkeypatch):
    """Even a Pro user is denied mode='anthropic' when _CLI_HEALTHY=False.

    _can_use_claude imports _CLI_HEALTHY inside its body on each call, so
    monkeypatching the module attribute is sufficient.
    """
    client, _, _ = fastapi_client
    monkeypatch.setattr("pipeline.providers._CLI_HEALTHY", False)
    r = client.post("/api/config", json={"mode": "anthropic"})
    assert r.status_code == 402, r.text
    assert r.json().get("code") == "plan_required", r.json()


# ── Ollama (local) preservation ───────────────────────────────────────────────


def test_free_user_can_set_local_ollama(fastapi_client, tmp_db):
    """Free users can always set mode='ollama' with a local model — no regression."""
    client, user_id, token = fastapi_client
    _downgrade_to_free(tmp_db, user_id, token)
    r = client.post("/api/config", json={
        "mode": "ollama",
        "ollama_model": "smollm2:135m",
    })
    assert r.status_code == 200, r.text


def test_free_user_default_state_is_ollama(fastapi_client, tmp_db):
    """A fresh free user lands on mode='ollama'. This is the existing Free-tier
    behavior — must not regress after the Anthropic gate flip."""
    client, user_id, token = fastapi_client
    _downgrade_to_free(tmp_db, user_id, token)
    r = client.get("/api/state")
    assert r.status_code == 200, r.text
    state = r.json()
    assert state.get("mode") == "ollama"
    assert state.get("ollama_model"), "ollama_model should be set for free users"


# ── Ollama (cloud) gate — existing entitlement, must be unchanged ─────────────


def test_free_user_blocked_on_cloud_ollama(fastapi_client, tmp_db):
    """Free users cannot set a *-cloud Ollama model (pre-existing gate).

    Verifies the Anthropic gate flip did not accidentally remove or weaken
    this parallel gate.
    """
    client, user_id, token = fastapi_client
    _downgrade_to_free(tmp_db, user_id, token)
    r = client.post("/api/config", json={
        "mode": "ollama",
        "ollama_model": "gemma4:31b-cloud",
    })
    assert r.status_code == 402, r.text
    assert r.json().get("code") == "plan_required", r.json()


def test_pro_user_can_set_cloud_ollama(fastapi_client):
    """Pro user can set a *-cloud Ollama model — fastapi_client defaults to Pro."""
    client, _, _ = fastapi_client
    r = client.post("/api/config", json={
        "mode": "ollama",
        "ollama_model": "gemma4:31b-cloud",
    })
    assert r.status_code == 200, r.text


def test_dev_user_can_set_cloud_ollama(dev_client):
    """Developer can set *-cloud Ollama model regardless of plan tier."""
    client, _, _ = dev_client
    r = client.post("/api/config", json={
        "mode": "ollama",
        "ollama_model": "gemma4:31b-cloud",
    })
    assert r.status_code == 200, r.text


# ── Mode switching ─────────────────────────────────────────────────────────────


def test_pro_user_can_flip_between_modes(fastapi_client):
    """Pro user can move between all three entitled modes in one session without
    state corruption. fastapi_client defaults to plan_tier='pro'."""
    client, _, _ = fastapi_client
    # ollama local
    r = client.post("/api/config", json={"mode": "ollama", "ollama_model": "smollm2:135m"})
    assert r.status_code == 200, f"ollama local failed: {r.text}"
    # ollama cloud
    r = client.post("/api/config", json={"mode": "ollama", "ollama_model": "gemma4:31b-cloud"})
    assert r.status_code == 200, f"ollama cloud failed: {r.text}"
    # anthropic CLI
    r = client.post("/api/config", json={"mode": "anthropic"})
    assert r.status_code == 200, f"anthropic failed: {r.text}"
    # Back to ollama local
    r = client.post("/api/config", json={"mode": "ollama", "ollama_model": "smollm2:135m"})
    assert r.status_code == 200, f"return-trip failed: {r.text}"


# ── api_key removed from /api/config whitelist and /api/state ─────────────────


def test_api_key_not_in_state_shape(fastapi_client):
    """api_key must not appear in the /api/state response (Tasks 9+10 dropped it)."""
    client, _, _ = fastapi_client
    r = client.get("/api/state")
    assert r.status_code == 200, r.text
    assert "api_key" not in r.json(), (
        f"api_key leaked into /api/state: {list(r.json().keys())}"
    )


def test_posting_api_key_does_not_leak_into_state(fastapi_client):
    """POSTing api_key to /api/config should be silently dropped (not in whitelist).

    The key must not appear in the subsequent /api/state response.
    """
    client, _, _ = fastapi_client
    r = client.post("/api/config", json={"api_key": "sk-ant-fake-key-123"})
    # A 200 is fine (unknown keys are silently dropped); a 400 is also fine.
    assert r.status_code in (200, 400), r.text
    state = client.get("/api/state").json()
    assert "api_key" not in state, (
        f"api_key leaked into /api/state after POST: {list(state.keys())}"
    )


# ── Dev Ops runtime no longer exposes anthropic_key_present ───────────────────


def test_dev_runtime_no_longer_exposes_anthropic_key_present(dev_client):
    """/api/dev/runtime must not contain the key 'anthropic_key_present'.

    Tasks 9+10 removed the ANTHROPIC_API_KEY machinery — the field would be
    stale/misleading and is expected to be gone.
    """
    client, _, _ = dev_client
    r = client.get("/api/dev/runtime")
    assert r.status_code == 200, r.text
    payload = r.json()

    def _walk(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                assert k != "anthropic_key_present", (
                    f"anthropic_key_present leaked at {path}.{k}: {list(obj.keys())}"
                )
                _walk(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _walk(v, f"{path}[{i}]")

    _walk(payload)
