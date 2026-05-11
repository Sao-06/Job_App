"""Tests for the persistent per-(user, job) scorer."""
from __future__ import annotations

import json
import sqlite3

import pytest

from pipeline import job_repo, user_scoring


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    job_repo.init_schema(c)
    job_repo.upsert_many(c, [
        {"url": "https://x.example/swe", "source": "test", "company": "Acme",
         "title": "Software Engineer", "location": "Remote", "remote": True,
         "requirements": ["Python", "SQL"], "experience_level": "mid-level",
         "description": "Build APIs in Python."},
        {"url": "https://x.example/mkt", "source": "test", "company": "Beta",
         "title": "Marketing Manager", "location": "NYC", "remote": False,
         "requirements": ["SEO", "Copywriting"], "experience_level": "senior",
         "description": "Lead marketing."},
        {"url": "https://x.example/fpga", "source": "test", "company": "Gamma",
         "title": "FPGA Verification Engineer", "location": "Austin, TX",
         "remote": False, "requirements": ["Verilog", "FPGA"],
         "experience_level": "entry-level",
         "description": "Verify FPGA designs."},
    ])
    return c


def test_engineering_resume_scores_highest_against_engineering_role(conn):
    profile = {
        "top_hard_skills": ["Python", "Verilog", "FPGA", "SQL"],
        "target_titles": ["Software Engineer", "FPGA Engineer"],
        "location": "San Francisco, CA",
        "experience_levels": ["mid-level"],
    }
    summary = user_scoring.score_jobs_for_user(conn, "user1", profile)
    assert summary["scored"] == 3
    rows = {r["title"]: r["score"] for r in (
        dict(zip(("title", "score"), row))
        for row in conn.execute(
            "SELECT jp.title, ujs.score FROM user_job_scores ujs "
            "JOIN job_postings jp ON jp.id = ujs.job_id "
            "WHERE ujs.user_id = 'user1'"
        ).fetchall()
    )}
    # Engineering roles should outscore marketing.
    assert rows["Software Engineer"] > rows["Marketing Manager"]
    assert rows["FPGA Verification Engineer"] > rows["Marketing Manager"]


def test_only_new_skips_already_scored_rows(conn):
    profile = {"top_hard_skills": ["Python"], "target_titles": ["Software Engineer"]}
    user_scoring.score_jobs_for_user(conn, "u2", profile)
    assert conn.execute("SELECT COUNT(*) FROM user_job_scores").fetchone()[0] == 3
    # Insert a fresh row that has no score yet.
    job_repo.upsert_many(conn, [
        {"url": "https://x.example/new", "source": "test", "company": "Delta",
         "title": "New Role", "location": "Remote", "remote": True,
         "requirements": ["Python"], "description": "."},
    ])
    summary = user_scoring.score_new_jobs_for_user(conn, "u2", profile)
    assert summary["scored"] == 1
    assert summary["only_new"] is True
    assert conn.execute("SELECT COUNT(*) FROM user_job_scores").fetchone()[0] == 4


def test_profile_signature_is_stable_for_irrelevant_field_changes():
    base = {
        "top_hard_skills": ["python", "sql"],
        "target_titles": ["Software Engineer"],
        "location": "SF",
        "experience_levels": ["mid-level"],
    }
    a = user_scoring.profile_signature(base)
    base2 = dict(base, name="Renamed Person")
    b = user_scoring.profile_signature(base2)
    assert a == b
    # Changing a load-bearing field DOES change the signature.
    base3 = dict(base, target_titles=["Marketing"])
    assert user_scoring.profile_signature(base3) != a


def test_delete_user_scores_removes_only_caller(conn):
    profile = {"top_hard_skills": ["Python"], "target_titles": ["SWE"]}
    user_scoring.score_jobs_for_user(conn, "u3", profile)
    user_scoring.score_jobs_for_user(conn, "u4", profile)
    assert conn.execute(
        "SELECT COUNT(*) FROM user_job_scores WHERE user_id IN ('u3','u4')"
    ).fetchone()[0] == 6
    deleted = user_scoring.delete_user_scores(conn, "u3")
    assert deleted == 3
    remaining_users = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM user_job_scores"
        ).fetchall()
    }
    assert remaining_users == {"u4"}


