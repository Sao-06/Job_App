"""
Adzuna — keyed public job-search API, GLOBAL.

Requires:
  ADZUNA_APP_ID    short hex string from the Adzuna developer console
  ADZUNA_APP_KEY   32-char hex secret

Both must be present, otherwise the source silently does NOT register.

Docs: https://developer.adzuna.com/docs/search
Endpoint: https://api.adzuna.com/v1/api/jobs/{country}/search/{page}

Adzuna supports 19 country verticals (us, gb, de, fr, ca, au, in, br,
mx, sg, nl, pl, it, es, za, at, be, ch, nz). We rotate countries
ALONGSIDE queries — each query in a batch hits a different country —
so over a few cycles the index is filled with jobs from across every
supported region. Same call budget as US-only mode (8 calls × 3 pages
per cycle), but ~19× the geographic coverage.

The free tier allows ~1000 calls/day and returns up to 50 results per
page.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Iterator

from .base import RawJob, is_remote_location, GENERAL_QUERIES, QueryRotator
from .registry import register
from ._http import http_get_json


_API_BASE = "https://api.adzuna.com/v1/api/jobs"


class AdzunaSource:
    name = "api:adzuna"
    cadence_seconds = 30 * 60
    timeout_seconds = 12

    # Every Adzuna country vertical. Order chosen to front-load the
    # largest-volume markets so a fresh boot fills the index with
    # diverse-but-recognizable jobs first; smaller markets follow.
    # Reference: https://developer.adzuna.com/overview
    COUNTRIES: tuple[str, ...] = (
        # Anglosphere + EU majors (highest job volume per slug)
        "us", "gb", "ca", "au", "de", "fr", "nl", "it", "es", "ch",
        # Growth + emerging markets — under-represented in remote-only feeds
        "in", "sg", "br", "mx", "pl", "at", "be", "za", "nz",
    )
    RESULTS_PER_PAGE = 50
    MAX_PAGES = 3

    # Rotate through ~50 cross-industry queries 8 at a time AND rotate
    # through 19 countries in lockstep. Same total call budget as the
    # old US-only path (8×3=24 calls per cycle) but each cycle now
    # spans 8 different countries instead of just 1. With cadence=30
    # min, every (query, country) pair lands within ~5 hours.
    QUERY_BATCH = 8

    def __init__(self, app_id: str, app_key: str):
        self.app_id = app_id
        self.app_key = app_key
        self.rotator = QueryRotator(GENERAL_QUERIES, batch_size=self.QUERY_BATCH)
        # Country rotator advances independently of query rotator so the
        # (query, country) pairing isn't always the same — over time every
        # query is tried in every country.
        self.country_rotator = QueryRotator(self.COUNTRIES, batch_size=self.QUERY_BATCH)

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        queries = self.rotator.next_batch()
        countries = self.country_rotator.next_batch()
        # Pair each query with one country in lockstep. If somehow the
        # batches differ in length (only happens with a tiny COUNTRIES
        # list), fall back to the first country.
        for query, country in zip(queries, countries or [self.COUNTRIES[0]] * len(queries)):
            for page in range(1, self.MAX_PAGES + 1):
                url = f"{_API_BASE}/{country}/search/{page}"
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
                            # Per-country currency hint via the country slug.
                            # Adzuna's salary fields are always in local
                            # currency; we surface the symbol so users can
                            # tell at a glance ("£60k" vs "$60k").
                            sym = _CURRENCY_SYMBOL.get(country, "$")
                            salary = f"{sym}{lo:,.0f}-{sym}{hi:,.0f}/yr"
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


# Per-country currency symbol for salary display. Falls back to $ for any
# country not listed here (the result is just a small visual quirk on the
# salary chip, never a correctness issue).
_CURRENCY_SYMBOL: dict[str, str] = {
    "us": "$",  "ca": "C$", "au": "A$", "nz": "NZ$",
    "gb": "£",
    "de": "€",  "fr": "€",  "nl": "€",  "it": "€",  "es": "€",
    "at": "€",  "be": "€",
    "ch": "CHF ",
    "pl": "zł",
    "br": "R$",
    "mx": "MX$",
    "in": "₹",
    "sg": "S$",
    "za": "R",
}

_app_id  = os.environ.get("ADZUNA_APP_ID")
_app_key = os.environ.get("ADZUNA_APP_KEY")
if _app_id and _app_key:
    register(AdzunaSource(_app_id, _app_key))
