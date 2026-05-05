"""Integration tests for /api/dev/* endpoints (dev gating, plan flips, runtime knobs)."""
import pytest

pytestmark = pytest.mark.integration


class TestDevGate:
    def test_403_for_non_dev(self, fastapi_client):
        client, _, _ = fastapi_client
        r = client.get("/api/dev/overview")
        assert r.status_code == 403

    def test_200_for_dev(self, dev_client):
        client, _, _ = dev_client
        r = client.get("/api/dev/overview")
        assert r.status_code == 200

    def test_dev_users_list(self, dev_client):
        client, _, _ = dev_client
        r = client.get("/api/dev/users")
        assert r.status_code == 200
        users = r.json()["users"]
        emails = {u["email"] for u in users}
        assert "tester@example.com" in emails


class TestDevPlanFlip:
    def test_grant_pro_endpoint(self, dev_client, tmp_db):
        client, user_id, _ = dev_client
        r = client.post(f"/api/dev/users/{user_id}/plan", json={"tier": "pro"})
        assert r.status_code == 200
        assert tmp_db.get_user_by_id(user_id)["plan_tier"] == "pro"

    def test_invalid_tier_400(self, dev_client):
        client, user_id, _ = dev_client
        r = client.post(f"/api/dev/users/{user_id}/plan",
                         json={"tier": "platinum"})
        assert r.status_code == 400

    def test_non_dev_blocked(self, fastapi_client):
        client, user_id, _ = fastapi_client
        r = client.post(f"/api/dev/users/{user_id}/plan", json={"tier": "pro"})
        assert r.status_code == 403


class TestDevRuntime:
    def test_get_runtime_state(self, dev_client):
        client, _, _ = dev_client
        r = client.get("/api/dev/runtime")
        assert r.status_code == 200
        body = r.json()
        assert "runtime" in body
        assert "env" in body

    def test_set_maintenance_flag(self, dev_client, monkeypatch):
        client, _, _ = dev_client
        # Reset via monkeypatch so a failed assertion can't leak the flag
        # into subsequent tests.
        import app as app_module
        monkeypatch.setitem(app_module._RUNTIME, "maintenance", False)
        r = client.post("/api/dev/runtime", json={"maintenance": True})
        assert r.status_code == 200
        assert r.json()["runtime"]["maintenance"] is True


class TestDevSession:
    def test_session_detail_for_dev(self, dev_client):
        client, _, _ = dev_client
        # First request guarantees the session cookie is set.
        client.get("/api/state")
        sid = client.cookies.get("jobs_ai_session")
        assert sid, "fastapi_client should always set jobs_ai_session"
        r = client.get(f"/api/dev/session/{sid}")
        assert r.status_code == 200

    def test_session_reset(self, dev_client):
        client, _, _ = dev_client
        client.post("/api/config", json={"threshold": 90})
        client.get("/api/state")
        sid = client.cookies.get("jobs_ai_session")
        assert sid
        r = client.post(f"/api/dev/session/{sid}/reset")
        assert r.status_code == 200


class TestFeedback:
    def test_submits_feedback(self, fastapi_client):
        client, _, _ = fastapi_client
        r = client.post("/api/feedback",
                         json={"message": "Test feedback message"})
        assert r.status_code == 200

    def test_rejects_empty_message(self, fastapi_client):
        client, _, _ = fastapi_client
        r = client.post("/api/feedback", json={"message": "  "})
        assert r.status_code == 400
