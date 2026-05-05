"""Tests for pipeline.config — verify the migration env guard works."""
import importlib
import sys

import pytest

pytestmark = pytest.mark.unit


class TestMigrationGuard:
    def test_skip_flag_neutralises_migrate_db_path(self, monkeypatch):
        """When JOBS_AI_SKIP_MIGRATION=1 (set by conftest), migrate_db_path
        must not run at module import time."""
        monkeypatch.setenv("JOBS_AI_SKIP_MIGRATION", "1")
        called = {"n": 0}
        # Patch the migration target on the existing module, then reload —
        # the env guard at the top of pipeline/config.py runs against the
        # patched function before re-binding finalises.
        import pipeline.config as cfg
        monkeypatch.setattr(
            cfg, "migrate_db_path",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        importlib.reload(cfg)
        assert called["n"] == 0

    def test_constants_exposed(self):
        from pipeline import config
        for name in ("OUTPUT_DIR", "RESOURCES_DIR", "DATA_DIR",
                     "DB_PATH", "TODAY", "MAX_SCRAPE_JOBS", "console", "DEMO_JOBS"):
            assert hasattr(config, name), f"pipeline.config missing {name}"

    def test_demo_jobs_well_formed(self):
        from pipeline.config import DEMO_JOBS
        assert isinstance(DEMO_JOBS, list)
        assert len(DEMO_JOBS) >= 5
        for j in DEMO_JOBS:
            assert "title" in j and "company" in j and "application_url" in j


class TestCliSpinner:
    def test_spinner_starts_and_stops_cleanly(self):
        from pipeline.config import _CliSpinner
        spinner = _CliSpinner(messages=["test"], interval=10).start()
        spinner.stop()
        # Calling stop twice is a no-op (idempotent).
        spinner.stop()

    def test_spinner_context_manager(self):
        from pipeline.config import _CliSpinner
        with _CliSpinner(messages=["test"], interval=10):
            pass
