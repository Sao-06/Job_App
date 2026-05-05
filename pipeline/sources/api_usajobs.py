"""
USAJobs.gov — federal job postings.

Requires:
  USAJOBS_USER_AGENT  the email you registered with USAJobs Developer
  USAJOBS_API_KEY     the auth key issued by USAJobs

If either is missing the source silently does NOT register itself.

Docs: https://developer.usajobs.gov/api-reference/get-api-search
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Iterator

from .base import RawJob
from .registry import register
from ._http import http_get_json


_BASE = "https://data.usajobs.gov/api/search"


class USAJobsSource:
    name = "api:usajobs"
    cadence_seconds = 30 * 60
    timeout_seconds = 15

    def __init__(self, user_agent: str, api_key: str):
        self.user_agent = user_agent
        self.api_key = api_key

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        # The API caps at 500 results per response and 10000 total via paging.
        page = 1
        while page < 20:                  # safety cap
            data = http_get_json(_BASE, params={
                "ResultsPerPage": 500, "Page": page,
            }, headers={
                "Authorization-Key": self.api_key,
                "User-Agent": self.user_agent,
                "Host": "data.usajobs.gov",
            }, timeout=self.timeout_seconds)
            if not data:
                break
            items = (((data or {}).get("SearchResult") or {}).get("SearchResultItems")) or []
            if not items:
                break
            for it in items:
                d = (it.get("MatchedObjectDescriptor") or {})
                url = d.get("ApplyURI") and (d["ApplyURI"][0] if d["ApplyURI"] else "") or d.get("PositionURI") or ""
                title = d.get("PositionTitle") or ""
                org = d.get("OrganizationName") or d.get("DepartmentName") or ""
                if not (url and title and org):
                    continue
                locs = d.get("PositionLocationDisplay") or ""
                remote = "remote" in locs.lower() if locs else False
                pay = (d.get("PositionRemuneration") or [{}])[0]
                salary = "Unknown"
                try:
                    lo = float(pay.get("MinimumRange") or 0)
                    hi = float(pay.get("MaximumRange") or 0)
                    if lo > 0 and hi > 0:
                        salary = f"${lo:,.0f}-${hi:,.0f}/yr"
                except Exception:
                    pass
                desc = (d.get("UserArea") or {}).get("Details", {}).get("JobSummary") or ""
                yield RawJob(
                    application_url=url,
                    company=org, title=title,
                    location=locs or "United States",
                    remote=remote,
                    description=desc,
                    salary_range=salary,
                    posted_date=(d.get("PublicationStartDate") or "")[:10],
                    platform="USAJobs",
                    source=self.name,
                )
            if len(items) < 500:
                break
            page += 1


_ua = os.environ.get("USAJOBS_USER_AGENT")
_key = os.environ.get("USAJOBS_API_KEY")
if _ua and _key:
    register(USAJobsSource(_ua, _key))
