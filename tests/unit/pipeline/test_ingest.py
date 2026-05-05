"""Tests for pipeline.ingest — single-source run, force_run, lock isolation."""
import sqlite3
import threading

import pytest

from pipeline import ingest as ingest_module
from pipeline import job_repo
from pipeline.ingest import force_run, run_one
import sys
import pipeline.sources.registry  # ensures the module is loaded
src_registry = sys.modules["pipeline.sources.registry"]
from pipeline.sources.registry import register as _register
from tests.fakes import FakeJobSource, make_raw_job

pytestmark = pytest.mark.unit


@pytest.fixture
def wired_db(tmp_path, monkeypatch):
    """Build a DB with the jobs schema and wire ingest._connect to it.

    Tests must reset the registry separately via the `fake_source` fixture
    so the parallel backfill doesn't pick up live providers.
    """
    db_path = tmp_path / "ingest.sqlite3"

    def _connect():
        c = sqlite3.connect(db_path)
        return c

    # Initialize schema once.
    init_conn = _connect()
    job_repo.init_schema(init_conn)
    init_conn.close()

    monkeypatch.setattr(ingest_module, "_connect", _connect)
    yield _connect


# ── run_one ─────────────────────────────────────────────────────────────────


class TestRunOne:
    def test_inserts_rows_and_records_run(self, wired_db, fake_source, monkeypatch):
        # Reset locks between tests.
        monkeypatch.setattr(ingest_module, "_locks", {})
        rows = [
            make_raw_job(company="Acme", title="FPGA Intern"),
            make_raw_job(company="Bravo", title="Photonics"),
        ]
        src = fake_source(name="fake:two-rows", jobs=rows)
        result = run_one(src)
        assert result["ok"] is True
        assert result["fetched"] == 2
        assert result["inserted"] == 2

        # The DB now has both rows.
        with wired_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM job_postings WHERE deleted = 0"
            ).fetchone()[0]
        assert count == 2

        # source_runs got a row recorded.
        with wired_db() as conn:
            telem = job_repo.latest_source_runs(conn)
        assert any(r["source"] == "fake:two-rows" and r["ok"] for r in telem)

    def test_records_failure_on_provider_error(self, wired_db, fake_source, monkeypatch):
        monkeypatch.setattr(ingest_module, "_locks", {})

        class BrokenSource(FakeJobSource):
            def fetch(self, since):
                raise RuntimeError("upstream API exploded")

        src = BrokenSource(name="fake:broken", jobs=[])
        result = run_one(src)
        assert result["ok"] is False
        assert "upstream API exploded" in (result["error"] or "")

        with wired_db() as conn:
            telem = job_repo.latest_source_runs(conn)
        broken = next(r for r in telem if r["source"] == "fake:broken")
        assert broken["ok"] is False
        assert "exploded" in (broken["error"] or "")

    def test_lock_prevents_concurrent_runs_for_same_source(
        self, wired_db, fake_source, monkeypatch
    ):
        monkeypatch.setattr(ingest_module, "_locks", {})
        # Pre-acquire the lock for this source.
        lock = threading.Lock()
        lock.acquire()
        ingest_module._locks["fake:locked"] = lock

        src = fake_source(name="fake:locked", jobs=[])
        result = run_one(src)
        assert result.get("skipped") is True


# ── force_run ───────────────────────────────────────────────────────────────


class TestForceRun:
    def test_run_all_registered(self, wired_db, fake_source, monkeypatch):
        monkeypatch.setattr(ingest_module, "_locks", {})
        # Register two fakes.
        s1 = fake_source(name="fake:one", jobs=[
            make_raw_job(company="One", title="Engineer", url="https://one/1")
        ])
        s2 = fake_source(name="fake:two", jobs=[
            make_raw_job(company="Two", title="Engineer", url="https://two/1"),
            make_raw_job(company="Two", title="Other",    url="https://two/2"),
        ])
        _register(s1)
        _register(s2)

        results = force_run()
        names = {r["source"] for r in results}
        assert names == {"fake:one", "fake:two"}
        # All ok.
        assert all(r.get("ok") for r in results)

    def test_run_one_by_name(self, wired_db, fake_source, monkeypatch):
        monkeypatch.setattr(ingest_module, "_locks", {})
        s1 = fake_source(name="fake:alpha", jobs=[
            make_raw_job(company="A", title="X", url="https://a/1")
        ])
        s2 = fake_source(name="fake:beta", jobs=[
            make_raw_job(company="B", title="Y", url="https://b/1")
        ])
        _register(s1)
        _register(s2)

        results = force_run("fake:alpha")
        assert len(results) == 1
        assert results[0]["source"] == "fake:alpha"

    def test_run_unknown_source_returns_empty(self, wired_db, fake_source, monkeypatch):
        monkeypatch.setattr(ingest_module, "_locks", {})
        # No registered sources matching name → empty list.
        assert force_run("nothing-here") == []
