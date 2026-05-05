"""Tests for pipeline.job_repo — canonical URLs, upsert, soft-delete, FTS triggers."""
import json
import sqlite3

import pytest

from pipeline import job_repo
from pipeline.job_repo import (
    bulk_get_by_ids,
    canonical_url,
    get_job,
    init_schema,
    latest_source_runs,
    mark_missing,
    record_source_run,
    total_active,
    upsert_many,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "jobs.sqlite3")
    init_schema(c)
    yield c
    c.close()


# ── canonical_url ───────────────────────────────────────────────────────────


class TestCanonicalUrl:
    def test_strips_utm_params(self):
        url = "https://example.com/jobs/123?utm_source=google&utm_campaign=foo"
        assert canonical_url(url) == "https://example.com/jobs/123"

    def test_keeps_non_tracking_params(self):
        url = "https://example.com/jobs?id=42&loc=remote"
        assert "id=42" in canonical_url(url)
        assert "loc=remote" in canonical_url(url)

    def test_lowercases_host(self):
        assert canonical_url("https://Example.COM/path") == "https://example.com/path"

    def test_drops_fragment(self):
        assert canonical_url("https://example.com/x#section") == "https://example.com/x"

    def test_drops_trailing_slash(self):
        assert canonical_url("https://example.com/jobs/") == "https://example.com/jobs"

    def test_keeps_root_slash(self):
        assert canonical_url("https://example.com/") == "https://example.com/"

    def test_empty_input(self):
        assert canonical_url("") == ""
        assert canonical_url(None) == ""

    def test_adds_https_scheme(self):
        assert canonical_url("example.com/job/1").startswith("https://")

    def test_strips_gclid_and_ref(self):
        url = "https://example.com/job?gclid=abc&fbclid=def&ref=top"
        assert canonical_url(url) == "https://example.com/job"


# ── _job_id ─────────────────────────────────────────────────────────────────


class TestJobId:
    def test_deterministic(self):
        assert job_repo._job_id("https://x.com/1") == job_repo._job_id("https://x.com/1")

    def test_different_inputs_different_ids(self):
        assert job_repo._job_id("https://x.com/1") != job_repo._job_id("https://x.com/2")

    def test_length_16(self):
        assert len(job_repo._job_id("https://x.com/1")) == 16


# ── init_schema idempotence ────────────────────────────────────────────────


class TestInitSchema:
    def test_creates_tables(self, conn):
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' OR type='view'"
        )}
        assert "job_postings" in names
        assert "source_runs" in names

    def test_creates_fts(self, conn):
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        # FTS5 virtual table appears as a regular table.
        assert "job_postings_fts" in names

    def test_idempotent(self, conn):
        # Re-running init_schema must not error or duplicate rows.
        init_schema(conn)
        init_schema(conn)


# ── upsert_many ─────────────────────────────────────────────────────────────


def _row(**overrides):
    base = {
        "application_url": "https://example.com/jobs/1",
        "company": "Acme",
        "title": "Engineer",
        "location": "Remote",
        "remote": True,
        "requirements": ["Verilog"],
        "salary_range": "$30-$40/hr",
        "experience_level": "internship",
        "education_required": "bachelors",
        "citizenship_required": "no",
        "job_category": "engineering",
        "posted_at": "2026-04-15",
    }
    base.update(overrides)
    return base


class TestUpsertMany:
    def test_inserts_new_rows(self, conn):
        inserted, skipped = upsert_many(conn, [_row()])
        assert inserted == 1
        assert skipped == 0
        assert total_active(conn) == 1

    def test_skips_rows_without_url(self, conn):
        bad = _row(application_url="")
        inserted, skipped = upsert_many(conn, [bad])
        assert inserted == 0
        assert skipped == 1

    def test_skips_rows_without_company_or_title(self, conn):
        _, skipped1 = upsert_many(conn, [_row(company="")])
        _, skipped2 = upsert_many(conn, [_row(title="")])
        assert skipped1 == 1 and skipped2 == 1

    def test_updates_on_url_conflict(self, conn):
        upsert_many(conn, [_row(title="Old Title")])
        upsert_many(conn, [_row(title="New Title")])
        with conn:
            (title,) = conn.execute("SELECT title FROM job_postings").fetchone()
        assert title == "New Title"

    def test_preserves_existing_salary_when_update_is_unknown(self, conn):
        upsert_many(conn, [_row(salary_range="$50/hr")])
        upsert_many(conn, [_row(salary_range="Unknown")])
        with conn:
            (salary,) = conn.execute("SELECT salary_range FROM job_postings").fetchone()
        assert salary == "$50/hr"

    def test_requirements_stored_as_json(self, conn):
        upsert_many(conn, [_row(requirements=["A", "B", "C"])])
        with conn:
            (req_json,) = conn.execute("SELECT requirements_json FROM job_postings").fetchone()
        assert json.loads(req_json) == ["A", "B", "C"]

    def test_canonical_url_stripped_of_utm(self, conn):
        upsert_many(conn, [_row(application_url="https://ex.com/jobs/1?utm_source=foo")])
        with conn:
            (url,) = conn.execute("SELECT canonical_url FROM job_postings").fetchone()
        assert url == "https://ex.com/jobs/1"

    def test_remote_flag_coerced_to_int(self, conn):
        upsert_many(conn, [_row(remote=True)])
        with conn:
            (remote,) = conn.execute("SELECT remote FROM job_postings").fetchone()
        assert remote == 1


