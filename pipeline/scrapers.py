"""
pipeline/scrapers.py
────────────────────
Job-board scraping clients: JobSpy (LinkedIn/Indeed/Glassdoor/ZipRecruiter),
a legacy Indeed stub, and the SimplifyJobs GitHub README scraper.
"""

import re
from datetime import date

from .config import console, MAX_SCRAPE_JOBS
from .helpers import (infer_experience_level, infer_education_required,
                      infer_citizenship_required, deduplicate_jobs,
                      clean_location_for_glassdoor)


# ── Query + salary helpers ────────────────────────────────────────────────────

NAN_TOKENS = {"", "nan", "none", "null", "n/a", "0", "0.0", "-"}


def normalize_salary(raw_min, raw_max, interval: str = "yr") -> str:
    """Return ``$lo–$hi/iv`` or ``"Unknown"`` — never emits ``nan`` / ``$0``."""
    def _bad(v):
        return v is None or str(v).strip().lower() in NAN_TOKENS
    if _bad(raw_min) or _bad(raw_max):
        return "Unknown"
    try:
        lo, hi = float(raw_min), float(raw_max)
    except (TypeError, ValueError):
        return "Unknown"
    if lo <= 0 or hi <= 0 or hi < lo:
        return "Unknown"
    iv = (interval or "yr").strip().lower()
    if iv in NAN_TOKENS:
        iv = "yr"
    return f"${lo:,.0f}–${hi:,.0f}/{iv}"


def sanitize_salary_field(value) -> str:
    """Coerce any pre-formatted salary string to ``"Unknown"`` if junk."""
    if value is None:
        return "Unknown"
    s = str(value).strip()
    if not s or s.lower() in NAN_TOKENS or "nan" in s.lower() or "$0" in s:
        return "Unknown"
    return s


def expand_title_queries(title: str) -> list:
    """Return up to 3 specific search variants for *any* target title.

    No role-specific tokens are baked in — variants derive from the title
    itself. The bare token ``"Engineer"`` is rejected as too generic.
    """
    base = (title or "").strip()
    if not base or base.lower() == "engineer":
        return []
    variants = [
        base,
        f'{base} Engineer' if "engineer" not in base.lower() else f'{base} Intern',
        f'{base} Intern' if "intern" not in base.lower() else f'{base} Design',
    ]
    seen, out = set(), []
    for v in variants:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def distribute_quota(total: int, n_buckets: int) -> list:
    """Split *total* across *n_buckets*; remainder lands in the first buckets."""
    if n_buckets <= 0:
        return []
    base, rem = divmod(max(total, 0), n_buckets)
    return [base + (1 if i < rem else 0) for i in range(n_buckets)]


# ── Base ───────────────────────────────────────────────────────────────────────

class JobBoardClient:
    """Abstract base for live job-board integrations."""

    def fetch_jobs(self, titles: list, location: str, days: int = 14) -> list:
        raise NotImplementedError


# ── JobSpy (multi-board) ───────────────────────────────────────────────────────

