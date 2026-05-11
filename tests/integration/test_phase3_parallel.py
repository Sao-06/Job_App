"""Phase 3 LLM-rerank parallelism + failure isolation + provider parity."""
import time
import pytest
from unittest.mock import MagicMock
from pipeline.phases import phase3_score_jobs


def _make_jobs(n):
    return [
        {"id": f"j{i}", "title": "Engineer", "company": f"Co{i}",
         "description": "Python role with FastAPI", "requirements": ["Python"],
         "remote": False, "location": "Remote",
         "experience_level": "entry", "education_required": "bachelors"}
        for i in range(n)
    ]


def _profile():
    return {
        "target_titles": [{"title": "Engineer"}],
        "top_hard_skills": [{"skill": "Python"}],
        "experience_levels": ["entry"],
        "education_filter": "bachelors",
    }


# ── Parallelism timing test ──────────────────────────────────────────────────

def test_phase3_scores_in_parallel():
    """10 mock score_job calls @ 200ms each → wall < 1.2s with max_workers=5.
    Serial baseline would be 2s."""
    provider = MagicMock()
    def fake_score(job, profile):
        time.sleep(0.2)
        return {
            "job_id": job["id"], "score": 80,
            "score_breakdown": {
                "required_skills":    {"raw": 0.8, "weight": 50, "points": 40},
                "industry":           {"raw": 0.7, "weight": 30, "points": 21},
                "location_seniority": {"raw": 0.5, "weight": 20, "points": 10},
            },
            "reasoning": "ok", "matching_skills": ["Python"], "missing_skills": [],
        }
    provider.score_job.side_effect = fake_score

    jobs = _make_jobs(10)
    start = time.time()
    out = phase3_score_jobs(
        jobs=jobs, profile=_profile(), provider=provider,
        min_score=0, llm_score_limit=10, fast_only=False,
    )
    elapsed = time.time() - start
    # Serial would be ~2s; parallel with max_workers=5 should be ~0.4s.
    # Using 1.2s threshold to allow for slow CI.
    assert elapsed < 1.2, f"Phase 3 not parallel ({elapsed=:.2f}s)"
    assert len(out) >= 10
    # Every job in the top-N should have an LLM score
    for job in out[:10]:
        assert "score_data" in job or "score" in job


# ── Failure isolation ────────────────────────────────────────────────────────

def test_phase3_one_failure_isolated():
    """If 1 of 5 score_job calls raises, the other 4 succeed AND the failed
    one falls back to deterministic compute_skill_coverage."""
    provider = MagicMock()
    call_count = {"n": 0}
    failed_jobs = []
    def fake_score(job, profile):
        call_count["n"] += 1
        if call_count["n"] == 3:
            failed_jobs.append(job["id"])
            raise RuntimeError("simulated CLI failure")
        return {
            "job_id": job["id"], "score": 75,
            "score_breakdown": {
                "required_skills":    {"raw": 0.5, "weight": 50, "points": 25},
                "industry":           {"raw": 0.5, "weight": 30, "points": 15},
                "location_seniority": {"raw": 0.5, "weight": 20, "points": 10},
            },
            "reasoning": "ok", "matching_skills": [], "missing_skills": [],
        }
    provider.score_job.side_effect = fake_score

    jobs = _make_jobs(5)
    out = phase3_score_jobs(
        jobs=jobs, profile=_profile(), provider=provider,
        min_score=0, llm_score_limit=5, fast_only=False,
    )
    # All 5 jobs must still have a score field — failed one used fallback
    assert len(out) == 5
    for job in out:
        # Either an LLM score_data dict or a fallback numeric score
        assert "score" in job or "score_data" in job
    assert len(failed_jobs) == 1


# ── Provider parity — works for non-Mock providers too ───────────────────────

def test_phase3_parallel_with_demo_provider():
    """DemoProvider's score_job is pure-Python and thread-safe. Make sure
    Phase 3 parallelism doesn't break it."""
    from pipeline.providers import DemoProvider
    provider = DemoProvider()
    jobs = _make_jobs(5)
    out = phase3_score_jobs(
        jobs=jobs, profile=_profile(), provider=provider,
        min_score=0, llm_score_limit=5, fast_only=False,
    )
    assert len(out) == 5
    # DemoProvider returns deterministic scores — they should all be present
    for job in out:
        assert "score" in job or "score_data" in job


# ── fast_only flag still works (no LLM scoring) ──────────────────────────────

def test_phase3_fast_only_skips_parallelism():
    """fast_only=True should not invoke provider.score_job at all."""
    provider = MagicMock()
    provider.score_job.side_effect = AssertionError("score_job should not be called")
    jobs = _make_jobs(5)
    out = phase3_score_jobs(
        jobs=jobs, profile=_profile(), provider=provider,
        min_score=0, llm_score_limit=5, fast_only=True,
    )
    assert len(out) == 5
    provider.score_job.assert_not_called()
