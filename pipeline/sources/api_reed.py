"""
Reed — UK-focused public job-search API, keyed.

Requires:
  REED_API_KEY   UUID-shaped string from https://www.reed.co.uk/developers

Auth is HTTP Basic with the API key as the username and an empty
password.

Reed's market is primarily UK roles, but the data set still includes
plenty of remote / hybrid listings worth pulling in. Up to 100 results
per page; we walk pages until exhaustion or the safety cap.

Docs: https://www.reed.co.uk/developers/jobseeker
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Iterator

from .base import RawJob, is_remote_location
from .registry import register
from ._http import http_get_json, basic_auth_header


_BASE = "https://www.reed.co.uk/api/1.0/search"


class ReedSource:
    name = "api:reed"
    cadence_seconds = 30 * 60
    timeout_seconds = 12

    RESULTS_PER_PAGE = 100
    MAX_PAGES = 5

    QUERIES = (
        "engineer",
        "software engineer",
        "hardware engineer",
        "data scientist",
        "machine learning",
    )

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _auth_headers(self) -> dict:
        return {"Authorization": basic_auth_header(self.api_key, "")}

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        for query in self.QUERIES:
            offset = 0
            for _page in range(self.MAX_PAGES):
                data = http_get_json(_BASE, params={
                    "keywords": query,
                    "resultsToTake": self.RESULTS_PER_PAGE,
                    "resultsToSkip": offset,
                }, headers=self._auth_headers(),
                  timeout=self.timeout_seconds)
                results = (data or {}).get("results") or []
                if not results:
                    break
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    apply_url = r.get("jobUrl") or ""
                    if not apply_url or apply_url in seen:
                        continue
                    seen.add(apply_url)
                    title = r.get("jobTitle") or ""
                    company = r.get("employerName") or ""
                    if not (company and title):
                        continue
                    location = r.get("locationName") or ""
                    currency = (r.get("currency") or "GBP").strip().upper()
                    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, currency + " ")
                    salary = "Unknown"
                    try:
                        lo = float(r.get("minimumSalary") or 0)
                        hi = float(r.get("maximumSalary") or 0)
                        if lo > 0 and hi > 0:
                            salary = f"{sym}{lo:,.0f}-{sym}{hi:,.0f}/yr"
                    except (TypeError, ValueError):
                        pass
                    yield RawJob(
                        application_url=apply_url,
                        company=company,
                        title=title,
                        location=location,
                        remote=is_remote_location(location),
                        description=r.get("jobDescription") or "",
                        salary_range=salary,
                        posted_date=str(r.get("date") or "")[:10],
                        platform="Reed",
                        source=self.name,
                    )
                if len(results) < self.RESULTS_PER_PAGE:
                    break
                offset += self.RESULTS_PER_PAGE


_key = os.environ.get("REED_API_KEY")
if _key:
    register(ReedSource(_key))
