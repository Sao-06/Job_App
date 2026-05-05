"""Tests for pipeline.sources.base — protocols, helpers, query rotator."""
import pytest

from pipeline.sources.base import (
    GENERAL_QUERIES,
    JobSource,
    QueryRotator,
    canonical_url,
    host_of,
    infer_metadata,
    is_remote_location,
    normalize_company,
)
import sys
import pipeline.sources.registry  # ensures module loaded
src_registry = sys.modules["pipeline.sources.registry"]
from pipeline.sources.registry import register as _register, get as _get

pytestmark = pytest.mark.unit


# ── Protocol contract ───────────────────────────────────────────────────────


class TestJobSourceProtocol:
    def test_fake_satisfies_protocol(self, fake_source):
        s = fake_source(name="fake:test", jobs=[])
        # runtime_checkable Protocol — isinstance works.
        assert isinstance(s, JobSource)

    def test_required_attributes(self, fake_source):
        s = fake_source(name="x", jobs=[])
        assert hasattr(s, "name")
        assert hasattr(s, "cadence_seconds")
        assert hasattr(s, "timeout_seconds")
        assert callable(s.fetch)


# ── Helpers re-exported from base ───────────────────────────────────────────


class TestCanonicalUrl:
    def test_re_export_works(self):
        # Make sure base.canonical_url is the same as job_repo.canonical_url.
        assert canonical_url("https://x.com/?utm_source=foo") == "https://x.com/"


class TestHostOf:
    def test_basic(self):
        assert host_of("https://www.example.com/jobs") == "www.example.com"

    def test_handles_garbage(self):
        # urlparse is lenient — we accept any return so long as it's a string.
        assert isinstance(host_of("not a url"), str)


class TestNormalizeCompany:
    def test_lowercases_and_strips_punct(self):
        assert normalize_company("Acme, Inc.") == "acme inc"

    def test_handles_none(self):
        assert normalize_company(None) == ""

    def test_handles_empty(self):
        assert normalize_company("") == ""


class TestIsRemoteLocation:
    @pytest.mark.parametrize("loc", ["Remote", "remote — US", "anywhere", "Worldwide"])
    def test_remote_variants(self, loc):
        assert is_remote_location(loc) is True

    @pytest.mark.parametrize("loc", ["Boston, MA", "San Francisco", "London"])
    def test_non_remote(self, loc):
        assert is_remote_location(loc) is False

    def test_handles_none(self):
        assert is_remote_location(None) is False


# ── infer_metadata ──────────────────────────────────────────────────────────


class TestInferMetadata:
    def test_attaches_inference_labels(self):
        job = {"title": "FPGA Engineering Intern",
               "description": "Verify FPGA blocks",
               "requirements": []}
        out = infer_metadata(job)
        assert out["experience_level"] == "internship"
        assert out["citizenship_required"] in ("yes", "no", "unknown")
        assert out["education_required"] in (
            "phd", "masters", "bachelors", "associates", "high_school", "unknown",
        )
        assert "job_category" in out

    def test_does_not_mutate_caller_dict(self):
        job = {"title": "Engineer", "description": "", "requirements": []}
        original_keys = set(job.keys())
        infer_metadata(job)
        # Caller's dict untouched.
        assert set(job.keys()) == original_keys


# ── QueryRotator ────────────────────────────────────────────────────────────


class TestQueryRotator:
    def test_returns_first_batch(self):
        r = QueryRotator(["a", "b", "c", "d"], batch_size=2)
        assert r.next_batch() == ["a", "b"]

    def test_advances(self):
        r = QueryRotator(["a", "b", "c", "d"], batch_size=2)
        r.next_batch()
        assert r.next_batch() == ["c", "d"]

    def test_wraps_around(self):
        r = QueryRotator(["a", "b", "c"], batch_size=2)
        r.next_batch()                  # ["a","b"]
        wrap = r.next_batch()           # wraps to ["c","a"]
        assert wrap == ["c", "a"]

    def test_empty_queries_inserts_sentinel(self):
        # Constructor coerces empty input to a [""] sentinel so callers
        # always get something back. The actual batch length depends on
        # batch_size and wrap-around, but the values are all empty strings.
        r = QueryRotator([], batch_size=3)
        out = r.next_batch()
        assert all(s == "" for s in out)

    def test_batch_size_floor(self):
        r = QueryRotator(["a", "b"], batch_size=0)
        # Constructor floors batch_size at 1.
        assert r.batch == 1

    def test_general_queries_populated(self):
        # Sanity check: the cross-industry list has multi-domain coverage.
        assert any("software engineer" in q for q in GENERAL_QUERIES)
        assert any("nurse" in q.lower() for q in GENERAL_QUERIES)
        assert any("teacher" in q for q in GENERAL_QUERIES)


# ── Registry ────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_register_and_get(self, fake_source):
        s = fake_source(name="fake:reg-test", jobs=[])
        _register(s)
        assert _get("fake:reg-test") is s

    def test_registry_returns_sorted(self, fake_source):
        s_b = fake_source(name="b", jobs=[])
        s_a = fake_source(name="a", jobs=[])
        _register(s_b)
        _register(s_a)
        names = [src.name for src in src_registry.registry()]
        # Sorted by name.
        assert names == sorted(names)

    def test_get_returns_none_for_missing(self, fake_source):
        # fake_source fixture clears the registry first.
        assert _get("does-not-exist") is None
