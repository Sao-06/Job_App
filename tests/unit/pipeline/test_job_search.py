"""Tests for pipeline.job_search — filtering, ranking, dedup, diversification, cursors."""
import sqlite3

import pytest
from freezegun import freeze_time

from pipeline import job_search
from pipeline.job_repo import init_schema, upsert_many
from pipeline.job_search import (
    SearchFilters,
    _decode_cursor,
    _encode_cursor,
    _freshness,
    _normalize_title,
    _skill_overlap,
    _title_match,
    _tokenize,
    newer_than,
    search,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def populated_conn(tmp_path):
    c = sqlite3.connect(tmp_path / "search.sqlite3")
    init_schema(c)
    rows = [
        {"application_url": "https://x.com/1", "company": "Acme",
         "title": "FPGA Intern", "location": "Boston, MA", "remote": False,
         "requirements": ["Verilog", "FPGA", "Python"], "salary_range": "$30/hr",
         "experience_level": "internship", "education_required": "bachelors",
         "citizenship_required": "no", "job_category": "engineering",
         "posted_at": "2026-04-15"},
        {"application_url": "https://x.com/2", "company": "Acme",
         "title": "Photonics Engineer", "location": "Remote", "remote": True,
         "requirements": ["Lumerical", "Python"], "salary_range": "$50/hr",
         "experience_level": "entry-level", "education_required": "bachelors",
         "citizenship_required": "no", "job_category": "engineering",
         "posted_at": "2026-04-20"},
        {"application_url": "https://x.com/3", "company": "BioCorp",
         "title": "Registered Nurse", "location": "San Francisco, CA", "remote": False,
         "requirements": ["RN", "BLS"], "salary_range": "$80k/yr",
         "experience_level": "mid-level", "education_required": "bachelors",
         "citizenship_required": "no", "job_category": "healthcare",
         "posted_at": "2026-04-01"},
        {"application_url": "https://x.com/4", "company": "DefenseCo",
         "title": "RF Engineer", "location": "DC", "remote": False,
         "requirements": ["RF", "matlab"], "salary_range": "$90k",
         "experience_level": "senior", "education_required": "bachelors",
         "citizenship_required": "yes", "job_category": "engineering",
         "posted_at": "2026-03-01"},
    ]
    upsert_many(c, rows)
    yield c
    c.close()


# ── Helpers ─────────────────────────────────────────────────────────────────


class TestTokenize:
    def test_lowercase_and_split(self):
        assert _tokenize("FPGA Intern, Boston") == ["fpga", "intern", "boston"]

    def test_keeps_special_chars_for_languages(self):
        toks = _tokenize("c++ python c#")
        assert "c++" in toks
        assert "python" in toks

    def test_filters_short_tokens(self):
        # Tokens < 2 chars dropped.
        assert "a" not in _tokenize("a big word")

    def test_handles_none(self):
        assert _tokenize(None) == []


class TestSkillOverlap:
    def test_zero_when_no_overlap(self):
        # 0 of 2 requirements match → 0.0.
        assert _skill_overlap(["python"], ["sql", "java"]) == 0.0

    def test_full_when_only_req_matches(self):
        # 1 of 1 requirements satisfied → 1.0.
        assert _skill_overlap(["python"], ["python developer"]) == 1.0

    def test_partial_match(self):
        # 1 of 2 requirements satisfied → 0.5.
        ov = _skill_overlap(["python"], ["python", "kubernetes"])
        assert ov == 0.5

    def test_neutral_default_when_either_empty(self):
        # Empty requirements or profile both return the 0.3 neutral floor.
        assert _skill_overlap([], ["python"]) == 0.3
        assert _skill_overlap(["python"], []) == 0.3


class TestTitleMatch:
    def test_full_match(self):
        assert _title_match(["FPGA Engineering Intern"], "FPGA Engineering Intern") == 1.0

    def test_partial_word_match(self):
        # words len > 2: "fpga", "engineering", "intern" — at least one in title.
        assert _title_match(["FPGA Engineering Intern"], "Embedded FPGA Designer") == 1.0

    def test_no_match(self):
        assert _title_match(["Photonics Intern"], "Sales Account Executive") == 0.0


class TestFreshness:
    @freeze_time("2026-05-05T12:00:00Z")
    def test_today_is_high(self):
        # ~0 days ago → close to 1.0
        assert _freshness("2026-05-05") > 0.9

    @freeze_time("2026-05-05T12:00:00Z")
    def test_30_days_old_decays_to_about_one_over_e(self):
        # exp(-1) ≈ 0.367
        score = _freshness("2026-04-05")
        assert 0.3 < score < 0.45

    @freeze_time("2026-05-05T12:00:00Z")
    def test_very_old_clamps_at_floor(self):
        # 100+ days → clamps at 0.05 floor.
        assert _freshness("2025-01-01") == pytest.approx(0.05, abs=0.005)

    def test_none_fallback(self):
        assert _freshness(None) == 0.3


# ── Cursor encoding ─────────────────────────────────────────────────────────


class TestCursor:
    def test_round_trip(self):
        c = _encode_cursor(0.75, "abc123", 2)
        result = _decode_cursor(c)
        assert result == (0.75, "abc123", 2)

    def test_decode_garbage_returns_none(self):
        assert _decode_cursor("not-base64!") is None

    def test_decode_empty_returns_none(self):
        assert _decode_cursor("") is None
        assert _decode_cursor(None) is None


# ── Title normalisation for dedup ───────────────────────────────────────────


class TestNormalizeTitle:
    def test_strips_year(self):
        assert "2026" not in _normalize_title("FPGA Intern (Summer 2026)")

    def test_strips_intern_internship(self):
        # Both "intern" and "internship" should be stripped.
        a = _normalize_title("FPGA Intern")
        b = _normalize_title("FPGA Internship")
        assert a == b

    def test_strips_remote_qualifier(self):
        a = _normalize_title("Software Engineer (Remote)")
        b = _normalize_title("Software Engineer (Hybrid)")
        # Both should drop their parenthetical.
        assert a.strip() == b.strip()

    def test_strips_punctuation(self):
        assert "engineer" in _normalize_title("Software Engineer!!!")


# ── search() end-to-end ─────────────────────────────────────────────────────


class TestSearch:
    def test_returns_all_rows_when_no_filters(self, populated_conn):
        page = search(conn=populated_conn, filters=SearchFilters(),
                      profile=None, cursor=None, limit=10)
        assert len(page.jobs) == 4
        assert page.total_estimate == 4

    def test_experience_level_filter(self, populated_conn):
        page = search(conn=populated_conn,
                      filters=SearchFilters(experience_levels=("internship",)),
                      profile=None, cursor=None, limit=10)
        assert len(page.jobs) == 1
        assert page.jobs[0].experience_level == "internship"

    def test_remote_only_filter(self, populated_conn):
        page = search(conn=populated_conn,
                      filters=SearchFilters(remote_only=True),
                      profile=None, cursor=None, limit=10)
        assert all(j.remote for j in page.jobs)

    def test_citizenship_exclude_required(self, populated_conn):
        page = search(conn=populated_conn,
                      filters=SearchFilters(citizenship_filter="exclude_required"),
                      profile=None, cursor=None, limit=10)
        assert all(j.citizenship_required != "yes" for j in page.jobs)

    def test_citizenship_only_required(self, populated_conn):
        page = search(conn=populated_conn,
                      filters=SearchFilters(citizenship_filter="only_required"),
                      profile=None, cursor=None, limit=10)
        assert all(j.citizenship_required == "yes" for j in page.jobs)

    def test_blacklist(self, populated_conn):
        page = search(conn=populated_conn,
                      filters=SearchFilters(blacklist=("acme",)),
                      profile=None, cursor=None, limit=10)
        assert all(j.company.lower() != "acme" for j in page.jobs)

    def test_whitelist_boosts_priority_companies(self, populated_conn):
        # Whitelist is a ranking boost (per spec), not a hard filter — priority
        # companies surface first but everyone else still appears. The
        # company round-robin layout interleaves, so the assertion is that
        # the FIRST result belongs to the boosted company.
        page = search(conn=populated_conn,
                      filters=SearchFilters(whitelist=("acme",)),
                      profile=None, cursor=None, limit=10)
        # Every row in the fixture is still returned (whitelist isn't a filter).
        assert len(page.jobs) == 4
        # The boosted company surfaces first.
        assert page.jobs[0].company.lower() == "acme"

    def test_location_substring_filter(self, populated_conn):
        page = search(conn=populated_conn,
                      filters=SearchFilters(location="boston"),
                      profile=None, cursor=None, limit=10)
        for j in page.jobs:
            assert "boston" in (j.location or "").lower()

    def test_profile_skews_ranking(self, populated_conn):
        # Profile heavy on Verilog should put the FPGA job at the top.
        profile = {"target_titles": ["FPGA Engineering Intern"],
                   "top_hard_skills": ["Verilog", "Python", "FPGA"]}
        page = search(conn=populated_conn, filters=SearchFilters(),
                      profile=profile, cursor=None, limit=10)
        # FPGA job should be in the top half.
        titles = [j.title for j in page.jobs]
        fpga_idx = next(i for i, t in enumerate(titles) if "FPGA" in t)
        assert fpga_idx < len(titles) // 2 + 1

    def test_cold_feed_diversifies_categories(self, populated_conn):
        # No profile + multiple categories → first page should not be all engineering.
        page = search(conn=populated_conn, filters=SearchFilters(),
                      profile=None, cursor=None, limit=10)
        cats = {j.job_category for j in page.jobs}
        assert "healthcare" in cats  # the lone healthcare row got surfaced

    def test_cursor_pagination(self, populated_conn):
        page1 = search(conn=populated_conn, filters=SearchFilters(),
                       profile=None, cursor=None, limit=2)
        assert len(page1.jobs) == 2
        if page1.next_cursor:
            page2 = search(conn=populated_conn, filters=SearchFilters(),
                           profile=None, cursor=page1.next_cursor, limit=2)
            page1_ids = {j.id for j in page1.jobs}
            page2_ids = {j.id for j in page2.jobs}
            assert page1_ids.isdisjoint(page2_ids)


# ── newer_than ──────────────────────────────────────────────────────────────


class TestNewerThan:
    def test_returns_empty_for_unknown_top(self, populated_conn):
        assert newer_than(conn=populated_conn, top_id="bogus") == []

    def test_returns_rows_strictly_newer(self, populated_conn):
        # Take any row's id, then advance the clock and add a new row with a
        # newer last_seen_at — newer_than should see the new one but not the old.
        with populated_conn:
            (top_id,) = populated_conn.execute(
                "SELECT id FROM job_postings ORDER BY last_seen_at LIMIT 1"
            ).fetchone()
        # Bump last_seen_at on a different row to make it strictly newer.
        with populated_conn:
            populated_conn.execute(
                "UPDATE job_postings SET last_seen_at = '2099-12-31T00:00:00Z' "
                "WHERE id != ?", (top_id,))
        results = newer_than(conn=populated_conn, top_id=top_id)
        assert len(results) >= 1
        for j in results:
            assert j.id != top_id