# ── mark_missing / soft delete ──────────────────────────────────────────────


class TestMarkMissing:
    def test_increments_miss_count_for_stale_rows(self, conn):
        upsert_many(conn, [_row(application_url="https://e.com/1")])
        # Nothing seen this run → all rows missed.
        missed, soft = mark_missing(conn, source="unknown",
                                     run_started_at="2099-01-01T00:00:00Z")
        assert missed == 1
        assert soft == 0
        with conn:
            (mc,) = conn.execute("SELECT miss_count FROM job_postings").fetchone()
        assert mc == 1

    def test_soft_deletes_after_three_strikes(self, conn):
        upsert_many(conn, [_row(application_url="https://e.com/1")])
        for _ in range(3):
            mark_missing(conn, source="unknown",
                         run_started_at="2099-01-01T00:00:00Z")
        with conn:
            (deleted,) = conn.execute("SELECT deleted FROM job_postings").fetchone()
        assert deleted == 1
        assert total_active(conn) == 0

    def test_resurrected_rows_reset_miss_count(self, conn):
        upsert_many(conn, [_row(application_url="https://e.com/1")])
        mark_missing(conn, source="unknown",
                     run_started_at="2099-01-01T00:00:00Z")
        # Re-upsert refreshes last_seen_at and zeroes miss_count.
        upsert_many(conn, [_row(application_url="https://e.com/1")])
        with conn:
            (mc,) = conn.execute("SELECT miss_count FROM job_postings").fetchone()
        assert mc == 0


# ── source_runs telemetry ───────────────────────────────────────────────────


class TestSourceRuns:
    def test_record_and_latest(self, conn):
        record_source_run(conn, source="api:foo",
                          started_at="2026-05-01T00:00:00Z",
                          finished_at="2026-05-01T00:01:00Z",
                          ok=True, fetched=10, inserted=8)
        record_source_run(conn, source="api:foo",
                          started_at="2026-05-01T01:00:00Z",
                          finished_at="2026-05-01T01:00:30Z",
                          ok=False, fetched=0, error="boom")
        rows = latest_source_runs(conn)
        assert len(rows) == 1
        # Most-recent run wins (the failing one).
        assert rows[0]["ok"] is False
        assert rows[0]["error"] == "boom"


# ── get_job / bulk_get_by_ids ───────────────────────────────────────────────


class TestRead:
    def test_get_job_round_trip(self, conn):
        upsert_many(conn, [_row(application_url="https://e.com/1", title="Engineer X")])
        with conn:
            (jid,) = conn.execute("SELECT id FROM job_postings").fetchone()
        rec = get_job(conn, jid)
        assert rec is not None
        assert rec["title"] == "Engineer X"
        assert rec["requirements"] == ["Verilog"]

    def test_get_job_returns_none_for_missing(self, conn):
        assert get_job(conn, "no-such-id") is None
        assert get_job(conn, "") is None

    def test_bulk_get_by_ids_skips_deleted(self, conn):
        upsert_many(conn, [_row(application_url="https://e.com/1"),
                            _row(application_url="https://e.com/2")])
        with conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM job_postings")]
        # Soft-delete one
        for _ in range(3):
            mark_missing(conn, source="unknown", run_started_at="2099-01-01T00:00:00Z")
        result = bulk_get_by_ids(conn, ids)
        # 0 active after both got soft-deleted.
        assert result == []
