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
    return out


def is_remote_location(loc: str) -> bool:
    if not loc:
        return False
    l = loc.lower()
    return "remote" in l or "anywhere" in l or "worldwide" in l


def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""
