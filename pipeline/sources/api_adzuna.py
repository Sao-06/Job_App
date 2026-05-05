"""
Adzuna — keyed public job-search API.

Requires:
  ADZUNA_APP_ID    short hex string from the Adzuna developer console
  ADZUNA_APP_KEY   32-char hex secret

Both must be present, otherwise the source silently does NOT register.

Docs: https://developer.adzuna.com/docs/search
Endpoint: https://api.adzuna.com/v1/api/jobs/{country}/search/{page}

The free tier allows ~1000 calls/day and returns up to 50 results per
page. We walk pages 1..MAX_PAGES across a curated set of broad
queries that cover the user's tech / hardware bias.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Iterator

from .base import RawJob, is_remote_location
from .registry import register
from ._http import http_get_json


_API_BASE = "https://api.adzuna.com/v1/api/jobs"


class AdzunaSource:
    name = "api:adzuna"
    cadence_seconds = 30 * 60
    timeout_seconds = 12

    # Country code → Adzuna's country slug. We default to US.
    COUNTRY = "us"
    RESULTS_PER_PAGE = 50
    MAX_PAGES = 5            # 5 × 50 × len(QUERIES) ≈ 2500 jobs/cycle

    QUERIES = (
        "engineer",
        "software engineer",
        "hardware engineer",
        "data scientist",
        "machine learning",
        "fpga",
    )

    def __init__(self, app_id: str, app_key: str):
        self.app_id = app_id
        self.app_key = app_key

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        for query in self.QUERIES:
            for page in range(1, self.MAX_PAGES + 1):
                url = f"{_API_BASE}/{self.COUNTRY}/search/{page}"
                data = http_get_json(url, params={
                    "app_id": self.app_id,
                    "app_key": self.app_key,
                    "results_per_page": self.RESULTS_PER_PAGE,
                    "what": query,
                    "content-type": "application/json",
                }, timeout=self.timeout_seconds)
                results = (data or {}).get("results") or []
                if not results:
                    break
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    apply_url = r.get("redirect_url") or ""
                    if not apply_url or apply_url in seen:
                        continue
                    seen.add(apply_url)
                    company = ((r.get("company") or {}).get("display_name")) or ""
                    title = r.get("title") or ""
                    if not (company and title):
                        continue
                    location = ((r.get("location") or {}).get("display_name")) or ""
                    salary = "Unknown"
                    try:
                        lo = float(r.get("salary_min") or 0)
                        hi = float(r.get("salary_max") or 0)
                        if lo > 0 and hi > 0:
                            salary = f"${lo:,.0f}-${hi:,.0f}/yr"
                    except (TypeError, ValueError):
                        pass
                    yield RawJob(
                        application_url=apply_url,
                        company=company,
                        title=title,
                        location=location,
                        remote=is_remote_location(location)
                                or "remote" in (r.get("category") or {}).get("label", "").lower(),
                        description=r.get("description") or "",
                        salary_range=salary,
                        posted_date=(r.get("created") or "")[:10],
                        platform="Adzuna",
                        source=self.name,
                    )
                if len(results) < self.RESULTS_PER_PAGE:
                    break


_app_id  = os.environ.get("ADZUNA_APP_ID")
_app_key = os.environ.get("ADZUNA_APP_KEY")
if _app_id and _app_key:
    register(AdzunaSource(_app_id, _app_key))