def test_known_user_ids_with_scores_orders_by_most_recent(conn):
    user_scoring.score_jobs_for_user(
        conn, "alice", {"top_hard_skills": ["x", "y"], "target_titles": ["t"]},
    )
    user_scoring.score_jobs_for_user(
        conn, "bob", {"top_hard_skills": ["x", "y"], "target_titles": ["t"]},
    )
    ids = user_scoring.known_user_ids_with_scores(conn)
    assert set(ids) == {"alice", "bob"}


def test_template_profile_yields_low_job_scores(conn):
    """A template resume — placeholder skills, one stub work entry, no
    bullets — should produce confidently-low scores on every job. The
    `profile_strength` multiplier in `score_jobs_for_user` is what
    prevents the previous "blank template reads 40% match" failure mode.
    """
    template = {
        "top_hard_skills": ["Python", "JavaScript", "React"],  # placeholder
        "target_titles": ["Software Engineer"],                 # placeholder
        "work_experience": [
            {"title": "Software Engineer", "company": "Company Name", "bullets": []},
        ],
        "experience_levels": ["entry-level"],
    }
    user_scoring.score_jobs_for_user(conn, "template_user", template)
    rows = conn.execute(
        "SELECT jp.title, ujs.score FROM user_job_scores ujs "
        "JOIN job_postings jp ON jp.id = ujs.job_id "
        "WHERE ujs.user_id = 'template_user'"
    ).fetchall()
    # Every score must be well below "confidently strong applicant" territory.
    # Pre-fix the SWE row read ~40 (title placeholder hit 1.0 + loc 0.5 default).
    for title, score in rows:
        assert score <= 20, f"Template scored {score} on '{title}' — multiplier failed"


def test_strong_profile_scores_higher_than_template_on_same_job(conn):
    """Two users, same job, very different resume strength → very
    different scores. Sanity-check that the strength multiplier doesn't
    flatten real resumes."""
    template = {
        "top_hard_skills": ["Python"],
        "target_titles": ["Software Engineer"],
        "work_experience": [{"title": "SWE", "company": "Co", "bullets": []}],
    }
    strong = {
        "top_hard_skills": ["Python", "SQL", "Django", "AWS", "Docker",
                            "Postgres", "Redis", "Go", "Terraform", "Kubernetes"],
        "target_titles": ["Backend Engineer", "Software Engineer", "Platform Engineer"],
        "work_experience": [
            {"title": "Senior Engineer", "company": "X", "bullets": [
                "Shipped API serving 5M req/day",
                "Cut p99 latency 60%",
                "Led 3-engineer team",
            ]},
            {"title": "Engineer", "company": "Y", "bullets": [
                "Built billing pipeline processing $2M/month",
                "Migrated DB to Postgres 14",
                "On-call rotation",
            ]},
            {"title": "Junior Engineer", "company": "Z", "bullets": [
                "Owned admin dashboard",
                "Wrote 80% test coverage",
                "Designed event bus",
            ]},
        ],
    }
    user_scoring.score_jobs_for_user(conn, "tmpl", template)
    user_scoring.score_jobs_for_user(conn, "strong", strong)
    # Compare on the SWE row (same job, both should match the role).
    tmpl_score = conn.execute(
        "SELECT ujs.score FROM user_job_scores ujs JOIN job_postings jp ON jp.id = ujs.job_id "
        "WHERE ujs.user_id = 'tmpl' AND jp.title = 'Software Engineer'"
    ).fetchone()[0]
    strong_score = conn.execute(
        "SELECT ujs.score FROM user_job_scores ujs JOIN job_postings jp ON jp.id = ujs.job_id "
        "WHERE ujs.user_id = 'strong' AND jp.title = 'Software Engineer'"
    ).fetchone()[0]
    assert strong_score > tmpl_score + 25, (
        f"Strong ({strong_score}) should be well above template ({tmpl_score})"
    )
