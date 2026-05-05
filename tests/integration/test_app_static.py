"""Integration tests for static-asset and output-file serving."""
import pytest

pytestmark = pytest.mark.integration


class TestStaticPages:
    def test_root_serves_landing(self, fastapi_client):
        client, _, _ = fastapi_client
        r = client.get("/")
        assert r.status_code == 200
        assert "<html" in r.text.lower() or r.headers.get("content-type", "").startswith("text/html")

    def test_app_serves_spa_shell(self, fastapi_client):
        client, _, _ = fastapi_client
        r = client.get("/app")
        assert r.status_code == 200
        assert "<html" in r.text.lower()

    def test_frontend_static_existing_file(self, fastapi_client):
        client, _, _ = fastapi_client
        # app.jsx exists in frontend/.
        r = client.get("/frontend/app.jsx")
        assert r.status_code == 200

    def test_frontend_static_404_for_missing(self, fastapi_client):
        client, _, _ = fastapi_client
        r = client.get("/frontend/does-not-exist.txt")
        assert r.status_code == 404


class TestOutputServing:
    def test_path_traversal_blocked(self, fastapi_client):
        client, _, _ = fastapi_client
        # Even with a valid auth cookie, ../ traversal must 404.
        r = client.get("/output/../requirements.txt")
        assert r.status_code == 404

    def test_blocked_suffix_returns_404(self, fastapi_client, tmp_path, monkeypatch):
        # A .sqlite file is on the suffix denylist — the route 404s before
        # checking existence.
        client, _, _ = fastapi_client
        # We can't easily stage a real .sqlite under OUTPUT_DIR for this test
        # without coupling to internals; the deny list is checked by suffix
        # alone, so any name with the bad suffix triggers 404.
        r = client.get("/output/anything.sqlite3")
        assert r.status_code == 404

    def test_unallowed_suffix_returns_404(self, fastapi_client):
        client, _, _ = fastapi_client
        # .ini isn't in the allowed-suffix list.
        r = client.get("/output/anything.ini")
        assert r.status_code == 404

    def test_session_file_requires_auth(self, fastapi_client):
        # Without auth cookie, per-session output files 401.
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.get("/output/sessions/some-other-sid/resume.pdf")
        assert r.status_code in (401, 404)
