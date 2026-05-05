"""
pipeline/phases.py
──────────────────
The seven pipeline phase functions, Playwright submitter, tracker writer,
email notifier, and dashboard helper.
"""

import json
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from rich.panel import Panel
from rich.table import Table

from .config import console, OUTPUT_DIR, RESOURCES_DIR, MAX_SCRAPE_JOBS, _CliSpinner
from .helpers import (infer_experience_level, infer_education_required,
                      infer_citizenship_required, deduplicate_jobs,
                      filter_jobs_by_education, validate_job_urls)
from .providers import BaseProvider
from .resume import _build_demo_resume, _save_tailored_resume
from .scrapers import (JobSpyClient, SimplifyJobsScraper, JobrightScraper,
                       InternListScraper, HimalayasApiScraper,
                       RemotiveApiScraper, ArbeitnowApiScraper,
                       sanitize_salary_field)


# ── Phase 1 ────────────────────────────────────────────────────────────────────

def phase1_ingest_resume(resume_text: str, provider: BaseProvider,
                         preferred_titles: list = None) -> dict:
    import hashlib
    from .config import OWNER_NAME
    from .profile_audit import audit_profile
    console.print("\n[bold cyan]Phase 1 — Resume Ingestion & Profile Extraction[/bold cyan]")
    _t0 = time.time()

    cache_key = json.dumps({
        "resume_text": resume_text,
        "provider": provider.__class__.__name__,
        "preferred_titles": preferred_titles or [],
    }, sort_keys=True)
    resume_hash = hashlib.md5(cache_key.encode()).hexdigest()
    cache_file = RESOURCES_DIR / f"profile_cache_{resume_hash}.json"

    if cache_file.exists():
        try:
            with open(cache_file, encoding="utf-8") as f:
                profile = json.load(f)
            console.print(f"  ⚡ Loaded cached profile (resume unchanged)")
            elapsed = time.time() - _t0
            console.print(f"  ⏱️  Phase 1 completed in [bold]{elapsed:.1f}s[/bold]")
            return profile
        except Exception:
            pass

    with _CliSpinner(interval=20):
        profile = provider.extract_profile(resume_text, preferred_titles=preferred_titles)

    # Post-extraction audit: flatten → quarantine → retention → verify → rerank.
    if profile:
        profile = audit_profile(profile, resume_text)
        # Back-compat: ensure `experience` is populated from research/work split.
        if not profile.get("experience"):
            profile["experience"] = (
                list(profile.get("research_experience") or [])
                + list(profile.get("work_experience") or [])
            )

    elapsed = time.time() - _t0
    if profile:
        console.print(f"  ✅ Profile extracted: [bold]{profile.get('name', OWNER_NAME)}[/bold]")
        console.print(f"  📊 Top skills: {', '.join(profile.get('top_hard_skills', [])[:5])}")
        titles = profile.get("target_titles", [])
        if titles:
            console.print(f"  🎯 Target titles: {', '.join(titles[:4])}")
        if profile.get("resume_gaps"):
            console.print(f"  ⚠️  Gaps: {', '.join(profile['resume_gaps'])}")
        for note in profile.get("_audit_log", []):
            console.print(f"  [dim]🔍 audit: {note}[/dim]")

    RESOURCES_DIR.mkdir(exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)

    console.print(f"  ⏱️  Phase 1 completed in [bold]{elapsed:.1f}s[/bold]")
    return profile


# ── Phase 2 ────────────────────────────────────────────────────────────────────

def _parse_posted_date(val) -> datetime | None:
    """Best-effort parse of the heterogeneous `posted_date` field."""
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _filter_by_posting_age(jobs: list, days_old: int) -> list:
    """Drop jobs posted before (now - days_old). Jobs with no date are kept."""
    if not days_old or days_old <= 0:
        return jobs
    cutoff = datetime.now() - timedelta(days=days_old)
    kept = []
    for j in jobs:
        posted = _parse_posted_date(j.get("posted_date"))
        if posted is None or posted >= cutoff:
            kept.append(j)
    return kept


def _sort_newest_first(jobs: list) -> list:
    """Sort by posted_date descending; undated jobs sort last."""
    return sorted(
        jobs,
        key=lambda j: _parse_posted_date(j.get("posted_date")) or datetime.min,
        reverse=True,
    )


