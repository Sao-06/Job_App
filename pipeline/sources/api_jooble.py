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

from .base import RawJob, is_remote_location, GENERAL_QUERIES, QueryRotator
from .registry import register
from ._http import http_post_json


class JoobleSource:
    name = "api:jooble"
    cadence_seconds = 30 * 60
    timeout_seconds = 15

    PAGE_SIZE = 20
    MAX_PAGES = 4
    QUERY_BATCH = 8

    # Jooble takes a free-text `location` filter. We rotate through every
    # major job market so the index spans Western Europe, Asia, LatAm,
    # Australasia, Middle East, and Africa instead of being US-pinned.
    # Same call budget as the old US-only path (8 queries × 4 pages per
    # cycle), now distributed across 16 regions in lockstep — full sweep
    # of every (query, location) pair lands within ~10 hours at the
    # 30-min cadence.
    LOCATIONS: tuple[str, ...] = (
        "United States", "United Kingdom", "Canada", "Australia",
        "Germany", "France", "Netherlands", "Spain", "Italy", "Sweden",
        "India", "Singapore", "Japan", "South Korea",
        "Brazil", "Mexico",
        "United Arab Emirates", "Israel", "South Africa", "Poland",
    )

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = f"https://jooble.org/api/{api_key}"
        self.rotator = QueryRotator(GENERAL_QUERIES, batch_size=self.QUERY_BATCH)
        self.location_rotator = QueryRotator(self.LOCATIONS, batch_size=self.QUERY_BATCH)

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        keywords_batch = self.rotator.next_batch()
        location_batch = self.location_rotator.next_batch()
        for keywords, location in zip(
            keywords_batch,
            location_batch or [self.LOCATIONS[0]] * len(keywords_batch),
        ):
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
