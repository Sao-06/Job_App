"""
The Muse — public API, no key required.
Docs: https://www.themuse.com/developers/api/v2

Returns ~10k US-relevant jobs paginated 20 per page across categories.
We walk pages until exhaustion (or the safety cap) and yield each
posting as a RawJob.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from .base import RawJob, is_remote_location
from .registry import register
from ._http import http_get_json


_BASE = "https://www.themuse.com/api/public/jobs"


class TheMuseSource:
    name = "api:themuse"
    cadence_seconds = 30 * 60
    timeout_seconds = 12

    # The Muse's taxonomy — broadened to cover non-tech families.
    CATEGORIES = (
        "Engineering", "Software Engineer", "Data Science",
        "Design", "Product", "Marketing", "Sales",
        "HR", "Finance", "Legal",
        "Customer Service", "Operations", "Education",
        "Healthcare", "Account Management", "Project Management",
    )
    MAX_PAGES = 8

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        for category in self.CATEGORIES:
            for page in range(self.MAX_PAGES):
                data = http_get_json(_BASE, params={
                    "category": category, "page": page, "descending": "true",
                }, timeout=self.timeout_seconds)
                if not data or not isinstance(data, dict):
                    break
                results = data.get("results") or []
                if not results:
                    break
                for r in results:
                    url = (r.get("refs") or {}).get("landing_page") or ""
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    locations = [l.get("name") for l in (r.get("locations") or []) if l]
                    location = ", ".join([str(l) for l in locations if l]) or ""
                    company = ((r.get("company") or {}).get("name")) or ""
                    title = r.get("name") or ""
                    if not (company and title):
                        continue
                    desc = r.get("contents") or ""
                    yield RawJob(
                        application_url=url,
                        company=company,
                        title=title,
                        location=location,
                        remote=is_remote_location(location),
                        description=desc,
                        posted_date=(r.get("publication_date") or "")[:10],
                        platform="The Muse",
                        source=self.name,
                    )
                if data.get("page_count") is not None and page + 1 >= data["page_count"]:
                    break


register(TheMuseSource())
