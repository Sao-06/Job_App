"""
JobSpy — wraps the ``python-jobspy`` library to scrape Indeed, Glassdoor,
and ZipRecruiter.

Why these three boards
──────────────────────
None of our other sources index the long-tail of non-tech, non-remote,
non-Fortune-500 jobs that flood these mainstream aggregators. Indeed in
particular is the largest US job board by volume; Glassdoor and
ZipRecruiter add coverage of mid-market and trades roles that the
keyed APIs (Adzuna, USAJobs, etc.) consistently underrepresent.

Why LinkedIn is explicitly NOT included
───────────────────────────────────────
LinkedIn aggressively pursues scrapers under the CFAA, the DMCA, and
their ToS. The Proxycurl shutdown (July 2026) is the most recent
high-profile precedent — they were sued into oblivion for exactly the
kind of automated-fetch we'd be doing here. The maintainer of
``python-jobspy`` himself recommends users skip the LinkedIn site_name.
We honor that. Even if LinkedIn is wired via env var or yaml in a
future cleanup, this source MUST NOT add it. Hardcoded list only.

Optional dependency
───────────────────
``python-jobspy`` is heavy (pandas + tls_client + lxml) and not strictly
required for the app to boot. We wrap the import in try/except and
silently skip ``register(...)`` when the lib isn't installed — mirroring
the env-var-missing pattern used by ``api_adzuna`` and friends.

Cadence + safety
────────────────
* 90-minute cadence — each tick fans out across 3 boards × 2 queries
  (6 HTTP fetches), so the load is mild even on a Pi.
* ``hours_old=72`` — only the last 3 days of postings, so we don't
  re-flood the index with the same week-old listings every cycle.
* Per-board calls are individually wrapped in try/except so a rate-limit
  on one board doesn't kill the others.
* Defensive 200-row cap per ``fetch()`` call.
"""

from __future__ import annotations

import traceback
from datetime import datetime
from typing import Iterator

from .base import RawJob, GENERAL_QUERIES, QueryRotator, strip_html
from .registry import register


# ── Optional dependency guard ─────────────────────────────────────────────────
# If python-jobspy isn't installed, the file still imports cleanly — we just
# never register the source. Deployments without the lib boot normally.
try:
    from jobspy import scrape_jobs as _scrape_jobs  # type: ignore
    _JOBSPY_AVAILABLE = True
except Exception:
    _scrape_jobs = None  # type: ignore
    _JOBSPY_AVAILABLE = False


# ── Site list. HARDCODED. Do not extend with LinkedIn. ────────────────────────
_SITES: tuple[str, ...] = ("indeed", "glassdoor", "zip_recruiter")

# Display name per site slug — surfaced as RawJob.platform.
_PLATFORM_LABEL: dict[str, str] = {
    "indeed":        "Indeed",
    "glassdoor":     "Glassdoor",
    "zip_recruiter": "ZipRecruiter",
}


