"""
Himalayas — public job-search API, no key required.
Docs: https://himalayas.app/jobs/api/search

Walks the search endpoint across a curated set of broad queries to pull
remote / US jobs. We stop per-query when a page returns fewer than the
batch size.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from .base import RawJob
from .registry import register
from ._http import http_get_json


def _norm_date(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        try:
            v = float(value)
            if v > 10_000_000_000:
                v /= 1000
            return datetime.utcfromtimestamp(v).date().isoformat()
        except Exception:
            return ""
    return str(value)[:10]


_BASE = "https://himalayas.app/jobs/api/search"


class HimalayasSource:
    name = "api:himalayas"
    cadence_seconds = 25 * 60
    timeout_seconds = 12

    QUERIES = (
        # Tech
        "software engineer", "data scientist", "machine learning",
        # Sales / customer-facing
        "account executive", "sales", "customer success",
        # Marketing / content
        "marketing", "content", "brand",
        # Ops / PM / design
        "product manager", "project manager", "designer", "operations",
        # Finance / HR / legal / education
        "accountant", "recruiter", "legal", "teacher", "consultant",
    )
    BATCH = 50

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        for q in self.QUERIES:
            page = 0
            while page < 6:               # safety cap
                # Dropped the `country: "US"` filter — Himalayas catalogs
                # remote roles globally and the country pin was halving the
                # results to US-only listings. The native location field on
                # each posting still tells us which countries are eligible.
                data = http_get_json(_BASE, params={
                    "q": q, "limit": self.BATCH, "offset": page * self.BATCH,
                }, timeout=self.timeout_seconds)
                jobs = (data or {}).get("jobs") or []
                if not jobs:
                    break
                for item in jobs:
                    if not isinstance(item, dict):
                        continue
                    url = item.get("applicationLink") or item.get("url") or ""
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    company = item.get("companyName") or item.get("company") or ""
                    title   = item.get("title") or ""
                    if not (company and title):
                        continue
                    locs = item.get("locationRestrictions") or []
                    loc_names = [
                        (l.get("name") if isinstance(l, dict) else str(l))
                        for l in locs if l
                    ]
                    location = ", ".join([n for n in loc_names if n]) or "Remote"
                    salary = "Unknown"
                    lo, hi = item.get("minSalary"), item.get("maxSalary")
                    try:
                        if lo and hi:
                            salary = f"${float(lo):,.0f}-${float(hi):,.0f}/yr"
                    except (TypeError, ValueError):
                        pass
                    tags = item.get("skills") or item.get("categories") or []
                    yield RawJob(
                        application_url=url,
                        company=company, title=title,
                        location=location, remote=True,
                        description=item.get("description") or "",
                        requirements=[str(t) for t in (tags or [])][:8],
                        salary_range=salary,
                        posted_date=_norm_date(item.get("pubDate") or item.get("postedAt")),
                        platform="Himalayas",
                        source=self.name,
                    )
                if len(jobs) < self.BATCH:
                    break
                page += 1


register(HimalayasSource())
