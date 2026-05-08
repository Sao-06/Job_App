"""Integration tests for the v2 tailoring endpoints.

Covers:
  • POST /api/resume/tailor/analyze   — heuristic keyword classification
  • POST /api/resume/tailor extended  — accepts selected_keywords, emits v2
  • Resume upload persists source_format on the resume record
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_job(seed_jobs):
    """Seed one job with deterministic requirements that overlap and miss
    against the FakeProvider's default profile (Verilog/SPICE/Cadence/...).
    Verilog is on the resume; UVM and AXI4 are not. Returns the id."""
    ids = seed_jobs(
        count=1, company="FakeCo", title="FPGA Intern",
        url="https://boards.greenhouse.io/fakeco/jobs/777",
        requirements=["Verilog", "FPGA verification", "AXI4", "UVM"],
        description="Design and verify FPGA blocks.",
    )
    return ids[0]


def test_analyze_returns_classified_keywords(fastapi_client, patched_provider, seed_resume, seeded_job):
    seed_resume()
    client, _, _ = fastapi_client
    resp = client.post("/api/resume/tailor/analyze", json={"job_id": seeded_job})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data["must_have"], list)
    assert isinstance(data["nice_to_have"], list)
    assert "ats_score_current" in data
    assert "estimated_after" in data
    assert len(data["must_have"]) + len(data["nice_to_have"]) == 4


def test_analyze_marks_present_keywords(fastapi_client, patched_provider, seed_resume, seeded_job):
    seed_resume()
    client, _, _ = fastapi_client
    resp = client.post("/api/resume/tailor/analyze", json={"job_id": seeded_job})
    assert resp.status_code == 200
    data = resp.json()
    by_kw = {c["keyword"]: c for c in (data["must_have"] + data["nice_to_have"])}
    # FakeProvider profile has Verilog as a top hard skill
    assert by_kw["Verilog"]["present"] is True
    assert by_kw["UVM"]["present"] is False


def test_tailor_accepts_selected_keywords(fastapi_client, patched_provider, seed_resume, seeded_job):
    seed_resume()
    client, _, _ = fastapi_client
    resp = client.post(
        "/api/resume/tailor",
        json={"job_id": seeded_job, "selected_keywords": ["AXI4"]},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["item"]
    assert item["co"] == "FakeCo"
    assert item.get("schema_version") == 2
    skills_flat = [
        (it.get("text"), it.get("diff"))
        for cat in item["v2"]["skills"]
        for it in cat.get("items") or []
    ]
    assert ("AXI4", "added") in skills_flat


def test_tailor_default_no_selection_still_succeeds(fastapi_client, patched_provider,
                                                    seed_resume, seeded_job):
    """Without selected_keywords, the endpoint still produces a valid v2 item."""
    seed_resume()
    client, _, _ = fastapi_client
    resp = client.post("/api/resume/tailor", json={"job_id": seeded_job})
    assert resp.status_code == 200
    item = resp.json()["item"]
    assert item["schema_version"] == 2
    # Heuristic surfaces every missing JD keyword in ats_keywords_missing
    gaps_lower = {g.lower() for g in item.get("ats_gaps") or []}
    assert "uvm" in gaps_lower or "axi4" in gaps_lower


def test_resume_upload_persists_source_format(fastapi_client, patched_provider, wait_extraction):
    """Upload a .tex resume; source_format should land on the record."""
    client, _, _ = fastapi_client
    files = {
        "file": (
            "resume.tex",
            b"\\documentclass{article}\n\\begin{document}Test\\end{document}",
            "application/x-tex",
        ),
    }
    resp = client.post("/api/resume/upload", files=files)
    assert resp.status_code == 200, resp.text
    state = wait_extraction(client)
    primary = next((r for r in state.get("resumes") or [] if r.get("primary")), None)
    assert primary is not None
    assert primary.get("source_format") == "tex"
