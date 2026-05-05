"""Integration tests for /api/jobs/ask and /api/atlas/chat/stream."""
import pytest

pytestmark = pytest.mark.integration


class TestJobsAsk:
    def test_returns_chat_reply(self, fastapi_client, patched_provider, seed_jobs):
        client, _, _ = fastapi_client
        patched_provider.chat_response = "Atlas says: focus on Verilog projects."
        jid = seed_jobs(count=1, company="Acme", title="FPGA Intern")[0]
        r = client.post("/api/jobs/ask",
                         json={"job_id": jid, "message": "What should I emphasize?"})
        assert r.status_code == 200
        body = r.json()
        assert "Verilog" in body["reply"]
        assert body["job"]["co"] == "Acme"

    def test_404_for_unknown_job(self, fastapi_client, patched_provider):
        client, _, _ = fastapi_client
        r = client.post("/api/jobs/ask",
                         json={"job_id": "no-such-job", "message": "hi"})
        assert r.status_code == 404

    def test_400_for_empty_message(self, fastapi_client, patched_provider, seed_jobs):
        client, _, _ = fastapi_client
        jid = seed_jobs(count=1)[0]
        r = client.post("/api/jobs/ask", json={"job_id": jid, "message": "  "})
        assert r.status_code == 400

    def test_unauthenticated(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/jobs/ask", json={"job_id": "x", "message": "y"})
        assert r.status_code == 401


class TestAtlasChatStream:
    def test_emits_start_delta_done(self, fastapi_client, patched_provider, read_sse_frames):
        client, _, _ = fastapi_client
        patched_provider.chat_response = "Two-word reply."
        with client.stream("POST", "/api/atlas/chat/stream",
                            json={"message": "What's my plan?"}) as resp:
            assert resp.status_code == 200
            frames = list(read_sse_frames(resp))
        types = [f.get("type") for f in frames]
        # Stream lifecycle: a start, at least one delta, then done.
        assert types[0] == "start"
        assert types[-1] == "done"
        assert any(t == "delta" for t in types)

    def test_400_for_empty_message(self, fastapi_client, patched_provider):
        client, _, _ = fastapi_client
        r = client.post("/api/atlas/chat/stream", json={"message": ""})
        assert r.status_code == 400
