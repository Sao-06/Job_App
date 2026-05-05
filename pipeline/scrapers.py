"""
pipeline/scrapers.py
────────────────────
Legacy compatibility shim.

The internet-scraping job-board layer (JobSpyClient, IndeedClient, the three
GitHub-README scrapers, and the small free-API wrappers) has been retired in
favor of the persistent local index in :mod:`pipeline.job_repo` populated by
:mod:`pipeline.ingest`. The ranked feed lives at ``GET /api/jobs/feed``.

Only the salary normalizers are kept because legacy callers (and the
new ingestion path) still want a single canonical formatter for the
``$lo–$hi/iv`` strings stored in ``job_postings.salary_range``.

Re-exports for back-compat:
  * normalize_salary
  * sanitize_salary_field

Anything else previously imported from this module (JobSpyClient,
SimplifyJobsScraper, JobrightScraper, InternListScraper,
HimalayasApiScraper, RemotiveApiScraper, ArbeitnowApiScraper, _make_job,
_api_job, _strip_html) has been removed. Callers that still need the
GitHub-README parser logic should use :mod:`pipeline.sources.github_readme`.
"""

from __future__ import annotations


# ── Salary helpers (still used by the ingestion layer + legacy callers) ──────

NAN_TOKENS = {"", "nan", "none", "null", "n/a", "0", "0.0", "-"}


def normalize_salary(raw_min, raw_max, interval: str = "yr") -> str:
    """Return ``$lo–$hi/iv`` or ``"Unknown"``. Never emits ``nan`` / ``$0``."""
    def _bad(v):
        return v is None or str(v).strip().lower() in NAN_TOKENS
    if _bad(raw_min) or _bad(raw_max):
        return "Unknown"
    try:
        lo, hi = float(raw_min), float(raw_max)
    except (TypeError, ValueError):
        return "Unknown"
    if lo <= 0 or hi <= 0 or hi < lo:
        return "Unknown"
    iv = (interval or "yr").strip().lower()
    if iv in NAN_TOKENS:
        iv = "yr"
    return f"${lo:,.0f}–${hi:,.0f}/{iv}"


def sanitize_salary_field(value) -> str:
    """Coerce any pre-formatted salary string to ``"Unknown"`` if it's junk."""
    if value is None:
        return "Unknown"
    s = str(value).strip()
    if not s or s.lower() in NAN_TOKENS or "nan" in s.lower() or "$0" in s:
        return "Unknown"
    return s


__all__ = ["normalize_salary", "sanitize_salary_field"]
