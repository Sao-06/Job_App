"""
pipeline/scrapers.py
────────────────────
Job-board scraping clients: JobSpy (LinkedIn/Indeed/Glassdoor/ZipRecruiter),
a legacy Indeed stub, and the SimplifyJobs GitHub README scraper.
"""

import re
from datetime import date

from .config import console, MAX_SCRAPE_JOBS
from .helpers import infer_experience_level, infer_education_required, infer_citizenship_required, deduplicate_jobs


# ── Base ───────────────────────────────────────────────────────────────────────

class JobBoardClient:
    """Abstract base for live job-board integrations."""

    def fetch_jobs(self, titles: list, location: str, days: int = 14) -> list:
        raise NotImplementedError


# ── JobSpy (multi-board) ───────────────────────────────────────────────────────

class JobSpyClient(JobBoardClient):
    """Scrapes LinkedIn, Indeed, Glassdoor, and ZipRecruiter via python-jobspy."""

    def fetch_jobs(self, titles: list, location: str, days: int = 14,
                   max_jobs: int = None) -> list:
        try:
            from jobspy import scrape_jobs
        except ImportError:
            console.print(
                "  [yellow]python-jobspy not installed — "
                "run: pip install python-jobspy[/yellow]"
            )
            return []

        cap = max_jobs or MAX_SCRAPE_JOBS
        per_title = max(5, cap // max(len(titles), 1))
        all_raw: list = []

        for title in titles:
            if len(all_raw) >= cap:
                break
            try:
                df = scrape_jobs(
                    site_name=["linkedin", "indeed", "glassdoor", "zip_recruiter"],
                    search_term=title,
                    location=location,
                    results_wanted=per_title,
                    hours_old=days * 24,
                    country_indeed="USA",
                )
                all_raw.extend(df.to_dict("records"))
                console.print(f"  📡 '{title}': {len(df)} results scraped")
            except Exception as e:
                console.print(f"  [yellow]JobSpy scrape failed for '{title}': {e}[/yellow]")

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

        mn  = r.get("min_amount")
        mx  = r.get("max_amount")
        iv  = r.get("interval") or "yr"
        sal = f"${mn}–${mx}/{iv}" if (mn or mx) else ""

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
                "salary_range":         "",
                "application_url":      url,
                "platform":             "SimplifyJobs/GitHub",
                "source":               "simplify",
                "experience_level":     "internship",
                "education_required":   "unknown",
                "citizenship_required": infer_citizenship_required(job_stub),
            }
            jobs.append(job)

        return deduplicate_jobs(jobs)
