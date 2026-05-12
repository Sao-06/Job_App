"""
Workday ATS — public Workday CXS jobs endpoint, no key required.

Endpoint shape:
    POST https://{slug}.{wdN}.myworkdayjobs.com/wday/cxs/{slug}/{site}/jobs
    body: {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

Workday is the dominant ATS for Fortune 500 — non-tech retail, healthcare,
banking, telecom, industrials. The big coverage win for the platform.

Unlike most public ATS endpoints, Workday is **per-tenant triple**: each
company is identified by a (slug, host shard, site) tuple in
``companies.yml`` rather than a single slug. The host shard (``wd1``,
``wd3``, ``wd5``, ``wd501``…) is assigned per Workday customer and cannot
be guessed — it must be discovered by visiting the tenant's careers page
and capturing the redirect.

Per the user's "metadata-only" decision, we deliberately don't pull job
descriptions — only title, location, posted-date, and apply URL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Iterator

from .base import RawJob, is_remote_location
from .registry import register
from ._http import http_post_json


_CONFIG_PATH = Path(__file__).with_name("companies.yml")

# How many jobs to pull per cycle. Workday paginates 20 per page, so
# 200 = 10 pages. Anything beyond that is older inventory we'll catch
# on the next cycle once newer postings get indexed.
_PAGE_LIMIT = 20
_MAX_PAGES = 10
_MAX_JOBS = _PAGE_LIMIT * _MAX_PAGES  # 200


def _coerce_entry(raw: object) -> tuple[str, str, str] | None:
    """Accept either ``{slug, host, site}`` dict or ``"slug:host:site"`` string.

    Returns ``None`` for malformed entries so the caller can skip silently.
    """
    if isinstance(raw, dict):
        slug = str(raw.get("slug") or "").strip()
        host = str(raw.get("host") or "").strip()
        site = str(raw.get("site") or "").strip()
        if slug and host and site:
            return slug, host, site
        return None
    if isinstance(raw, str):
        parts = raw.split(":")
        if len(parts) == 3 and all(p.strip() for p in parts):
            return parts[0].strip(), parts[1].strip(), parts[2].strip()
        return None
    return None


def _load_companies() -> list[tuple[str, str, str]]:
    """Read ``workday:`` from companies.yml. Defensive on shape."""
    try:
        import yaml
    except ImportError:
        return []
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        raw_list = data.get("workday") or []
    except Exception:
        return []
    out: list[tuple[str, str, str]] = []
    for entry in raw_list:
        triple = _coerce_entry(entry)
        if triple is not None:
            out.append(triple)
    return out


_REL_DAYS_RE = re.compile(r"posted\s+(\d+)\+?\s+day", re.IGNORECASE)


def _parse_posted_on(raw: str) -> str:
    """Map Workday's relative ``postedOn`` to an ISO ``YYYY-MM-DD`` date.

    Known variants: "Posted Today", "Posted Yesterday", "Posted N Days Ago",
    "Posted 30+ Days Ago". Localized non-English strings or any other format
    drift returns ``""`` so downstream "fresh-only" filters (`days_old`)
    correctly age the row out instead of treating it as "posted today" — that
    silent fallback masked parse breakage.
    """
    if not raw:
        return ""
    today = datetime.now(timezone.utc).date()
    s = str(raw).strip().lower()
    if "today" in s:
        return today.isoformat()
    if "yesterday" in s:
        return (today - timedelta(days=1)).isoformat()
    m = _REL_DAYS_RE.search(s)
    if m:
        try:
            n = int(m.group(1))
            return (today - timedelta(days=max(0, n))).isoformat()
        except Exception:
            pass
    return ""


def _prettify_slug(slug: str) -> str:
    """Best-effort company name from a slug — used as fallback when the
    Workday payload doesn't carry a display name."""
    return slug.replace("-", " ").replace("_", " ").strip().title()


class WorkdaySource:
    cadence_seconds = 60 * 60      # hourly — large tenants justify the budget
    timeout_seconds = 15

    def __init__(self, slug: str, wd_host: str, site: str):
        self.slug = slug
        self.wd_host = wd_host
        self.site = site
        self.name = f"ats:workday:{slug}"
        self._base = f"https://{slug}.{wd_host}.myworkdayjobs.com"
        self._api_url = f"{self._base}/wday/cxs/{slug}/{site}/jobs"
        self._site_url = f"{self._base}/{site}"
        self._company_label = _prettify_slug(slug)

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        out: list[RawJob] = []
        offset = 0
        for _ in range(_MAX_PAGES):
            body = {
                "appliedFacets": {},
                "limit": _PAGE_LIMIT,
                "offset": offset,
                "searchText": "",
            }
            data = http_post_json(self._api_url, body, timeout=self.timeout_seconds)
            if not isinstance(data, dict):
                break
            postings = data.get("jobPostings") or []
            if not postings:
                break
            for j in postings:
                if not isinstance(j, dict):
                    continue
                external_path = (j.get("externalPath") or "").strip()
                title = (j.get("title") or "").strip()
                if not (external_path and title):
                    continue
                apply_url = f"{self._site_url}{external_path}"
                location = (j.get("locationsText") or "").strip()
                out.append(RawJob(
                    application_url=apply_url,
                    company=self._company_label,
                    title=title,
                    location=location or "United States",
                    remote=is_remote_location(location),
                    description="",                # metadata-only by design
                    posted_date=_parse_posted_on(j.get("postedOn") or ""),
                    platform="Workday",
                    source=self.name,
                ))
                if len(out) >= _MAX_JOBS:
                    break
            if len(out) >= _MAX_JOBS:
                break
            offset += _PAGE_LIMIT
            total = data.get("total")
            # Stop once the server has nothing more to give us.
            if isinstance(total, int) and offset >= total:
                break
        return iter(out)


for _slug, _host, _site in _load_companies():
    register(WorkdaySource(_slug, _host, _site))
