"""
Findwork.dev — keyed dev-jobs API.

Requires:
  FINDWORK_API_KEY   token from https://findwork.dev/developers/

Auth header: ``Authorization: Token {key}``.
Endpoint:    https://findwork.dev/api/jobs/

Returns paginated results; each page exposes ``next`` as a full URL we
can follow until exhaustion. Findwork's catalog is dev-heavy and remote-
biased — a useful complement to the broader keyword-search APIs.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Iterator

from .base import RawJob, is_remote_location
from .registry import register
from ._http import http_get_json


_BASE = "https://findwork.dev/api/jobs/"


class FindworkSource:
    name = "api:findwork"
    cadence_seconds = 30 * 60
    timeout_seconds = 15

    MAX_PAGES = 10
    SEARCH_TERMS = (
        "engineer",
        "hardware",
        "fpga",
        "machine learning",
        "data",
    )

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"Authorization": f"Token {api_key}"}

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        for term in self.SEARCH_TERMS:
            url: str | None = _BASE
            params: dict | None = {"search": term, "sort_by": "date", "order": "desc"}
            for _page in range(self.MAX_PAGES):
                if not url:
                    break
                data = http_get_json(url, params=params, headers=self.headers,
                                     timeout=self.timeout_seconds)
                if not isinstance(data, dict):
                    break
                results = data.get("results") or []
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    apply_url = r.get("url") or ""
                    if not apply_url or apply_url in seen:
                        continue
                    seen.add(apply_url)
                    title = r.get("role") or ""
                    company = r.get("company_name") or ""
                    if not (company and title):
                        continue
                    location = r.get("location") or ""
                    remote = bool(r.get("remote")) or is_remote_location(location)
                    keywords = r.get("keywords") or []
                    if isinstance(keywords, str):
                        keywords = [keywords]
                    yield RawJob(
                        application_url=apply_url,
                        company=company,
                        title=title,
                        location=location or "Remote",
                        remote=remote,
                        description=r.get("text") or "",
                        requirements=[str(k) for k in keywords if k][:8],
                        posted_date=str(r.get("date_posted") or "")[:10],
                        platform=r.get("source") or "Findwork",
                        source=self.name,
                    )
                # Pagination via the API's `next` URL (already includes query).
                url = data.get("next") or None
                params = None
                if not results:
                    break


_key = os.environ.get("FINDWORK_API_KEY")
if _key:
    register(FindworkSource(_key))
