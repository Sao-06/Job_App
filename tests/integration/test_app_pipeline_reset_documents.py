"""
Integration tests for the pipeline-reset and documents endpoints.

`/api/pipeline/reset` is the non-destructive companion to `/api/reset`:
it clears jobs/scoring/applications/tracker/report but preserves the
resume, profile, settings, and any generated documents.

`/api/documents` (+ /content, /rename, DELETE) is the SPA Documents page
backend — surfaces every artifact in the bound session's output dir,
with editor / rename / delete plumbing.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _output_dir_for(client) -> Path:
    """Resolve the bound session's output directory by introspecting the
    server module through the test client. The session id is stamped into
    the path; we cannot hard-code a constant."""
    import app as app_module
    sid = client.cookies.get("jobs_ai_session")
    assert sid, "fastapi_client should set a session cookie"
    out = app_module.OUTPUT_DIR / "sessions" / sid
    out.mkdir(parents=True, exist_ok=True)
    return out


# ── /api/pipeline/reset ─────────────────────────────────────────────────────


class TestPipelineReset:
    def test_clears_pipeline_results_only(self, fastapi_client):
        client, _, _ = fastapi_client
        # Bootstrap a /api/state poll so the session row is created.
        client.get("/api/state")
        # Plant pipeline + pref data into the session through /api/config
        # (which is the public mutation surface).
        client.post("/api/config", json={"threshold": 90, "job_titles": "test role"})
        # Force pipeline-state via a direct mutation through the test seam.
        import app as app_module
        sid = client.cookies.get("jobs_ai_session")
        state = app_module._memory_sessions.get(sid) or app_module._session_store.peek_state(sid)
        state["jobs"] = [{"id": "j1"}]
        state["scored"] = [{"id": "j1", "score": 80}]
        state["applications"] = [{"company": "X", "title": "Y", "status": "Applied"}]
        state["tracker_data"] = {"month": "2026-05", "rows": [{"n": 1}]}
        state["report"] = "stub report"
        state["tailored_map"] = {"j1": {"job": {}, "tailored": {}}}
        state["done"] = {1, 2, 3, 4}
        state["error"] = {2: "old error"}
        state["elapsed"] = {1: 2.0, 2: 5.0}
        if app_module._session_store is not None and (state.get("user") or {}).get("id"):
            app_module._session_store.save_state(sid, state)
        else:
            app_module._memory_sessions[sid] = state

        r = client.post("/api/pipeline/reset")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True

        s = client.get("/api/state").json()
        # Pipeline data wiped.
        assert s.get("scored_summary") in (None, {}, {"jobs": [], "total": 0,
                                                     "auto": 0, "manual": 0,
                                                     "below": 0, "filtered": 0,
                                                     "synthetic": True}) or \
               (s.get("scored_summary") or {}).get("total", 0) == 0
        assert s["applications"] == []
        assert s["tracker_data"] is None
        assert s["report"] in ("", None)
        # Phase 1 done preserved; phases 2..7 cleared.
        assert 1 in s["done"]
        for p in (2, 3, 4):
            assert p not in s["done"]
        # Errors and elapsed for phases >=2 cleared, phase 1 preserved.
        # (state.error keys are int-or-string depending on JSON round-trip;
        # we assert no '2'/'3' keys regardless of type.)
        err_keys = {str(k) for k in (s.get("error") or {}).keys()}
        assert "2" not in err_keys
        ela_keys = {str(k) for k in (s.get("elapsed") or {}).keys()}
        assert "2" not in ela_keys
        assert "1" in ela_keys                          # phase 1 elapsed kept
        # Provider / search-pref settings untouched.
        assert s["threshold"] == 90
        assert s["job_titles"] == "test role"

    def test_preserves_documents_on_disk(self, fastapi_client):
        client, _, _ = fastapi_client
        client.get("/api/state")
        out_dir = _output_dir_for(client)
        # Plant a fake "tailored resume" file directly on disk.
        artifact = out_dir / "Jane_Resume_Acme_Engineer.txt"
        artifact.write_text("placeholder", encoding="utf-8")
        assert artifact.exists()

        r = client.post("/api/pipeline/reset")
        assert r.status_code == 200

        # The whole point: document survives the pipeline reset.
        assert artifact.exists()
        assert artifact.read_text(encoding="utf-8") == "placeholder"

    def test_refuses_while_phase_running(self, fastapi_client):
        client, _, _ = fastapi_client
        client.get("/api/state")
        # Manually stamp a running phase via the same helpers /api/state polls.
        import app as app_module
        sid = client.cookies.get("jobs_ai_session")
        app_module._phase_progress_open(sid, 2)
        try:
            r = client.post("/api/pipeline/reset")
            assert r.status_code == 409
            assert "running" in (r.json().get("detail") or "").lower()
        finally:
            app_module._phase_progress_close(sid, 2)

    def test_unauthenticated_returns_401(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/pipeline/reset")
        assert r.status_code == 401


# ── /api/state.running_phases ───────────────────────────────────────────────


class TestRunningPhasesInState:
    def test_empty_when_no_phase_running(self, fastapi_client):
        client, _, _ = fastapi_client
        s = client.get("/api/state").json()
        assert s["running_phases"] == []

    def test_populated_while_phase_open(self, fastapi_client):
        client, _, _ = fastapi_client
        client.get("/api/state")
        import app as app_module
        sid = client.cookies.get("jobs_ai_session")
        app_module._phase_progress_open(sid, 3)
        app_module._phase_progress_log(sid, 3, "scoring 5/10 jobs")
        try:
            s = client.get("/api/state").json()
            running = s["running_phases"]
            assert len(running) == 1
            assert running[0]["phase"] == 3
            assert "scoring 5/10 jobs" in running[0]["recent_logs"]
            assert running[0]["elapsed_s"] >= 0
        finally:
            app_module._phase_progress_close(sid, 3)

    def test_clears_after_close(self, fastapi_client):
        client, _, _ = fastapi_client
        client.get("/api/state")
        import app as app_module
        sid = client.cookies.get("jobs_ai_session")
        app_module._phase_progress_open(sid, 2)
        app_module._phase_progress_close(sid, 2)
        s = client.get("/api/state").json()
        assert s["running_phases"] == []


# ── /api/documents ──────────────────────────────────────────────────────────


@pytest.fixture
def planted_docs(fastapi_client):
    """Drop a deterministic mix of artifacts in the session output dir so
    Documents tests have predictable rows to assert against."""
    client, _, _ = fastapi_client
    client.get("/api/state")
    out_dir = _output_dir_for(client)
    files = {
        "Jane_Resume_Acme_Engineer.tex":  "\\documentclass{article}\\begin{document}Hi\\end{document}",
        "Jane_Resume_Acme_Engineer.pdf":  b"%PDF-1.4 dummy",
        "Jane_CoverLetter_Acme.txt":      "Dear hiring team,\n",
        "Job_Applications_Tracker_2026-05.xlsx": b"PK fake xlsx",
        "20260507_job-application-run-report.md": "# Run report\n",
        "weird.dat":                      b"binary not allowed",
        ".hidden":                        b"hidden file",
    }
    for name, payload in files.items():
        target = out_dir / name
        if isinstance(payload, bytes):
            target.write_bytes(payload)
        else:
            target.write_text(payload, encoding="utf-8")
    # Files inside uploads/ should not be surfaced as documents.
    upl = out_dir / "uploads"
    upl.mkdir(parents=True, exist_ok=True)
    (upl / "primary.pdf").write_bytes(b"%PDF-1.4")
    return client, out_dir


class TestDocumentsList:
    def test_lists_only_allowed_artifacts(self, planted_docs):
        client, _ = planted_docs
        r = client.get("/api/documents")
        assert r.status_code == 200
        names = {d["name"] for d in r.json()["documents"]}
        # Allowed types surface.
        assert "Jane_Resume_Acme_Engineer.tex"          in names
        assert "Jane_Resume_Acme_Engineer.pdf"          in names
        assert "Jane_CoverLetter_Acme.txt"              in names
        assert "Job_Applications_Tracker_2026-05.xlsx"  in names
        assert "20260507_job-application-run-report.md" in names
        # Disallowed / hidden / uploads-folder are absent.
        assert "weird.dat" not in names
        assert ".hidden"   not in names
        assert "primary.pdf" not in names

    def test_classifies_kind(self, planted_docs):
        client, _ = planted_docs
        docs = {d["name"]: d for d in client.get("/api/documents").json()["documents"]}
        assert docs["Jane_Resume_Acme_Engineer.tex"]["kind"] == "resume"
        assert docs["Jane_CoverLetter_Acme.txt"]["kind"]     == "cover_letter"
        assert docs["Job_Applications_Tracker_2026-05.xlsx"]["kind"] == "tracker"
        assert docs["20260507_job-application-run-report.md"]["kind"] == "report"

    def test_marks_editable_correctly(self, planted_docs):
        client, _ = planted_docs
        docs = {d["name"]: d for d in client.get("/api/documents").json()["documents"]}
        assert docs["Jane_Resume_Acme_Engineer.tex"]["editable"] is True
        assert docs["Jane_CoverLetter_Acme.txt"]["editable"] is True
        assert docs["20260507_job-application-run-report.md"]["editable"] is True
        assert docs["Jane_Resume_Acme_Engineer.pdf"]["editable"] is False
        assert docs["Job_Applications_Tracker_2026-05.xlsx"]["editable"] is False

    def test_unauthenticated_returns_401(self, planted_docs):
        client, _ = planted_docs
        client.cookies.clear()
        r = client.get("/api/documents")
        assert r.status_code == 401


class TestDocumentsContent:
    def test_get_returns_text(self, planted_docs):
        client, _ = planted_docs
        r = client.get("/api/documents/Jane_CoverLetter_Acme.txt/content")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Jane_CoverLetter_Acme.txt"
        assert body["content"].startswith("Dear hiring team")
        assert body["editable"] is True

    def test_get_rejects_binary(self, planted_docs):
        client, _ = planted_docs
        r = client.get("/api/documents/Jane_Resume_Acme_Engineer.pdf/content")
        assert r.status_code == 400

    def test_post_persists_edit(self, planted_docs):
        client, out_dir = planted_docs
        new_text = "Updated cover letter body.\n"
        r = client.post(
            "/api/documents/Jane_CoverLetter_Acme.txt/content",
            json={"content": new_text},
        )
        assert r.status_code == 200, r.text
        # File on disk reflects the edit.
        assert (out_dir / "Jane_CoverLetter_Acme.txt").read_text(encoding="utf-8") == new_text

    def test_post_rejects_non_string_content(self, planted_docs):
        client, _ = planted_docs
        r = client.post(
            "/api/documents/Jane_CoverLetter_Acme.txt/content",
            json={"content": 12345},
        )
        assert r.status_code == 400


class TestDocumentsRename:
    def test_renames_file(self, planted_docs):
        client, out_dir = planted_docs
        r = client.post(
            "/api/documents/Jane_CoverLetter_Acme.txt/rename",
            json={"name": "Jane_CoverLetter_Acme_v2.txt"},
        )
        assert r.status_code == 200
        assert not (out_dir / "Jane_CoverLetter_Acme.txt").exists()
        assert (out_dir / "Jane_CoverLetter_Acme_v2.txt").exists()

    def test_refuses_collision(self, planted_docs):
        client, _ = planted_docs
        r = client.post(
            "/api/documents/Jane_CoverLetter_Acme.txt/rename",
            json={"name": "Jane_Resume_Acme_Engineer.tex"},
        )
        assert r.status_code == 409

    def test_refuses_traversal(self, planted_docs):
        client, _ = planted_docs
        r = client.post(
            "/api/documents/Jane_CoverLetter_Acme.txt/rename",
            json={"name": "../escape.txt"},
        )
        assert r.status_code == 400


class TestDocumentsDelete:
    def test_deletes_file(self, planted_docs):
        client, out_dir = planted_docs
        target = out_dir / "Jane_CoverLetter_Acme.txt"
        assert target.exists()
        r = client.delete("/api/documents/Jane_CoverLetter_Acme.txt")
        assert r.status_code == 200
        assert not target.exists()

    def test_refuses_unknown(self, planted_docs):
        client, _ = planted_docs
        r = client.delete("/api/documents/no_such_doc.tex")
        assert r.status_code == 404

    def test_refuses_traversal(self, planted_docs):
        client, _ = planted_docs
        r = client.delete("/api/documents/..%2Fescape.tex")
        # FastAPI normalizes the encoded path; the validator still rejects
        # path components and dot-dot regardless of how they got in.
        assert r.status_code in (400, 404)
