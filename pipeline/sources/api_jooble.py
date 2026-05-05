"""
Jooble — global meta-search keyed API.

Requires:
  JOOBLE_API_KEY   UUID-style string from https://jooble.org/api/about

Endpoint: POST https://jooble.org/api/{key}
Body: ``{"keywords": str, "location": str, "page": str, "ResultOnPage": str}``

Jooble aggregates from many job boards (Indeed mirrors etc.), so it's
a high-volume add. ResultOnPage maxes at 20; we walk pages 1..MAX_PAGES.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Iterator

from .base import RawJob, is_remote_location
from .registry import register
from ._http import http_post_json


class JoobleSource:
    name = "api:jooble"
    cadence_seconds = 30 * 60
    timeout_seconds = 15

    PAGE_SIZE = 20
    MAX_PAGES = 8                   # 8 × 20 × len(QUERIES) ≈ 1600 jobs/cycle

    QUERIES = (
        ("engineer", "United States"),
        ("software engineer", "United States"),
        ("hardware engineer", "United States"),
        ("data scientist", "United States"),
        ("machine learning", "United States"),
        ("fpga", "United States"),
    )

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = f"https://jooble.org/api/{api_key}"

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        for keywords, location in self.QUERIES:
            for page in range(1, self.MAX_PAGES + 1):
                data = http_post_json(self.url, {
                    "keywords": keywords,
                    "location": location,
                    "page": str(page),
                    "ResultOnPage": str(self.PAGE_SIZE),
                }, timeout=self.timeout_seconds)
                jobs = (data or {}).get("jobs") or []
                if not jobs:
                    break
                for r in jobs:
                    if not isinstance(r, dict):
                        continue
                    apply_url = r.get("link") or ""
                    if not apply_url or apply_url in seen:
                        continue
                    seen.add(apply_url)
                    title = r.get("title") or ""
                    company = r.get("company") or ""
                    if not (company and title):
                        # Jooble sometimes blanks the company; skip the row
                        # instead of polluting dedup with anonymous entries.
                        continue
                    loc = r.get("location") or location
                    yield RawJob(
                        application_url=apply_url,
                        company=company,
                        title=title,
                        location=loc,
                        remote=is_remote_location(loc),
                        description=r.get("snippet") or "",
                        salary_range=r.get("salary") or "Unknown",
                        posted_date=str(r.get("updated") or "")[:10],
                        platform=r.get("source") or "Jooble",
                        source=self.name,
                    )
                if len(jobs) < self.PAGE_SIZE:
                    break


_key = os.environ.get("JOOBLE_API_KEY")
if _key:
    register(JoobleSource(_key))
