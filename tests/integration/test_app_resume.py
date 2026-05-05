"""Integration tests for the resume CRUD endpoints."""
import pytest

pytestmark = pytest.mark.integration


class TestResumeDemo:
    def test_loads_demo_resume(self, fastapi_client, patched_provider):
        client, _, _ = fastapi_client
        r = client.post("/api/resume/demo")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["filename"] == "demo_resume.txt"
        assert body["extracting"] is True

    def test_resume_appears_in_state(self, fastapi_client, patched_provider, wait_extraction):
        client, _, _ = fastapi_client
        client.post("/api/resume/demo")
        s = wait_extraction(client)
        resumes = s["resumes"]
        assert len(resumes) == 1
        assert resumes[0]["filename"] == "demo_resume.txt"
        assert resumes[0]["primary"] is True


class TestResumeUpload:
    def test_uploads_text_resume(self, fastapi_client, patched_provider, fixtures_dir):
        client, _, _ = fastapi_client
        path = fixtures_dir / "resumes" / "sample_text.txt"
        with open(path, "rb") as f:
            r = client.post(
                "/api/resume/upload",
                files={"file": (path.name, f, "text/plain")},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["filename"] == "sample_text.txt"
        assert body["length"] > 0

    def test_first_upload_becomes_primary(self, fastapi_client, patched_provider,
                                            wait_extraction, fixtures_dir):
        client, _, _ = fastapi_client
        path = fixtures_dir / "resumes" / "sample_text.txt"
        with open(path, "rb") as f:
            client.post("/api/resume/upload",
                        files={"file": (path.name, f, "text/plain")})
        s = wait_extraction(client)
        assert s["resumes"][0]["primary"] is True

    def test_second_upload_not_primary(self, fastapi_client, patched_provider,
                                          wait_extraction, fixtures_dir):
        client, _, _ = fastapi_client
        path = fixtures_dir / "resumes" / "sample_text.txt"
        for _ in range(2):
            with open(path, "rb") as f:
                client.post("/api/resume/upload",
                            files={"file": (path.name, f, "text/plain")})
        s = wait_extraction(client)
        assert len(s["resumes"]) == 2
        primary = [r for r in s["resumes"] if r["primary"]]
        assert len(primary) == 1


class TestResumeContent:
    def test_returns_primary_text(self, fastapi_client, patched_provider,
                                    wait_extraction, fixtures_dir):
        client, _, _ = fastapi_client
        path = fixtures_dir / "resumes" / "sample_text.txt"
        with open(path, "rb") as f:
            client.post("/api/resume/upload",
                        files={"file": (path.name, f, "text/plain")})
        wait_extraction(client)
        r = client.get("/api/resume/content")
        assert r.status_code == 200
        text = r.json()["text"]
        assert "Jane Tester" in text


class TestResumeText:
    def test_save_text_replaces_primary(self, fastapi_client, patched_provider, wait_extraction):
        client, _, _ = fastapi_client
        client.post("/api/resume/demo")
        wait_extraction(client)
        new_text = "Updated Resume Body\n[Jane Tester]\nemail@example.com"
        r = client.post("/api/resume/text", json={"text": new_text})
        assert r.status_code == 200
        body = client.get("/api/resume/content").json()
        assert "Updated Resume Body" in body["text"]

    def test_rejects_empty_text(self, fastapi_client, patched_provider, wait_extraction):
        client, _, _ = fastapi_client
        client.post("/api/resume/demo")
        wait_extraction(client)
        r = client.post("/api/resume/text", json={"text": "   "})
        assert r.status_code == 400


class TestResumePrimary:
    def test_primary_switch_clears_downstream(self, fastapi_client, patched_provider,
                                                 wait_extraction, fixtures_dir):
        client, _, _ = fastapi_client
        path = fixtures_dir / "resumes" / "sample_text.txt"
        ids = []
        for _ in range(2):
            with open(path, "rb") as f:
                resp = client.post("/api/resume/upload",
                                    files={"file": (path.name, f, "text/plain")})
            ids.append(resp.json()["id"])
        wait_extraction(client)
        r = client.post(f"/api/resume/primary/{ids[1]}")
        assert r.status_code == 200
        s = client.get("/api/state").json()
        primary_id = next(r["id"] for r in s["resumes"] if r["primary"])
        assert primary_id == ids[1]


class TestResumeRename:
    def test_renames_resume(self, fastapi_client, patched_provider, wait_extraction):
        client, _, _ = fastapi_client
        resp = client.post("/api/resume/demo")
        rid = resp.json()["id"]
        wait_extraction(client)
        r = client.post(f"/api/resume/rename/{rid}",
                         json={"filename": "my-cool-resume.pdf"})
        assert r.status_code == 200
        s = client.get("/api/state").json()
        names = {r["filename"] for r in s["resumes"]}
        assert "my-cool-resume.pdf" in names

    def test_rejects_empty_filename(self, fastapi_client, patched_provider):
        client, _, _ = fastapi_client
        resp = client.post("/api/resume/demo")
        rid = resp.json()["id"]
        r = client.post(f"/api/resume/rename/{rid}", json={"filename": ""})
        assert r.status_code == 400

    def test_404_for_unknown_id(self, fastapi_client, patched_provider):
        client, _, _ = fastapi_client
        r = client.post("/api/resume/rename/no-such-id",
                         json={"filename": "x.pdf"})
        assert r.status_code == 404


class TestResumeDelete:
    def test_deletes_and_promotes_next_primary(self, fastapi_client, patched_provider,
                                                   wait_extraction, fixtures_dir):
        client, _, _ = fastapi_client
        path = fixtures_dir / "resumes" / "sample_text.txt"
        ids = []
        for _ in range(2):
            with open(path, "rb") as f:
                resp = client.post("/api/resume/upload",
                                    files={"file": (path.name, f, "text/plain")})
            ids.append(resp.json()["id"])
        wait_extraction(client)
        client.delete(f"/api/resume/{ids[0]}")
        s = client.get("/api/state").json()
        assert len(s["resumes"]) == 1
        assert s["resumes"][0]["primary"] is True
        assert s["resumes"][0]["id"] == ids[1]

    def test_404_for_unknown_id(self, fastapi_client, patched_provider):
        client, _, _ = fastapi_client
        r = client.delete("/api/resume/no-such")
        assert r.status_code == 404