class JobSpyClient(JobBoardClient):
    """Scrapes LinkedIn, Indeed, and Glassdoor via python-jobspy.

    ZipRecruiter is intentionally excluded — it surfaces aggregator noise that
    frequently hides citizenship/clearance requirements.
    """

    SITE_NAMES = ["indeed", "linkedin", "glassdoor"]
    PRIORITY_WEIGHT = 2  # Phase-1 suggested titles get this many quota shares

    def fetch_jobs(self, titles: list, location: str, days: int = 14,
                   max_jobs: int = None, priority_titles: list = None) -> list:
        try:
            from jobspy import scrape_jobs
        except ImportError:
            console.print(
                "  [yellow]python-jobspy not installed — "
                "run: pip install python-jobspy[/yellow]"
            )
            return []

        cap = max_jobs or MAX_SCRAPE_JOBS

        # Relevance fix: drop the bare "Engineer" placeholder and expand each
        # remaining Phase-1 title into specific search variants. Phase-1
        # suggested titles (priority_titles) are placed first AND get a
        # heavier quota share so we don't drown in generic listings.
        priority_set = {
            (t or "").strip().lower()
            for t in (priority_titles or [])
            if t and t.strip().lower() != "engineer"
        }
        cleaned_titles = [t for t in titles if t and t.strip().lower() != "engineer"]
        # Ensure all priority titles are present, in front, dedup-preserving order.
        ordered: list = []
        seen_titles: set = set()
        for src in [list(priority_titles or []), cleaned_titles]:
            for t in src:
                key = (t or "").strip().lower()
                if not key or key == "engineer" or key in seen_titles:
                    continue
                seen_titles.add(key)
                ordered.append(t.strip())

        # Build expanded queries paired with the source title's weight.
        expanded: list = []   # list[(query, weight)]
        for t in ordered:
            weight = self.PRIORITY_WEIGHT if t.lower() in priority_set else 1
            for q in expand_title_queries(t):
                expanded.append((q, weight))
        if not expanded:
            expanded = [(t, 1) for t in (cleaned_titles or titles)]

        # Weighted quota distribution: each share = cap / total_weight.
        total_weight = sum(w for _, w in expanded) or 1
        quotas = [max(1, (cap * w) // total_weight) for _, w in expanded]
        all_raw: list = []

        for (query, _w), quota in zip(expanded, quotas):
            if len(all_raw) >= cap:
                break
            if quota <= 0:
                continue
            remaining = cap - len(all_raw)
            results_wanted = max(1, min(quota, remaining))
            try:
                df = scrape_jobs(
                    site_name=self.SITE_NAMES,
                    search_term=query,
                    location=location,
                    results_wanted=results_wanted,
                    hours_old=days * 24,
                    country_indeed="USA",
                )
                all_raw.extend(df.to_dict("records"))
                console.print(f"  📡 '{query}': {len(df)} results scraped (quota {quota})")
            except Exception as e:
                msg = str(e).lower()
                # Glassdoor "location not parsed" → retry without Glassdoor,
                # using a normalized location string.
                if "location not parsed" in msg or "glassdoor" in msg:
                    fallback_loc = clean_location_for_glassdoor(location)
                    fallback_sites = [s for s in self.SITE_NAMES if s != "glassdoor"]
                    console.print(
                        f"  [yellow]Glassdoor parse failure for '{query}' — "
                        f"retrying without Glassdoor (loc={fallback_loc!r})[/yellow]"
                    )
                    try:
                        df = scrape_jobs(
                            site_name=fallback_sites,
                            search_term=query,
                            location=fallback_loc,
                            results_wanted=results_wanted,
                            hours_old=days * 24,
                            country_indeed="USA",
                        )
                        all_raw.extend(df.to_dict("records"))
                        console.print(
                            f"  📡 '{query}' (fallback): {len(df)} results scraped"
                        )
                        continue
                    except Exception as e2:
                        console.print(
                            f"  [yellow]Fallback also failed for '{query}': {e2}[/yellow]"
                        )
                else:
                    console.print(f"  [yellow]JobSpy scrape failed for '{query}': {e}[/yellow]")

        jobs = [self._map(r) for r in all_raw if r.get("job_url")]
        if max_jobs and len(jobs) > max_jobs:
            jobs = jobs[:max_jobs]
            console.print(f"  ✂️  Capped at {max_jobs} jobs (max_scrape_jobs limit)")
        return jobs

    @staticmethod
    def _map(r: dict) -> dict:
        import hashlib

        url   = str(r.get("job_url") or "")
        comp  = str(r.get("company") or "")
        title = str(r.get("title") or "")
        uid   = hashlib.md5(f"{comp}{title}{url}".encode()).hexdigest()[:10]

        sal = normalize_salary(
            r.get("min_amount"), r.get("max_amount"), r.get("interval") or "yr",
        )

        loc    = str(r.get("location") or "")
        pd_raw = r.get("date_posted")
        try:
            posted = pd_raw.isoformat() if hasattr(pd_raw, "isoformat") else str(pd_raw or "")
        except Exception:
            posted = ""

        job = {
            "id":              uid,
            "title":           title,
            "company":         comp,
            "location":        loc,
            "remote":          "remote" in loc.lower() or bool(r.get("is_remote")),
            "posted_date":     posted,
            "description":     str(r.get("description") or ""),
            "requirements":    [],
            "salary_range":    sal,
            "application_url": url,
            "platform":        str(r.get("site") or ""),
            "source":          "jobspy",
        }
        job["experience_level"]     = infer_experience_level(job)
        job["education_required"]   = infer_education_required(job)
        job["citizenship_required"] = infer_citizenship_required(job)
        return job


# ── Indeed stub (legacy) ───────────────────────────────────────────────────────

class IndeedClient(JobBoardClient):
    """Legacy stub — JobSpyClient covers Indeed now."""

    def fetch_jobs(self, titles: list, location: str, days: int = 14) -> list:
        return []


# ── SimplifyJobs (GitHub README) ──────────────────────────────────────────────

class SimplifyJobsScraper:
    """Scrapes the SimplifyJobs Summer 2026 internship board from GitHub."""

    README_URL = (
        "https://raw.githubusercontent.com/SimplifyJobs/"
        "Summer2026-Internships/dev/README.md"
    )

    def fetch_jobs(self, section: str = "hardware") -> list:  # noqa: ARG002
        import urllib.request as _ur

        try:
            with _ur.urlopen(self.README_URL, timeout=15) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            console.print(f"  [yellow]SimplifyJobs fetch failed: {e}[/yellow]")
            return []

        lines = raw.splitlines()

        section_start = -1
        for i, line in enumerate(lines):
            if "hardware engineering" in line.lower() and line.strip().startswith("#"):
                section_start = i
                break
        if section_start == -1:
            console.print("  [yellow]SimplifyJobs: Hardware Engineering section not found[/yellow]")
            return []

        section_lines = []
        for line in lines[section_start + 1:]:
            if line.strip().startswith("##"):
                break
            section_lines.append(line)

        section_text = "\n".join(section_lines)
        row_blocks   = re.findall(r'<tr>(.*?)</tr>', section_text, re.DOTALL | re.I)

        jobs: list = []
        last_company = ""

        for block in row_blocks:
            cells = re.findall(r'<td>(.*?)</td>', block, re.DOTALL | re.I)
            if len(cells) < 4:
                continue

            company_raw = cells[0].strip()
            if company_raw in ("↳", ""):
                company = last_company
            else:
                company = re.sub(r'<[^>]+>', '', company_raw).strip()
                company = company.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                last_company = company

            title = re.sub(r'<[^>]+>', '', cells[1]).strip()
            title = re.sub(r'[\U00010000-\U0010ffff]', '', title).strip()

            location_raw = cells[2].strip()
            location = re.sub(r'<br\s*/?>', ', ', location_raw, flags=re.I)
            location = re.sub(r'<[^>]+>', '', location).strip()
            location = re.sub(r',\s*,', ',', location).strip(", ")

            link_cell = cells[3].strip()
            if "\U0001f512" in link_cell or "closed" in link_cell.lower():
                continue

            url_match = re.search(r'href="(https?://[^"]+)"', link_cell)
            if not url_match:
                continue
            direct = re.findall(r'href="(https?://[^"]+)"', link_cell)
            non_simplify = [u for u in direct if "simplify.jobs" not in u]
            url = non_simplify[0] if non_simplify else direct[0]

            if not company or not title:
                continue

            job_stub = {"title": title, "description": "", "requirements": []}
            job = {
                "id":                   f"simplify_{abs(hash(company + title + url)) % 100000}",
                "title":                title,
                "company":              company,
                "location":             location,
                "remote":               "remote" in location.lower(),
                "posted_date":          date.today().isoformat(),
                "description":          f"{title} internship at {company}.",
                "requirements":         [],
                "salary_range":         "Unknown",
                "application_url":      url,
                "platform":             "SimplifyJobs/GitHub",
                "source":               "simplify",
                "experience_level":     "internship",
                "education_required":   "unknown",
                "citizenship_required": infer_citizenship_required(job_stub),
            }
            jobs.append(job)

        return deduplicate_jobs(jobs)
