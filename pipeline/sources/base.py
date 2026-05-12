"""
pipeline/sources/base.py
────────────────────────
Shared types + helpers for source providers.

Each provider implements :class:`JobSource`. ``fetch(since)`` returns an
iterator of :class:`RawJob` dicts; the ingestion worker normalizes,
infers metadata, and upserts.
"""

from __future__ import annotations

import re
from typing import Iterator, Protocol, TypedDict, runtime_checkable
from datetime import datetime
from urllib.parse import urlparse

from ..helpers import (
    infer_experience_level,
    infer_education_required,
    infer_citizenship_required,
    infer_job_category,
)
from ..job_repo import canonical_url as _canonical_url


# ── Public types ──────────────────────────────────────────────────────────────

class RawJob(TypedDict, total=False):
    application_url: str          # ← REQUIRED. Becomes canonical_url after normalize.
    company: str                  # ← REQUIRED.
    title: str                    # ← REQUIRED.
    location: str
    remote: bool
    description: str              # consumed for inference, then dropped
    requirements: list            # short list of bullet strings
    salary_range: str
    posted_date: str              # ISO date or YYYY-MM-DD; coerced downstream
    platform: str
    source: str                   # set by the source provider; "gh:simplify/new-grad", etc.


@runtime_checkable
class JobSource(Protocol):
    """Each provider exposes:

    * ``name``: stable id, e.g. "gh:simplify/new-grad" or "api:adzuna".
    * ``cadence_seconds``: how often the scheduler should call ``fetch``.
    * ``timeout_seconds``: optional per-call wall clock cap.
    * ``fetch(since)``: yields RawJob dicts.
    """

    name: str
    cadence_seconds: int
    timeout_seconds: int

    def fetch(self, since: datetime | None) -> Iterator[RawJob]: ...


# ── Helpers re-exported / convenience wrappers ────────────────────────────────

def canonical_url(url: str) -> str:
    return _canonical_url(url)


_NORM_RE = re.compile(r"[^\w\s]")


def normalize_company(name: str) -> str:
    """Lowercase + strip punctuation. Used for company-level dedup."""
    if not name:
        return ""
    return _NORM_RE.sub("", name.strip().lower())


def infer_metadata(job: dict) -> dict:
    """Run the existing inference helpers and return only the labels.

    Mutates a *copy* — leaves the caller's dict untouched.
    """
    out = dict(job)
    out["experience_level"]     = infer_experience_level(job)
    out["education_required"]   = infer_education_required(job)
    out["citizenship_required"] = infer_citizenship_required(job)
    out["job_category"]         = infer_job_category(job)
    return out


def is_remote_location(loc: str) -> bool:
    if not loc:
        return False
    l = loc.lower()
    return "remote" in l or "anywhere" in l or "worldwide" in l


# HTML stripping for source descriptions. Many provider APIs (Recruitee,
# JobSpy/Indeed, ATS rich-text fields, etc.) return descriptions as HTML
# fragments. We want plain text in the DB so skill_coverage matching and
# the SPA detail view both Just Work. Cheap fast-path: when no `<` appears
# in the body the regex never runs.

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_CLOSE_RE = re.compile(r"</(?:p|div|h[1-6]|tr|table|ul|ol|section|article)\s*>",
                              re.IGNORECASE)
_LI_OPEN_RE = re.compile(r"<li[^>]*>", re.IGNORECASE)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_RUN_OF_BLANKS_RE = re.compile(r"\n{3,}")


def strip_html(text: str) -> str:
    """Convert HTML to plain text, preserving newline structure.

    Block-level closes become newlines; ``<li>`` becomes a leading bullet so
    section-detection downstream still recognizes list items. Entities are
    decoded via stdlib ``html.unescape`` — covers every named + numeric
    entity (the prior hand-built dict missed ``&times;`` / ``&bull;`` / etc.
    and silently dropped numeric entities). Decode runs BEFORE tag stripping
    so double-encoded payloads (Greenhouse's ``&lt;div&gt;`` style) unescape
    to real tags first.
    """
    if not text:
        return ""
    if "<" not in text and "&" not in text:
        return text.strip()
    import html as _html_mod
    out = _html_mod.unescape(text)
    out = _BR_RE.sub("\n", out)
    out = _LI_OPEN_RE.sub("\n• ", out)
    out = _BLOCK_CLOSE_RE.sub("\n", out)
    out = _HTML_TAG_RE.sub("", out)
    out = _RUN_OF_BLANKS_RE.sub("\n\n", out)
    return out.strip()


def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


# ── Cross-industry query catalogue (used by keyed APIs) ──────────────────────
# Roughly 6 entries per major job family. Keyed sources iterate a *batch*
# of these per cycle (via :class:`QueryRotator`) so we cover the full set
# over a few cycles instead of burning quota on every cycle.

GENERAL_QUERIES: tuple[str, ...] = (
    # Engineering / tech
    "software engineer", "data scientist", "machine learning",
    "fpga", "hardware engineer", "qa engineer", "devops",
    # Finance / accounting
    "accountant", "financial analyst", "investment banking",
    "controller", "auditor",
    # Sales
    "account executive", "sales representative", "business development",
    "sales manager", "customer success",
    # Marketing
    "marketing manager", "brand manager", "social media",
    "content marketing", "growth marketing",
    # Product / project
    "product manager", "project manager", "program manager",
    # Design / creative
    "ux designer", "graphic designer", "art director", "copywriter",
    # Healthcare-adjacent
    "registered nurse", "medical assistant", "pharmacy technician",
    "clinical research",
    # Education
    "teacher", "professor", "curriculum",
    # HR / people ops
    "human resources", "recruiter", "talent acquisition",
    # Legal
    "paralegal", "legal counsel", "compliance",
    # Operations / supply / trades
    "operations manager", "logistics", "supply chain",
    "warehouse", "electrician", "mechanic",
    # Customer service
    "customer service", "support specialist",
    # Public sector
    "policy analyst", "public administration",
)


class QueryRotator:
    """Rotates through a long list of queries N at a time across calls.

    Lets a keyed source cover dozens of job families without burning
    its daily quota on every cycle.
    """

    def __init__(self, queries, *, batch_size: int):
        self.queries = list(queries) or [""]
        self.batch = max(1, int(batch_size))
        self._cursor = 0

    def next_batch(self) -> list[str]:
        n = len(self.queries)
        if n == 0:
            return []
        start = self._cursor % n
        end = start + self.batch
        if end <= n:
            batch = self.queries[start:end]
        else:
            batch = self.queries[start:] + self.queries[: end - n]
        self._cursor = (self._cursor + self.batch) % n
        return batch
