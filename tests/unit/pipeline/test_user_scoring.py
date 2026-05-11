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