class JobSpySource:
    name = "scraper:jobspy"
    cadence_seconds = 90 * 60
    timeout_seconds = 60  # the lib makes multiple internal HTTP calls per scrape

    QUERY_BATCH = 2          # 2 queries × 3 boards = 6 fetches per cycle
    RESULTS_PER_CALL = 20    # JobSpy default is 15; bumped modestly for breadth
    HOURS_OLD = 72           # last 3 days only — avoids re-ingesting stale listings
    MAX_ROWS_PER_FETCH = 200 # defensive cap — one fetch should never balloon

    def __init__(self) -> None:
        self.rotator = QueryRotator(GENERAL_QUERIES, batch_size=self.QUERY_BATCH)

    # ── core loop ────────────────────────────────────────────────────────────
    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        if not _JOBSPY_AVAILABLE or _scrape_jobs is None:
            return iter(())

        # Lazy-import the salary helper so the heavy ``pipeline.scrapers``
        # module isn't loaded just because this source was imported.
        try:
            from ..scrapers import normalize_salary
        except Exception:
            normalize_salary = None  # type: ignore

        seen: set[str] = set()
        out: list[RawJob] = []
        emitted = 0

        for query in self.rotator.next_batch():
            for site in _SITES:
                if emitted >= self.MAX_ROWS_PER_FETCH:
                    break
                try:
                    df = _scrape_jobs(
                        site_name=[site],
                        search_term=query,
                        location="",                # broadest dataset — no city filter
                        results_wanted=self.RESULTS_PER_CALL,
                        hours_old=self.HOURS_OLD,
                        country_indeed="USA",       # scopes Indeed; ignored by the others
                    )
                except Exception:
                    # Rate limit, IP block, parser drift — log and move on.
                    traceback.print_exc()
                    continue

                # ``df`` is a pandas DataFrame; iterate as plain dicts so we
                # don't take a hard pandas dependency in this module.
                rows = []
                try:
                    if df is not None and getattr(df, "empty", True) is False:
                        rows = df.to_dict("records")
                except Exception:
                    traceback.print_exc()
                    rows = []

                for r in rows:
                    if emitted >= self.MAX_ROWS_PER_FETCH:
                        break
                    if not isinstance(r, dict):
                        continue

                    apply_url = (_clean(r.get("job_url_direct"))
                                 or _clean(r.get("job_url")))
                    if not apply_url or apply_url in seen:
                        continue
                    company = _clean(r.get("company"))
                    title = _clean(r.get("title"))
                    if not (company and title):
                        continue
                    seen.add(apply_url)

                    location = _clean(r.get("location"))
                    is_remote = bool(r.get("is_remote"))

                    # Salary — pandas often hands back NaN floats; the
                    # normalize_salary helper handles those tokens.
                    salary = "Unknown"
                    if normalize_salary is not None:
                        try:
                            salary = normalize_salary(
                                r.get("min_amount"),
                                r.get("max_amount"),
                                str(r.get("interval") or "yr"),
                            )
                        except Exception:
                            salary = "Unknown"

                    posted = ""
                    raw_date = r.get("date_posted")
                    if raw_date is not None:
                        s = str(raw_date)
                        # filter out the pandas NaT / nan sentinels
                        if s and s.lower() not in ("nat", "nan", "none"):
                            posted = s[:10]

                    # Each yielded row tags its own board so the dev page
                    # reports indeed/glassdoor/ziprecruiter separately even
                    # though the registry key is the umbrella scraper.
                    row_site = _clean(r.get("site")).lower() or site
                    label = _PLATFORM_LABEL.get(row_site, row_site.title() or "JobSpy")
                    src = f"scraper:jobspy:{row_site}"

                    # Indeed/Glassdoor/ZipRecruiter return the full posting body
                    # in the dataframe — keep it (HTML-stripped, length-capped)
                    # so scoring has real text to match against. The upsert
                    # layer trims past 16 KB anyway.
                    description = strip_html(_clean(r.get("description")))
                    if len(description) > 8000:
                        description = description[:8000].rsplit(" ", 1)[0] + "…"

                    out.append(RawJob(
                        application_url=apply_url,
                        company=company,
                        title=title,
                        location=location,
                        remote=is_remote,
                        description=description,
                        requirements=[],
                        salary_range=salary,
                        posted_date=posted,
                        platform=label,
                        source=src,
                    ))
                    emitted += 1

            if emitted >= self.MAX_ROWS_PER_FETCH:
                break

        return iter(out)


def _clean(v) -> str:
    """Coerce DataFrame cells (which may be NaN floats / None / etc.) to str."""
    if v is None:
        return ""
    s = str(v).strip()
    if not s or s.lower() in ("nan", "nat", "none", "null"):
        return ""
    return s


# Register only when the optional dependency is importable. Mirrors the
# env-var-missing pattern: deployments without the lib still boot, they
# just don't get this source.
if _JOBSPY_AVAILABLE:
    register(JobSpySource())
