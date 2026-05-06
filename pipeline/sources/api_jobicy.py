"""
Jobicy — public remote-jobs API, no key required.
Docs: https://jobicy.com/jobs-rss-feed
The JSON endpoint returns up to 50 jobs at a time across multiple
categories.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from .base import RawJob
from .registry import register
from ._http import http_get_json


_BASE = "https://jobicy.com/api/v2/remote-jobs"


class JobicySource:
    name = "api:jobicy"
    cadence_seconds = 30 * 60
    timeout_seconds = 12

    CATEGORIES = (
        "engineering", "data-science", "design", "devops", "tech",
        "product", "marketing", "sales", "business",
        "writing", "finance", "human-resources", "customer-service",
        "operations", "consulting",
    )

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        for cat in self.CATEGORIES:
            # Dropped the `geo: "usa"` filter — Jobicy is explicitly a
            # remote-jobs board and the geo filter was artificially pinning
            # the feed to US-only postings even though the upstream catalog
            # spans every continent. Without it we get genuinely global
            # remote inventory (Europe, LatAm, APAC, anywhere).
            data = http_get_json(_BASE, params={
                "count": 50, "industry": cat,
            }, timeout=self.timeout_seconds)
            jobs = (data or {}).get("jobs") or []
            for r in jobs:
                if not isinstance(r, dict):
                    continue
                url = r.get("url") or r.get("jobApplyUrl") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                title = r.get("jobTitle") or ""
                company = r.get("companyName") or ""
                if not (company and title):
                    continue
                location = r.get("jobGeo") or "Remote"
                tags = r.get("jobIndustry") or r.get("jobLevel") or []
                if isinstance(tags, str):
                    tags = [tags]
                yield RawJob(
                    application_url=url,
                    company=company,
                    title=title,
                    location=str(location),
                    remote=True,
                    description=r.get("jobDescription") or "",
                    requirements=[str(t) for t in tags][:8],
                    posted_date=(r.get("pubDate") or "")[:10],
                    platform="Jobicy",
                    source=self.name,
                )


register(JobicySource())