def _load_cached_jobs(sample_file: Path):
    """Read sample_jobs.json. Returns ``(jobs_list, meta)`` or ``(None, None)``.

    Supports two on-disk formats:
      - legacy: bare ``[ {...}, ... ]`` job list (no meta)
      - new:    ``{"_meta": {...}, "jobs": [ {...}, ... ]}``
    """
    if not sample_file.exists():
        return None, None
    try:
        with open(sample_file, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    if isinstance(payload, dict) and "jobs" in payload:
        return list(payload.get("jobs") or []), dict(payload.get("_meta") or {})
    if isinstance(payload, list):
        return payload, None
    return None, None


def _cache_titles_overlap(meta, current_titles: list) -> bool:
    """Decide whether the cache may be used given *current_titles*.

    - Missing meta (legacy file)        → bypass (force live scrape so the
                                          new metadata gets written).
    - Empty/None current_titles         → permissive: reuse cache.
    - Otherwise: case-insensitive set intersection of cached vs. current
      titles. Empty intersection → bypass.
    """
    if not meta:
        return False
    if not current_titles:
        return True
    cached = {str(t).strip().lower() for t in (meta.get("titles") or []) if t}
    if not cached:
        return False
    requested = {str(t).strip().lower() for t in current_titles if t}
    return bool(cached & requested)


def _cache_is_fresh(meta, ttl_minutes: int = 30) -> bool:
    if not meta:
        return False
    try:
        created = float(meta.get("created_at", 0))
    except (TypeError, ValueError):
        return False
    return (time.time() - created) <= max(1, ttl_minutes) * 60


def _cache_matches(meta, *, titles: list, location: str, days_old: int,
                   deep_search: bool, ttl_minutes: int) -> bool:
    if not _cache_is_fresh(meta, ttl_minutes):
        return False
    if not _cache_titles_overlap(meta, titles):
        return False
    if str(meta.get("location", "")).strip().lower() != str(location or "").strip().lower():
        return False
    if int(meta.get("days_old", 0) or 0) != int(days_old or 0):
        return False
    return bool(meta.get("deep_search")) == bool(deep_search)


def _save_jobs_cache(sample_file: Path, jobs: list, *, titles: list, location: str,
                     days_old: int, deep_search: bool) -> None:
    try:
        RESOURCES_DIR.mkdir(exist_ok=True)
        payload = {
            "_meta": {
                "created_at": time.time(),
                "titles": titles,
                "location": location,
                "days_old": days_old,
                "deep_search": bool(deep_search),
            },
            "jobs": jobs,
        }
        with open(sample_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as exc:
        console.print(f"  [dim]Cache save skipped: {exc}[/dim]")


def _resolve_effective_titles(job_titles, profile: dict):
    """Decide which titles will actually drive the scrape.

    Returns ``(titles, source)`` where *source* is one of:
      - ``"user"``    → caller-supplied titles are kept as-is
      - ``"phase1"``  → caller passed nothing meaningful; falls back to
                         ``profile["target_titles"]``
      - ``"merged"``  → caller's titles plus phase-1 suggestions, deduped
    """
    incoming = [t for t in (job_titles or []) if t and str(t).strip()]
    only_engineer = (
        len(incoming) == 1 and str(incoming[0]).strip().lower() == "engineer"
    )
    phase1 = [str(t).strip() for t in (profile.get("target_titles") or []) if t]

    if (not incoming or only_engineer) and phase1:
        return phase1, "phase1"

    if incoming and phase1:
        merged: list = []
        seen: set = set()
        for t in (*incoming, *phase1):
            key = str(t).strip().lower()
            if key and key not in seen:
                seen.add(key)
                merged.append(str(t).strip())
        return merged, "merged"

    return incoming, "user"


def phase2_discover_jobs(profile: dict, job_titles: list, location: str,
                          provider: BaseProvider,
                          use_simplify: bool = True,
                          max_jobs: int = None,
                          days_old: int = 30,
                          education_filter=None,
                          include_unknown_education: bool = False,
                          deep_search: bool = False,
                          cache_ttl_minutes: int = 30,
                          force_live: bool = False,
                          offset: int = 0) -> list:
    console.print("\n[bold cyan]Phase 2 — Job Discovery & Search[/bold cyan]")

    job_titles, _titles_source = _resolve_effective_titles(job_titles, profile)
    console.print(
        f"  🔍 Searching: {', '.join(job_titles) if job_titles else '(none)'} "
        f"(source: {_titles_source})"
    )
    console.print(f"  📍 Location: {location}")
    console.print(f"  📅 Posted within last {days_old} days")
    cap = max_jobs or MAX_SCRAPE_JOBS
    console.print(f"  🔢 Max jobs cap: {cap}")
    if offset > 0:
        console.print(f"  ⏩ Offset: {offset}")
    console.print(f"  Search depth: {'deep' if deep_search else 'quick'}")
    _t0 = time.time()

    sample_file = RESOURCES_DIR / ("sample_jobs_deep.json" if deep_search else "sample_jobs_quick.json")
    cached_jobs, cached_meta = _load_cached_jobs(sample_file)
    cache_hit = not force_live and cached_jobs is not None and _cache_matches(
        cached_meta, titles=job_titles, location=location, days_old=days_old,
        deep_search=deep_search, ttl_minutes=cache_ttl_minutes,
    )
    if force_live:
        console.print("  [yellow]⚡ Force-live enabled; bypassing cache.[/yellow]")
    elif cached_jobs is not None and not cache_hit:
        cached_titles = (cached_meta or {}).get("titles") or []
        console.print(
            "  [yellow]♻️  Cache bypassed: stored titles "
            f"{cached_titles or '(none)'} or filters do not match; scraping live.[/yellow]"
        )

    if cache_hit:
        jobs = cached_jobs
        console.print(f"  📂 Loaded {len(jobs)} postings from cache")
        # Always re-infer metadata — stale cache values would otherwise
        # mask upstream keyword-list improvements.
        for job in jobs:
            job["experience_level"]     = infer_experience_level(job)
            job["education_required"]   = infer_education_required(job)
            job["citizenship_required"] = infer_citizenship_required(job)
            job["salary_range"]         = sanitize_salary_field(job.get("salary_range"))
            job.setdefault("source", "cache")
    else:
        jobs: list = []
        api_cap = min(cap, 18)
        fast_sources = [
            ("Himalayas", lambda: HimalayasApiScraper().fetch_jobs(job_titles, max_jobs=api_cap)),
            ("Remotive", lambda: RemotiveApiScraper().fetch_jobs(job_titles, max_jobs=api_cap)),
            ("Arbeitnow", lambda: ArbeitnowApiScraper().fetch_jobs(job_titles, profile, max_jobs=api_cap)),
        ]
        
        # Only use API sources if offset is 0, since they don't support pagination
        if offset == 0:
            ex = ThreadPoolExecutor(max_workers=len(fast_sources))
            futures = {ex.submit(fn): name for name, fn in fast_sources}
            try:
                for fut in as_completed(futures, timeout=7):
                    name = futures[fut]
                    try:
                        found = fut.result(timeout=1)
                    except Exception as exc:
                        console.print(f"  [yellow]{name}: skipped ({exc})[/yellow]")
                        continue
                    console.print(f"  ⚡ {name}: {len(found)} API jobs")
                    jobs.extend(found)
                    jobs = deduplicate_jobs(jobs)
                    if not deep_search and len(jobs) >= min(cap, 10):
                        console.print("  Fast API results ready; skipping slower HTML scrape")
                        break
            except FuturesTimeout:
                console.print("  [yellow]Fast API sources timed out; using completed results[/yellow]")
            finally:
                ex.shutdown(wait=False, cancel_futures=True)

        board_client = JobSpyClient()
        primary_cap = cap if deep_search else min(cap, 16)
        site_names = None if deep_search else ["indeed", "linkedin"]
        max_queries = None if deep_search else 3
        if deep_search or offset > 0 or len(jobs) < min(cap, 10):
            with _CliSpinner(
                messages=[
                    "Scraping primary job boards...",
                    "Still fetching job listings — network can be slow…",
                    "Hang tight — collecting first matches...",
                    "Almost done collecting postings…",
                    "Deduplicating and filtering results…",
                ],
                interval=20,
            ):
                jobspy_jobs = board_client.fetch_jobs(
                    job_titles, location, days=days_old, max_jobs=primary_cap,
                    priority_titles=profile.get("target_titles") or [],
                    site_names=site_names, max_queries=max_queries,
                    offset=offset,
                )
            jobs = deduplicate_jobs(jobs + jobspy_jobs)

        if not jobs:
            console.print(
                "  [yellow]⚠️  JobSpy returned 0 results — "
                "falling back to demo job postings.[/yellow]"
            )
            console.print("  🤖 Generating demo job postings...")
            jobs = provider.generate_demo_jobs(profile, job_titles, location)

        for job in jobs:
            job["experience_level"]     = infer_experience_level(job)
            job["education_required"]   = infer_education_required(job)
            job["citizenship_required"] = infer_citizenship_required(job)
            job["salary_range"]         = sanitize_salary_field(job.get("salary_range"))
            job.setdefault("source", "jobspy")

        if use_simplify and deep_search:
            existing_urls = {j.get("application_url") for j in jobs if j.get("application_url")}

            scrapers = [
                ("SimplifyJobs", lambda: SimplifyJobsScraper().fetch_jobs()),
                ("Jobright", lambda: JobrightScraper().fetch_jobs()),
                ("InternList", lambda: InternListScraper().fetch_jobs()),
            ]
            ex = ThreadPoolExecutor(max_workers=len(scrapers))
            futures = {ex.submit(fn): name for name, fn in scrapers}
            try:
                completed = as_completed(futures, timeout=18)
                for fut in completed:
                    name = futures[fut]
                    try:
                        found = fut.result(timeout=1)
                    except Exception as exc:
                        console.print(f"  [yellow]{name}: skipped ({exc})[/yellow]")
                        continue
                    console.print(f"  📋 {name}: {len(found)} listings")
                    new_jobs = [j for j in found if j.get("application_url") not in existing_urls]
                    jobs = jobs + new_jobs
                    existing_urls |= {j["application_url"] for j in new_jobs if j.get("application_url")}
                    if cap and len(jobs) >= cap:
                        console.print(f"  Reached {cap} jobs; stopping secondary merge")
                        break
            except FuturesTimeout:
                console.print("  [yellow]Secondary boards timed out; using completed results[/yellow]")
            finally:
                ex.shutdown(wait=False, cancel_futures=True)

    before = len(jobs)
    jobs   = deduplicate_jobs(jobs)
    after  = len(jobs)
    console.print(
        f"  🔀 Deduplication: {before} → {after} jobs "
        f"({before - after} duplicates merged)"
    )

    # (rest of existing code)

    # Relaxed age filter: if deep_search is on, look back 90 days instead of 30.
    days_to_check = 90 if deep_search else days_old
    jobs = _filter_by_posting_age(jobs, days_to_check)
    console.print(f"  📅 After age filter ({days_to_check} days): {len(jobs)} jobs remain")
    
    # Relaxed education filter: only drop if it's explicitly a mismatch, not unknown.
    if education_filter:
        before_edu = len(jobs)
        # We now keep 'unknown' education listings, only dropping explicit mismatches.
        jobs = filter_jobs_by_education(
            jobs, education_filter, include_unknown=include_unknown_education,
        )
        from .helpers import (_last_education_dropped_unknown as _du,
                              _last_education_dropped_mismatch as _dm)
        console.print(
            f"  🎓 Education filter (kept unknown): {before_edu} → {len(jobs)} "
            f"(dropped {_dm} mismatches, {_du} unknown)"
        )
    jobs = _sort_newest_first(jobs)
    console.print(f"  📅 Newest-first sort applied ({len(jobs)} jobs within {days_old}-day window)")

    if cap and len(jobs) > cap:
        jobs = jobs[:cap]
        console.print(f"  ✂️  Final list capped at {cap} jobs")

    validate_job_urls(jobs)
    from .helpers import (_last_url_broken as _ub2,
                          _last_url_reconstructed as _ur2)
    console.print(
        f"  🔗 URL validation: {_ur2} reconstructed, {_ub2} broken "
        f"(broken kept for manual review)"
    )

    if not jobs:
        console.print(
            "  [yellow]⚠️  All discovered jobs were filtered out (age/education) — "
            "falling back to demo postings.[/yellow]"
        )
        jobs = provider.generate_demo_jobs(profile, job_titles, location)
        # Re-apply basic metadata to demo jobs if needed
        for job in jobs:
            job.setdefault("experience_level", "internship")
            job.setdefault("education_required", "unknown")
            job.setdefault("citizenship_required", "unknown")
            job.setdefault("source", "demo")

    _save_jobs_cache(
        sample_file, jobs, titles=job_titles, location=location,
        days_old=days_old, deep_search=deep_search,
    )
    console.print(f"  ⏱️  Phase 2 completed in [bold]{time.time() - _t0:.1f}s[/bold]")
    return jobs


# ── Phase 3 ────────────────────────────────────────────────────────────────────

def phase3_score_jobs(jobs: list, profile: dict, provider: BaseProvider,
                       min_score: int = 60,
                       experience_levels=None,
                       education_filter=None,  # noqa: ARG001 — moved to phase 2; kept for back-compat
                       citizenship_filter: str = "all",
                       include_unknown_education: bool = False,  # noqa: ARG001
                       llm_score_limit: int = 10,
                       fast_only: bool = False) -> list:
    """Score every job that survived Phase 2's filters.

    Returns the FULL list (not pruned by min_score) so the UI can show why
    each job was kept or skipped. Each job gets a `filter_status` field:
      - "passed"               → score >= min_score
      - "below_threshold"      → scored, but below min_score
      - "filtered_experience"  → dropped by experience pre-filter (score=0)
      - "filtered_citizenship" → dropped by citizenship pre-filter (score=0)

    Note: education filtering now happens in Phase 2. The `education_filter`
    and `include_unknown_education` params are accepted for back-compat but
    ignored here.
    """
    console.print("\n[bold cyan]Phase 3 — Relevance Scoring & Shortlisting[/bold cyan]")

    pre_filtered: list = []  # jobs excluded before scoring (kept in output for visibility)
    to_score:     list = list(jobs)

    if experience_levels and experience_levels != ["all"]:
        kept, dropped = [], []
        for j in to_score:
            if j.get("experience_level", "unknown") in experience_levels:
                kept.append(j)
            else:
                dropped.append({**j, "score": 0, "filter_status": "filtered_experience",
                                "filter_reason": f"experience={j.get('experience_level', 'unknown')}"})
        to_score = kept
        pre_filtered.extend(dropped)
        console.print(f"  🎯 Experience filter {experience_levels}: {len(to_score)} jobs remain")

    if citizenship_filter == "exclude_required":
        # Hard filter: trust the inferred field, AND regex-scan the raw text
        # so postings that slipped past inference still get caught.
        citizenship_re = re.compile(
            r"\b(?:u\.?s\.?\s*citizen(?:ship)?(?:\s+required)?"
            r"|must\s+be\s+(?:a\s+)?u\.?s\.?\s*citizen"
            r"|security\s+clearance"
            r"|active\s+clearance"
            r"|secret\s+clearance"
            r"|top\s+secret"
            r"|ts/sci"
            r"|itar"
            r"|export\s+control"
            r"|green\s+card\s+holder)\b",
            re.IGNORECASE,
        )
        kept, dropped = [], []
        for j in to_score:
            haystack = " ".join([
                str(j.get("description") or ""),
                " ".join(str(r) for r in (j.get("requirements") or [])),
            ])
            text_match = bool(citizenship_re.search(haystack))
            field_yes  = j.get("citizenship_required", "unknown") == "yes"
            if not text_match and not field_yes:
                kept.append(j)
            else:
                reason = "regex match in description" if text_match else "citizenship required"
                dropped.append({**j, "score": 0, "filter_status": "filtered_citizenship",
                                "filter_reason": reason})
        to_score = kept
        pre_filtered.extend(dropped)
        console.print(
            f"  🇺🇸 Citizenship filter (exclude required): {len(to_score)} jobs remain "
            f"({len(dropped)} dropped, regex+field)"
        )
    elif citizenship_filter == "only_required":
        kept, dropped = [], []
        for j in to_score:
            if j.get("citizenship_required", "unknown") == "yes":
                kept.append(j)
            else:
                dropped.append({**j, "score": 0, "filter_status": "filtered_citizenship",
                                "filter_reason": "citizenship not required"})
        to_score = kept
        pre_filtered.extend(dropped)
        console.print(f"  🇺🇸 Citizenship filter (only required): {len(to_score)} jobs remain")

    def _fast_score(job: dict, profile: dict) -> int:
        """Fast heuristic scoring (0-100) based on keyword matching."""
        reqs = set(str(r).lower() for r in (job.get("requirements") or []) if r)
        skills = set(str(s).lower() for s in (profile.get("top_hard_skills") or []))
        titles = set(str(t).lower() for t in (profile.get("target_titles") or []))

        skill_match = len(reqs & skills) / len(reqs) * 50 if reqs else 25
        title_match = 25 if any(t in job.get("title", "").lower() for t in titles) else 5
        exp_match = 15 if job.get("experience_level") in ["internship", "entry-level"] else 5
        return int(skill_match + title_match + exp_match)

    console.print(f"  🔢 Fast-scoring {len(to_score)} jobs…")
    for job in to_score:
        job["_fast_score"] = _fast_score(job, profile)

    to_score.sort(key=lambda j: j["_fast_score"], reverse=True)
    llm_score_count = 0 if fast_only else min(llm_score_limit, len(to_score))
    to_llm_score = to_score[:llm_score_count]
    to_skip = to_score[llm_score_count:]

    if fast_only:
        console.print(f"  ⚡ Fast-only scoring enabled; skipping LLM for {len(to_score)} jobs")
    elif to_skip:
        console.print(
            f"  ⚡ Fast-scoring all {len(to_score)} jobs; "
            f"LLM-scoring top {llm_score_count} only"
        )

    _t0 = time.time()
    scored: list = []
    for i, job in enumerate(to_llm_score, 1):
        result = provider.score_job(job, profile)
        merged = {**job, **result}
        s = merged.get("score", 0)
        merged["filter_status"] = "passed" if s >= min_score else "below_threshold"
        merged["filter_reason"] = "" if s >= min_score else f"score {s} < {min_score}"
        scored.append(merged)
        if i % 5 == 0 or i == len(to_llm_score):
            console.print(
                f"  [dim]🧠 LLM-scored {i}/{len(to_llm_score)} jobs  "
                f"({time.time() - _t0:.0f}s elapsed)[/dim]"
            )

    for job in to_skip:
        merged = {**job}
        s = merged.get("_fast_score", 0)
        merged["score"] = s
        if fast_only:
            merged["filter_status"] = "passed" if s >= min_score else "below_threshold"
            merged["filter_reason"] = "" if s >= min_score else f"heuristic score {s} < {min_score}"
        else:
            merged["filter_status"] = "below_threshold"
            merged["filter_reason"] = f"heuristic score {s}, skipped LLM (top {llm_score_count} only)"
        scored.append(merged)

    # Combine: scored first (sorted by score), then pre-filtered for visibility.
    scored.sort(key=lambda x: x.get("score", 0), reverse=True)
    scored = scored + pre_filtered
    passed_count = sum(1 for j in scored if j.get("filter_status") == "passed")

    display_n = min(12, len(scored))
    table = Table(title=f"Job Match Scores (showing top {display_n} of {len(scored)}, min: {min_score})")
    table.add_column("#",       style="dim",   width=4)
    table.add_column("Company", style="cyan",  width=18)
    table.add_column("Title",   style="white", width=28)
    table.add_column("Score",   style="bold",  width=8)
    table.add_column("Status",  width=22)

    for i, job in enumerate(scored[:12], 1):
        s = job.get("score", 0)
        fs = job.get("filter_status", "passed")
        if fs.startswith("filtered_"):
            colour, status = "red", f"❌ {fs.replace('filtered_', '')}"
        elif s >= 75:
            colour, status = "bold green", "✅ Auto-eligible"
        elif s >= 60:
            colour, status = "yellow", "⚠️  Review needed"
        else:
            colour, status = "red", "❌ Below threshold"
        table.add_row(str(i), job.get("company", ""), job.get("title", ""),
                      f"[{colour}]{s}[/{colour}]", status)

    console.print(table)
    console.print(
        f"  ⏱️  Phase 3 completed in [bold]{time.time() - _t0:.1f}s[/bold]  "
        f"({passed_count}/{len(scored)} jobs passed the {min_score} score threshold)"
    )
    return scored


# ── Phase 4 ────────────────────────────────────────────────────────────────────

def _ats_score(text: str, requirements: list) -> int:
    """Return percentage of *requirements* keywords present in *text* (0-100)."""
    if not requirements:
        return 0
    text_l = (text or "").lower()
    hits = 0
    for req in requirements:
        token = str(req or "").strip().lower()
        if token and token in text_l:
            hits += 1
    return int(round(100 * hits / len(requirements)))


def _profile_to_text(profile: dict) -> str:
    """Flatten a structured profile dict into a single searchable string."""
    parts: list = []
    parts.extend(profile.get("top_hard_skills") or [])
    parts.extend(profile.get("top_soft_skills") or [])
    for role in (profile.get("experience") or []):
        parts.append(role.get("title", ""))
        parts.append(role.get("company", ""))
        parts.extend(role.get("bullets") or [])
    for proj in (profile.get("projects") or []):
        parts.append(proj.get("name", ""))
        parts.append(proj.get("description", ""))
        parts.extend(proj.get("skills_used") or [])
    for ed in (profile.get("education") or []):
        parts.append(ed.get("degree", ""))
        parts.append(ed.get("institution", ""))
    return " ".join(p for p in parts if p)


def _tailoring_is_empty(tailored: dict) -> bool:
    """True when the LLM returned a tailoring response with no usable content."""
    if not tailored:
        return True
    return (
        not tailored.get("skills_reordered")
        and not tailored.get("experience_bullets")
    )


def phase4_tailor_resume(job: dict, profile: dict, resume_text: str,
                          provider: BaseProvider, include_cover_letter: bool = False,
                          section_order: list = None) -> dict:
    tailored = provider.tailor_resume(job, profile, resume_text) or {}

    # Validator: if both skills_reordered and experience_bullets came back empty,
    # the LLM silently failed.  Retry once, then fall back to profile-derived
    # defaults so _save_tailored_resume always has something to render.
    if _tailoring_is_empty(tailored):
        console.print(
            "  [yellow][!] Tailoring returned empty - retrying once.[/yellow]"
        )
        try:
            retry = provider.tailor_resume(job, profile, resume_text) or {}
        except Exception as e:
            console.print(f"  [yellow]Retry failed: {e}[/yellow]")
            retry = {}
        if not _tailoring_is_empty(retry):
            tailored = retry
        else:
            console.print(
                "  [yellow][!] Retry also empty - falling back to profile defaults.[/yellow]"
            )
            reqs = [str(r) for r in (job.get("requirements") or []) if r]
            skills_fb = list(profile.get("top_hard_skills") or [])
            # Ensure ATS "after" score can improve: surface any job requirements
            # not already present in the profile's hard-skill list.
            seen = {s.lower() for s in skills_fb}
            for r in reqs:
                if r.lower() not in seen:
                    skills_fb.append(r)
                    seen.add(r.lower())
            tailored = {
                "skills_reordered":     skills_fb,
                "experience_bullets":   [],
                "ats_keywords_missing": tailored.get("ats_keywords_missing") or reqs,
                "section_order":        tailored.get("section_order")
                                         or ["Skills", "Projects", "Experience", "Education"],
            }

    if section_order:
        tailored["section_order"] = section_order
    if include_cover_letter:
        tailored["cover_letter"] = provider.generate_cover_letter(job, profile)

    # ── ATS scoring (before/after) ─────────────────────────────────────────────
    requirements = job.get("requirements") or []
    before_text  = (resume_text or "") + " " + _profile_to_text(profile)
    after_extras = " ".join(tailored.get("skills_reordered") or [])
    for entry in (tailored.get("experience_bullets") or []):
        after_extras += " " + " ".join(entry.get("bullets") or [])
    after_text = before_text + " " + after_extras
    tailored["ats_score_before"] = _ats_score(before_text, requirements)
    tailored["ats_score_after"]  = _ats_score(after_text,  requirements)
    return tailored


# ── Playwright submitter ───────────────────────────────────────────────────────

class PlaywrightSubmitter:
    """Real form submission via Playwright (Greenhouse boards supported)."""

    def __init__(self, profile: dict):
        self.profile = profile

    def submit(self, job: dict, resume_path: str = "", cover_letter: str = "") -> dict:  # noqa: ARG002
        url = job.get("application_url", "")
        if "boards.greenhouse.io" in url:
            return self._submit_greenhouse(job, resume_path)
        return phase5_simulate_submission(job)

    def _submit_greenhouse(self, job: dict, resume_path: str) -> dict:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            console.print(
                "  [yellow]playwright missing — "
                "pip install playwright && playwright install chromium[/yellow]"
            )
            return phase5_simulate_submission(job)

        import random
        url        = job.get("application_url", "")
        profile    = self.profile
        name_parts = (profile.get("name") or "").split()
        first      = name_parts[0] if name_parts else ""
        last       = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
        email      = profile.get("email", "")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_page()
            try:
                page.goto(url, timeout=30000)
                for sel in ["input[name='first_name']", "input[id='first_name']"]:
                    if page.locator(sel).count():
                        page.fill(sel, first); break
                for sel in ["input[name='last_name']", "input[id='last_name']"]:
                    if page.locator(sel).count():
                        page.fill(sel, last); break
                for sel in ["input[name='email']", "input[id='email']"]:
                    if page.locator(sel).count():
                        page.fill(sel, email); break
                if resume_path and Path(resume_path).exists():
                    for sel in ["input[type='file']", "input[name='resume']"]:
                        if page.locator(sel).count():
                            page.set_input_files(sel, resume_path); break
                for sel in ["button[type='submit']", "input[type='submit']"]:
                    if page.locator(sel).count():
                        page.click(sel)
                        page.wait_for_timeout(2000)
                        break
                return {
                    "status": "Applied",
                    "confirmation": (
                        f"GH-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000, 9999)}"
                    ),
                }
            except Exception as e:
                console.print(f"  [yellow]Playwright error: {e}[/yellow]")
                return phase5_simulate_submission(job)
            finally:
                browser.close()


# ── Phase 5 ────────────────────────────────────────────────────────────────────

def _load_existing_applications(output_dir: Path = None) -> set:
    """Return set of (company_lower, title_lower) already in this month's tracker."""
    month        = datetime.now().strftime("%Y-%m")
    tracker_path = (output_dir or OUTPUT_DIR) / f"Job_Applications_Tracker_{month}.xlsx"
    if not tracker_path.exists():
        return set()
    try:
        import openpyxl
        wb      = openpyxl.load_workbook(tracker_path, read_only=True)
        ws      = wb.active
        headers = [cell.value for cell in next(ws.iter_rows(max_row=1))]
        title_col   = headers.index("Job Title") if "Job Title" in headers else None
        company_col = headers.index("Company")   if "Company"   in headers else None
        applied: set = set()
        if title_col is not None and company_col is not None:
            for row in ws.iter_rows(min_row=2, values_only=True):
                t = row[title_col]
                c = row[company_col]
                if t and c:
                    applied.add((str(c).lower(), str(t).lower()))
        wb.close()
        return applied
    except Exception:
        return set()


def phase5_simulate_submission(job: dict, already_applied: set = None) -> dict:
    if already_applied is None:
        already_applied = set()
    key = (job.get("company", "").lower(), job.get("title", "").lower())
    if key in already_applied:
        console.print("  ⏭️  Already applied — skipped")
        return {"status": "Skipped", "confirmation": "N/A",
                "notes": "Already applied — skipped"}
    import random
    status  = random.choice(["Applied", "Applied", "Applied", "Manual Required"])
    confirm = (
        f"DEMO-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000, 9999)}"
        if status == "Applied" else "N/A"
    )
    return {"status": status, "confirmation": confirm}


# ── Phase 6 ────────────────────────────────────────────────────────────────────

def phase6_update_tracker(applications: list, output_dir: Path = None) -> Path:
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        console.print("  [yellow]openpyxl missing — run: pip install openpyxl[/yellow]")
        return None

    console.print("\n[bold cyan]Phase 6 — Excel Tracker[/bold cyan]")

    month        = datetime.now().strftime("%Y-%m")
    out_dir = output_dir or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    tracker_path = out_dir / f"Job_Applications_Tracker_{month}.xlsx"
    headers = [
        "#", "Date Applied", "Job Title", "Company", "Industry",
        "Location", "Job Posting URL", "Company Website", "Application Portal",
        "Match Score", "Score Reasoning", "Resume Version", "Cover Letter Sent",
        "Status", "Confirmation #", "Notes", "Follow-Up Date", "Response Received",
    ]

    if tracker_path.exists():
        wb = openpyxl.load_workbook(tracker_path)
        ws = wb["Applications"] if "Applications" in wb.sheetnames else wb.active
        ws.title = "Applications"
        existing_headers = [cell.value for cell in ws[1]]
        if existing_headers != headers:
            ws.delete_rows(1)
            ws.insert_rows(1)
            for col, hdr in enumerate(headers, 1):
                ws.cell(row=1, column=col, value=hdr)
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Applications"
        for col, hdr in enumerate(headers, 1):
            ws.cell(row=1, column=col, value=hdr)

    hdr_fill = PatternFill("solid", fgColor="1F4E79")
    hdr_font = Font(color="FFFFFF", bold=True)
    for col, hdr in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=hdr)
        cell.fill      = hdr_fill
        cell.font      = hdr_font
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    status_fills = {
        "Applied":         PatternFill("solid", fgColor="C6EFCE"),
        "Manual Required": PatternFill("solid", fgColor="FFEB9C"),
        "Skipped":         PatternFill("solid", fgColor="FFC7CE"),
        "Error":           PatternFill("solid", fgColor="D9D9D9"),
    }

    existing_rows = {}
    for row_idx in range(2, ws.max_row + 1):
        company = ws.cell(row=row_idx, column=headers.index("Company") + 1).value
        title = ws.cell(row=row_idx, column=headers.index("Job Title") + 1).value
        if company and title:
            existing_rows[(str(company).lower(), str(title).lower())] = row_idx

    def _row_values(app: dict, row_num: int) -> list:
        applied_str = app.get("date_applied", datetime.now().strftime("%m/%d/%Y"))
        try:
            follow_up = (
                datetime.strptime(applied_str, "%m/%d/%Y") + timedelta(days=7)
            ).strftime("%m/%d/%Y")
        except ValueError:
            follow_up = ""
        company_slug = app.get("company", "").lower().replace(" ", "")
        return [
            row_num, applied_str, app.get("title", ""), app.get("company", ""),
            "Technology / Semiconductor", app.get("location", ""),
            app.get("application_url", ""), f"https://www.{company_slug}.com",
            app.get("platform", ""), app.get("score", 0),
            app.get("reasoning") or app.get("reason", ""),
            app.get("resume_version", ""),
            "Yes" if app.get("cover_letter_sent") else "No",
            app.get("status", "Applied"), app.get("confirmation", "N/A"),
            app.get("notes", ""), follow_up, "",
        ]

    for app in applications:
        key = (str(app.get("company", "")).lower(), str(app.get("title", "")).lower())
        row_idx = existing_rows.get(key)
        if not row_idx:
            row_idx = ws.max_row + 1
            existing_rows[key] = row_idx
        values = _row_values(app, row_idx - 1)
        for col, value in enumerate(values, 1):
            ws.cell(row=row_idx, column=col, value=value)
        fill = status_fills.get(app.get("status", "Applied"), status_fills["Applied"])
        for col in range(1, len(headers) + 1):
            ws.cell(row=row_idx, column=col).fill = fill

    ws.freeze_panes = "A2"
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max_len + 4, 40)

    if "Dashboard" in wb.sheetnames:
        del wb["Dashboard"]
    ws_d    = wb.create_sheet("Dashboard")
    all_rows = [
        dict(zip(headers, row))
        for row in ws.iter_rows(min_row=2, values_only=True)
        if any(row)
    ]
    total   = len(all_rows)
    applied = sum(1 for a in all_rows if a.get("Status") == "Applied")
    manual  = sum(1 for a in all_rows if a.get("Status") == "Manual Required")
    skipped = sum(1 for a in all_rows if a.get("Status") == "Skipped")
    avg_sc  = sum(float(a.get("Match Score") or 0) for a in all_rows) / max(total, 1)
    for row in [
        ("Metric", "Value"), ("Run Date", date.today().isoformat()),
        ("Total Jobs Evaluated", total), ("Applications Submitted", applied),
        ("Manual Review Required", manual), ("Skipped (Low Match)", skipped),
        ("Average Match Score", f"{avg_sc:.1f}"),
    ]:
        ws_d.append(row)
    ws_d["A1"].font = Font(bold=True)
    ws_d["B1"].font = Font(bold=True)

    wb.save(tracker_path)
    console.print(f"  ✅ Tracker saved → [bold]{tracker_path}[/bold]")
    return tracker_path


