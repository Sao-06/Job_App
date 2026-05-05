"""
Remotive — public job-search API, no key required.
Docs: https://remotive.com/api/remote-jobs

Returns a flat list with no pagination; a single call yields the full
active corpus (a few thousand jobs across all categories).
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from .base import RawJob
from .registry import register
from ._http import http_get_json


class RemotiveSource:
    name = "api:remotive"
    cadence_seconds = 25 * 60
    timeout_seconds = 15

    # Remotive's category slugs — see https://remotive.com/api/remote-jobs?category=
    CATEGORIES = (
        "software-dev", "data", "devops", "design", "qa",
        "product", "marketing", "sales", "business",
        "customer-support", "writing", "finance-legal",
        "human-resources", "all-others",
    )

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        for cat in self.CATEGORIES:
            data = http_get_json(
                "https://remotive.com/api/remote-jobs",
                params={"category": cat},
                timeout=self.timeout_seconds,
            )
            jobs = (data or {}).get("jobs") or []
            for r in jobs:
                if not isinstance(r, dict):
                    continue
                url = r.get("url") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                company = (r.get("company_name") or "").strip()
                title = (r.get("title") or "").strip()
                if not (company and title):
                    continue
                yield RawJob(
                    application_url=url,
                    company=company, title=title,
                    location=r.get("candidate_required_location") or "Remote",
                    remote=True,
                    description=r.get("description") or "",
                    requirements=[str(t) for t in (r.get("tags") or [])][:8],
                    salary_range=r.get("salary") or "Unknown",
                    posted_date=(r.get("publication_date") or "")[:10],
                    platform="Remotive",
                    source=self.name,
                )


register(RemotiveSource())
