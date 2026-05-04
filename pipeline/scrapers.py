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


# ── Shared GitHub README helpers ──────────────────────────────────────────────

def _fetch_url(url: str, timeout: int = 15) -> str | None:
    """Fetch a URL and return the decoded text, or None on failure."""
    import urllib.request as _ur
    try:
        with _ur.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _strip_html(text: str) -> str:
    text = re.sub(r'<br\s*/?>', ', ', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace(
        "&gt;", ">").replace("&nbsp;", " ")
    return re.sub(r',\s*,', ',', text).strip(", ").strip()


def _make_job(*, company, title, location, url, platform, source,
              posted=None) -> dict:
    job_stub = {"title": title, "description": "", "requirements": []}
    return {
        "id":                   f"{source}_{abs(hash(company + title + url)) % 100000}",
        "title":                title,
        "company":              company,
        "location":             location,
        "remote":               "remote" in location.lower(),
        "posted_date":          posted or date.today().isoformat(),
        "description":          f"{title} at {company}.",
        "requirements":         [],
        "salary_range":         "Unknown",
        "application_url":      url,
        "platform":             platform,
        "source":               source,
        "experience_level":     "internship",
        "education_required":   "unknown",
        "citizenship_required": infer_citizenship_required(job_stub),
    }


# ── SimplifyJobs (GitHub README HTML table) ────────────────────────────────────

class SimplifyJobsScraper:
    """Scrapes the SimplifyJobs Summer 2026 internship board from GitHub.

    Falls back gracefully if the expected section header is renamed:
    tries several keyword variants, then falls back to scanning the
    entire README for any <tr> rows.
    """

    README_URL = (
        "https://raw.githubusercontent.com/SimplifyJobs/"
        "Summer2026-Internships/dev/README.md"
    )

    # Ordered list of section-header keywords to try.  First match wins.
    SECTION_KEYWORDS = [
        "hardware engineering",
        "hardware",
        "electrical engineering",
        "embedded",
        "semiconductor",
        "engineering",
    ]

    def fetch_jobs(self) -> list:
        raw = _fetch_url(self.README_URL)
        if not raw:
            console.print("  [yellow]SimplifyJobs fetch failed[/yellow]")
            return []

        lines = raw.splitlines()

        # Locate the best matching section header.
        section_start = -1
        matched_kw = ""
        for kw in self.SECTION_KEYWORDS:
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("#") and kw in stripped.lower():
                    section_start = i
                    matched_kw = kw
                    break
            if section_start != -1:
                break

        if section_start != -1:
            console.print(
                f"  SimplifyJobs: found section '{matched_kw}' at line {section_start}"
            )
            section_lines = []
            for line in lines[section_start + 1:]:
                if line.strip().startswith("##"):
                    break
                section_lines.append(line)
            search_text = "\n".join(section_lines)
        else:
            # No section found — scan the entire README.
            console.print(
                "  [yellow]SimplifyJobs: no matching section — scanning full README[/yellow]"
            )
            search_text = raw

        return deduplicate_jobs(self._parse_html_table(search_text))

    def _parse_html_table(self, text: str) -> list:
        row_blocks = re.findall(r'<tr>(.*?)</tr>', text, re.DOTALL | re.I)
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
                company = _strip_html(company_raw)
                last_company = company

            title = re.sub(r'[\U00010000-\U0010ffff]', '', _strip_html(cells[1])).strip()
            location = _strip_html(cells[2])

            link_cell = cells[3].strip()
            if "\U0001f512" in link_cell or "closed" in link_cell.lower():
                continue

            urls = re.findall(r'href="(https?://[^"]+)"', link_cell)
            if not urls:
                continue
            non_simplify = [u for u in urls if "simplify.jobs" not in u]
            url = non_simplify[0] if non_simplify else urls[0]

            if not company or not title:
                continue

            jobs.append(_make_job(
                company=company, title=title, location=location,
                url=url, platform="SimplifyJobs/GitHub", source="simplify",
            ))

        return jobs


# ── Jobright-AI (GitHub README Markdown tables) ────────────────────────────────

class JobrightScraper:
    """Scrapes jobright-ai's GitHub internship/new-grad repos.

    Tries several repo names in priority order; collects all that respond.
    Parses Markdown pipe tables: | Company | Title | Location | Work Model | Date |
    """

    BASE = "https://raw.githubusercontent.com/jobright-ai"

    # Repos to try, in priority order.  Year prefix tried for current + prior year.
    REPO_SLUGS = [
        "2026-Tech-Internship",
        "2025-Tech-Internship",
        "2026-Engineer-Internship",
        "2025-Engineer-Internship",
        "2026-Hardware-Engineer-Internship",
        "2025-Hardware-Engineer-Internship",
        "2026-Software-Engineer-New-Grad",
    ]
    BRANCHES = ["main", "master", "dev"]

    def fetch_jobs(self) -> list:
        jobs: list = []
        tried = 0
        for slug in self.REPO_SLUGS:
            raw = None
            for branch in self.BRANCHES:
                url = f"{self.BASE}/{slug}/{branch}/README.md"
                raw = _fetch_url(url, timeout=10)
                if raw:
                    break
            tried += 1
            if not raw:
                continue
            parsed = self._parse_md_table(raw, slug)
            jobs.extend(parsed)
            console.print(
                f"  Jobright [{slug}]: {len(parsed)} listings"
            )
            if len(jobs) >= 200:  # cap total before dedup
                break

        if not jobs and tried:
            console.print(
                "  [yellow]Jobright: no repos responded — check repo names[/yellow]"
            )
        return deduplicate_jobs(jobs)

    def _parse_md_table(self, text: str, repo_slug: str) -> list:
        """Parse Markdown pipe tables from a jobright-ai README.

        Expected column order: Company | Job Title | Location | Work Model | Date
        Column positions are detected dynamically from the header row.
        """
        jobs: list = []
        lines = text.splitlines()

        # Find table header row to detect column positions.
        header_idx = -1
        col_company = col_title = col_location = col_date = -1
        for i, line in enumerate(lines):
            if "|" not in line:
                continue
            cols = [c.strip().lower() for c in line.split("|") if c.strip()]
            if any(k in " ".join(cols) for k in ("company", "job title", "title")):
                header_idx = i
                for j, c in enumerate(cols):
                    if "company" in c:    col_company  = j
                    if "title" in c:      col_title    = j
                    if "location" in c:   col_location = j
                    if "date" in c:       col_date     = j
                break

        if header_idx == -1:
            return []

        for line in lines[header_idx + 2:]:  # skip separator row
            if not line.strip() or not line.strip().startswith("|"):
                continue
            parts = [p.strip() for p in line.split("|")]
            parts = [p for p in parts if p or p == ""]  # keep structure

            def _get(idx):
                return parts[idx] if 0 <= idx < len(parts) else ""

            company_raw = _get(col_company if col_company >= 0 else 1)
            title_raw   = _get(col_title   if col_title   >= 0 else 2)
            loc_raw     = _get(col_location if col_location >= 0 else 3)
            date_raw    = _get(col_date    if col_date    >= 0 else 5)

            # Extract display text and URL from Markdown link [text](url)
            def _md_link(cell):
                m = re.search(r'\[([^\]]+)\]\((https?://[^)]+)\)', cell)
                if m:
                    return m.group(1).strip(), m.group(2).strip()
                return re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', cell).strip(), ""

            company, _ = _md_link(company_raw)
            title, apply_url = _md_link(title_raw)
            if not apply_url:
                # Try finding any URL in the row
                m = re.search(r'https?://\S+', line)
                apply_url = m.group(0).rstrip(')') if m else ""

            location = re.sub(r'\s+', ' ', loc_raw).strip()
            posted   = date_raw.strip()[:10] if date_raw else date.today().isoformat()

            if not company or not title or not apply_url:
                continue

            # Skip rows that are headers/separators
            if company.lower() in ("company", "---", ""):
                continue

            jobs.append(_make_job(
                company=company, title=title, location=location,
                url=apply_url, platform="Jobright/GitHub",
                source="jobright", posted=posted,
            ))

        return jobs


# ── InternList.com ─────────────────────────────────────────────────────────────

class InternListScraper:
    """Scrapes intern-list.com engineering internship listings.

    The site is a JS-rendered SPA; we try the static HTML and
    extract any visible job-card data using regex patterns.
    Falls back gracefully to empty list if the site structure changes.
    """

    URLS = [
        "https://www.intern-list.com/?selectedKey=%F0%9F%9B%A0%EF%B8%8F%20Engineering%20and%20Development",
        "https://www.intern-list.com/",
    ]

    def fetch_jobs(self) -> list:
        import urllib.request as _ur

        for url in self.URLS:
            raw = None
            req = _ur.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
            try:
                with _ur.urlopen(req, timeout=12) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                break
            except Exception:
                continue

        if not raw:
            console.print("  [yellow]InternList: could not reach site[/yellow]")
            return []

        jobs: list = []

        # Pattern 1: JSON-LD / embedded JSON data blobs
        json_blobs = re.findall(
            r'type="application/json"[^>]*>(.*?)</script>',
            raw, re.DOTALL | re.I,
        )
        for blob in json_blobs:
            try:
                import json as _json
                data = _json.loads(blob.strip())
                extracted = self._extract_from_json(data)
                jobs.extend(extracted)
            except Exception:
                pass

        # Pattern 2: HTML job card rows — common patterns
        if not jobs:
            # Try anchor tags with company + role text
            card_pattern = re.compile(
                r'<a[^>]+href="(https?://[^"]+)"[^>]*>.*?'
                r'<[^>]*class="[^"]*(?:company|employer)[^"]*"[^>]*>(.*?)</[^>]+>.*?'
                r'<[^>]*class="[^"]*(?:role|title|position)[^"]*"[^>]*>(.*?)</[^>]+>',
                re.DOTALL | re.I,
            )
            for m in card_pattern.finditer(raw):
                apply_url = m.group(1)
                company   = _strip_html(m.group(2))
                title     = _strip_html(m.group(3))
                if company and title and apply_url:
                    jobs.append(_make_job(
                        company=company, title=title, location="United States",
                        url=apply_url, platform="InternList",
                        source="internlist",
                    ))

        console.print(f"  InternList: {len(jobs)} listings parsed")
        return deduplicate_jobs(jobs)

    def _extract_from_json(self, data) -> list:
        """Recursively dig into a JSON blob for job-like objects."""
        jobs = []
        if isinstance(data, list):
            for item in data:
                jobs.extend(self._extract_from_json(item))
        elif isinstance(data, dict):
            company = data.get("company") or data.get("employer") or ""
            title   = data.get("title") or data.get("role") or data.get("position") or ""
            url     = (data.get("apply_url") or data.get("url") or
                       data.get("link") or data.get("applicationUrl") or "")
            loc     = data.get("location") or data.get("city") or "United States"
            if company and title and url and url.startswith("http"):
                jobs.append(_make_job(
                    company=str(company), title=str(title),
                    location=str(loc), url=str(url),
                    platform="InternList", source="internlist",
                ))
            else:
                for v in data.values():
                    if isinstance(v, (dict, list)):
                        jobs.extend(self._extract_from_json(v))
        return jobs
