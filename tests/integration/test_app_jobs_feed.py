"""Integration tests for /api/jobs/feed cursor + filter behavior."""
import pytest

pytestmark = pytest.mark.integration


class TestJobsFeed:
    def test_returns_seeded_jobs(self, fastapi_client, seed_jobs):
        client, _, _ = fastapi_client
        seed_jobs(count=5)
        r = client.get("/api/jobs/feed")
        assert r.status_code == 200
        body = r.json()
        assert "jobs" in body
        assert "next_cursor" in body
        assert len(body["jobs"]) >= 1

    def test_payload_shape(self, fastapi_client, seed_jobs):
        client, _, _ = fastapi_client
        seed_jobs(count=3)
        body = client.get("/api/jobs/feed").json()
        for j in body["jobs"]:
            for k in ("id", "co", "role", "loc", "score", "url",
                      "remote", "salary", "exp", "edu", "cit",
                      "posted", "source"):
                assert k in j

    def test_limit_param(self, fastapi_client, seed_jobs):
        client, _, _ = fastapi_client
        seed_jobs(count=20)
        body = client.get("/api/jobs/feed?limit=5").json()
        assert len(body["jobs"]) <= 5

    def test_cursor_pagination(self, fastapi_client, seed_jobs):
        client, _, _ = fastapi_client
        seed_jobs(count=20)
        page1 = client.get("/api/jobs/feed?limit=3").json()
        if page1["next_cursor"]:
            page2 = client.get(
                f"/api/jobs/feed?limit=3&cursor={page1['next_cursor']}"
            ).json()
            ids1 = {j["id"] for j in page1["jobs"]}
            ids2 = {j["id"] for j in page2["jobs"]}
            assert ids1.isdisjoint(ids2)

    def test_remote_filter(self, fastapi_client, seed_jobs):
        client, _, _ = fastapi_client
        seed_jobs(count=5, remote=True)
        body = client.get("/api/jobs/feed?remote=1").json()
        assert all(j["remote"] for j in body["jobs"])

    def test_blacklist_overrides_state(self, fastapi_client, seed_jobs):
        client, _, _ = fastapi_client
        # 3 rows is enough — seed_jobs's company rotation covers Company0 / 1 / 2.
        seed_jobs(count=3)
        body = client.get("/api/jobs/feed?blacklist=Company0").json()
        cos = {j["co"].lower() for j in body["jobs"]}
        assert "company0" not in cos


class TestJobsAction:
    def test_like_unlike(self, fastapi_client):
        client, _, _ = fastapi_client
        client.post("/api/jobs/action",
                     json={"action": "like", "job_id": "job-1"})
        s = client.get("/api/state").json()
        assert "job-1" in s["liked_ids"]
        client.post("/api/jobs/action",
                     json={"action": "unlike", "job_id": "job-1"})
        s = client.get("/api/state").json()
        assert "job-1" not in s["liked_ids"]

    def test_hide_unhide(self, fastapi_client):
        client, _, _ = fastapi_client
        client.post("/api/jobs/action",
                     json={"action": "hide", "job_id": "job-2"})
        s = client.get("/api/state").json()
        assert "job-2" in s["hidden_ids"]
        client.post("/api/jobs/action",
                     json={"action": "unhide", "job_id": "job-2"})
        s = client.get("/api/state").json()
        assert "job-2" not in s["hidden_ids"]

    def test_unauthenticated(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/jobs/action",
                         json={"action": "like", "job_id": "x"})
        assert r.status_code == 401