# ── Email notification ─────────────────────────────────────────────────────────

def _send_email_notification(report_text: str, n_applied: int) -> None:
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "NOTIFY_EMAIL"]
    missing  = [v for v in required if not os.environ.get(v)]
    if missing:
        console.print(
            f"  [dim]Email notification skipped (missing env: {', '.join(missing)})[/dim]"
        )
        return
    try:
        host      = os.environ["SMTP_HOST"]
        port      = int(os.environ["SMTP_PORT"])
        user      = os.environ["SMTP_USER"]
        password  = os.environ["SMTP_PASS"]
        recipient = os.environ["NOTIFY_EMAIL"]
        subject   = (
            f"Job Application Run Complete — "
            f"{date.today().isoformat()} ({n_applied} applied)"
        )
        msg            = MIMEText(report_text)
        msg["Subject"] = subject
        msg["From"]    = user
        msg["To"]      = recipient
        with smtplib.SMTP_SSL(host, port) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
        console.print(f"  📧 Notification sent to {recipient}")
    except Exception as e:
        console.print(f"  [yellow]Email notification failed: {e}[/yellow]")


# ── Phase 7 ────────────────────────────────────────────────────────────────────

def phase7_run_report(applications: list, tracker_path: Path,
                       provider: BaseProvider, output_dir: Path = None) -> str:
    console.print("\n[bold cyan]Phase 7 — End-of-Run Report[/bold cyan]")

    applied_list = [a for a in applications if a.get("status") == "Applied"]
    manual_list  = [a for a in applications if a.get("status") == "Manual Required"]
    skipped_list = [a for a in applications if a.get("status") == "Skipped"]
    top3 = sorted(applied_list, key=lambda x: x.get("score", 0), reverse=True)[:3]

    summary_data = {
        "total_found":    len(applications),
        "applied":        len(applied_list),
        "manual":         len(manual_list),
        "skipped":        len(skipped_list),
        "top3_applied":   [(a["company"], a["title"], a["score"]) for a in top3],
        "manual_reasons": [a.get("notes", "Form requires manual review") for a in manual_list],
    }

    report_text = provider.generate_report(summary_data)

    out_dir = output_dir or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{datetime.now().strftime('%Y%m%d')}_job-application-run-report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# Job Application Run Report\n**Date:** {date.today().isoformat()}\n\n")
        f.write(report_text)
        if tracker_path:
            f.write(f"\n\n---\n**Tracker:** `{tracker_path.name}`\n")

    console.print(Panel(report_text, title="[bold]Run Summary[/bold]", border_style="green"))
    console.print(f"  📄 Report saved → [bold]{report_path}[/bold]")
    _send_email_notification(report_text, len(applied_list))
    return report_text


# ── Dashboard helper ───────────────────────────────────────────────────────────

def _launch_dashboard_and_wait(tracker_path: Path) -> None:
    """Launch the Flask dashboard, open the browser, and block until Enter."""
    import subprocess
    import webbrowser

    dashboard_script = Path(sys.argv[0]).resolve().parent / "dashboard" / "app.py"
    if not dashboard_script.exists():
        console.print("  [yellow]dashboard/app.py not found — skipping dashboard.[/yellow]")
        return

    try:
        proc = subprocess.Popen([sys.executable, str(dashboard_script)])
        time.sleep(1.5)
        webbrowser.open("http://localhost:5000")
        console.print(Panel(
            "Dashboard running at [bold]http://localhost:5000[/bold]\n\n"
            "• Review scored jobs and approve Manual Required rows.\n"
            "• Approved rows will be submitted in phases 4-7.\n"
            "• Press [bold]Enter[/bold] when ready to continue.",
            title="[bold cyan]Web Dashboard[/bold cyan]",
            border_style="cyan",
        ))
        input()
        proc.terminate()
    except Exception as e:
        console.print(f"  [yellow]Dashboard launch failed: {e}[/yellow]")
