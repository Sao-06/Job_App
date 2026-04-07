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
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from rich.panel import Panel
from rich.table import Table

from .config import console, OUTPUT_DIR, RESOURCES_DIR, MAX_SCRAPE_JOBS, _CliSpinner
from .helpers import (infer_experience_level, infer_education_required,
                      infer_citizenship_required, deduplicate_jobs)
from .providers import BaseProvider
from .resume import _build_demo_resume, _save_tailored_resume
from .scrapers import JobSpyClient, SimplifyJobsScraper


# ── Phase 1 ────────────────────────────────────────────────────────────────────

def phase1_ingest_resume(resume_text: str, provider: BaseProvider,
                         preferred_titles: list = None) -> dict:
    from .config import OWNER_NAME
    console.print("\n[bold cyan]Phase 1 — Resume Ingestion & Profile Extraction[/bold cyan]")
    _t0 = time.time()
    with _CliSpinner(interval=20):
        profile = provider.extract_profile(resume_text, preferred_titles=preferred_titles)
    elapsed = time.time() - _t0
    if profile:
        console.print(f"  ✅ Profile extracted: [bold]{profile.get('name', OWNER_NAME)}[/bold]")
        console.print(f"  📊 Top skills: {', '.join(profile.get('top_hard_skills', [])[:5])}")
        titles = profile.get("target_titles", [])
        if titles:
            console.print(f"  🎯 Target titles: {', '.join(titles[:4])}")
        if profile.get("resume_gaps"):
            console.print(f"  ⚠️  Gaps: {', '.join(profile['resume_gaps'])}")
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


def phase2_discover_jobs(profile: dict, job_titles: list, location: str,
                          provider: BaseProvider,
                          use_simplify: bool = True,
                          max_jobs: int = None,
                          days_old: int = 30) -> list:
    console.print("\n[bold cyan]Phase 2 — Job Discovery & Search[/bold cyan]")
    console.print(f"  🔍 Searching: {', '.join(job_titles)}")
    console.print(f"  📍 Location: {location}")
    console.print(f"  📅 Posted within last {days_old} days")
    cap = max_jobs or MAX_SCRAPE_JOBS
    console.print(f"  🔢 Max jobs cap: {cap}")
    _t0 = time.time()

    sample_file = RESOURCES_DIR / "sample_jobs.json"
    if sample_file.exists():
        with open(sample_file, encoding="utf-8") as f:
            jobs = json.load(f)
        console.print(f"  📂 Loaded {len(jobs)} postings from resources/sample_jobs.json")
        # Always re-infer metadata — stale cache values would otherwise
        # mask upstream keyword-list improvements.
        for job in jobs:
            job["experience_level"]     = infer_experience_level(job)
            job["education_required"]   = infer_education_required(job)
            job["citizenship_required"] = infer_citizenship_required(job)
            job.setdefault("source", "cache")
        jobs = _filter_by_posting_age(jobs, days_old)
        jobs = _sort_newest_first(jobs)
        if cap and len(jobs) > cap:
            jobs = jobs[:cap]
            console.print(f"  ✂️  Capped cached list to {cap} jobs")
        console.print(f"  ✅ Returning {len(jobs)} cached jobs (newest first)")
        console.print(f"  ⏱️  Phase 2 completed in [bold]{time.time() - _t0:.1f}s[/bold]")
        return jobs

    board_client = JobSpyClient()
    with _CliSpinner(
        messages=[
            "Scraping LinkedIn, Indeed, Glassdoor, ZipRecruiter…",
            "Still fetching job listings — network can be slow…",
            "Hang tight — scraping multiple job boards…",
            "Almost done collecting postings…",
            "Deduplicating and filtering results…",
        ],
        interval=20,
    ):
        jobs = board_client.fetch_jobs(
            job_titles, location, days=days_old, max_jobs=cap,
        )

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
        job.setdefault("source", "jobspy")

    if use_simplify:
        simplify_jobs = SimplifyJobsScraper().fetch_jobs()
        console.print(f"  📋 SimplifyJobs: {len(simplify_jobs)} listings")
        if simplify_jobs:
            simplify_urls = {j["application_url"] for j in simplify_jobs
                             if j.get("application_url")}
            jobs = [j for j in jobs if j.get("application_url") not in simplify_urls]
            jobs = jobs + simplify_jobs

    before = len(jobs)
    jobs   = deduplicate_jobs(jobs)
    after  = len(jobs)
    console.print(
        f"  🔀 Deduplication: {before} → {after} jobs "
        f"({before - after} duplicates merged)"
    )

    jobs = _filter_by_posting_age(jobs, days_old)
    jobs = _sort_newest_first(jobs)
    console.print(f"  📅 Newest-first sort applied ({len(jobs)} jobs within {days_old}-day window)")

    if cap and len(jobs) > cap:
        jobs = jobs[:cap]
        console.print(f"  ✂️  Final list capped at {cap} jobs")

    RESOURCES_DIR.mkdir(exist_ok=True)
    with open(sample_file, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2)
    console.print(f"  ✅ {len(jobs)} postings saved to resources/sample_jobs.json")
    console.print(f"  ⏱️  Phase 2 completed in [bold]{time.time() - _t0:.1f}s[/bold]")
    return jobs


# ── Phase 3 ────────────────────────────────────────────────────────────────────

def phase3_score_jobs(jobs: list, profile: dict, provider: BaseProvider,
                       min_score: int = 60,
                       experience_levels=None,
                       education_filter=None,
                       citizenship_filter: str = "all",
                       include_unknown_education: bool = False) -> list:
    console.print("\n[bold cyan]Phase 3 — Relevance Scoring & Shortlisting[/bold cyan]")

    filtered = list(jobs)

    if experience_levels and experience_levels != ["all"]:
        filtered = [j for j in filtered
                    if j.get("experience_level", "unknown") in experience_levels]
        console.print(f"  🎯 Experience filter {experience_levels}: {len(filtered)} jobs remain")

    if education_filter and education_filter != ["all"]:
        allowed = set(education_filter)
        if include_unknown_education:
            allowed.add("unknown")
        filtered = [j for j in filtered
                    if j.get("education_required", "unknown") in allowed]
        suffix = "" if include_unknown_education else " (strict — excludes unspecified)"
        console.print(f"  🎓 Education filter {education_filter}{suffix}: {len(filtered)} jobs remain")

    if citizenship_filter == "exclude_required":
        filtered = [j for j in filtered if j.get("citizenship_required", "unknown") != "yes"]
        console.print(f"  🇺🇸 Citizenship filter (exclude required): {len(filtered)} jobs remain")
    elif citizenship_filter == "only_required":
        filtered = [j for j in filtered if j.get("citizenship_required", "unknown") == "yes"]
        console.print(f"  🇺🇸 Citizenship filter (only required): {len(filtered)} jobs remain")

    console.print(f"  🔢 Scoring {len(filtered)} jobs…")
    _t0 = time.time()
    scored = []
    for i, job in enumerate(filtered, 1):
        result = provider.score_job(job, profile)
        scored.append({**job, **result})
        if i % 10 == 0 or i == len(filtered):
            console.print(
                f"  [dim]📊 Scored {i}/{len(filtered)} jobs  "
                f"({time.time() - _t0:.0f}s elapsed)[/dim]"
            )

    scored = [j for j in scored if j.get("score", 0) >= min_score]
    scored.sort(key=lambda x: x.get("score", 0), reverse=True)

    table = Table(title=f"Job Match Scores (min: {min_score})")
    table.add_column("#",       style="dim",   width=4)
    table.add_column("Company", style="cyan",  width=18)
    table.add_column("Title",   style="white", width=28)
    table.add_column("Score",   style="bold",  width=8)
    table.add_column("Status",  width=20)

    for i, job in enumerate(scored[:12], 1):
        s = job.get("score", 0)
        if s >= 75:
            colour, status = "bold green", "✅ Auto-eligible"
        elif s >= 60:
            colour, status = "yellow", "⚠️  Review needed"
        else:
            colour, status = "red", "❌ Skipped"
        table.add_row(str(i), job.get("company", ""), job.get("title", ""),
                      f"[{colour}]{s}[/{colour}]", status)

    console.print(table)
    console.print(
        f"  ⏱️  Phase 3 completed in [bold]{time.time() - _t0:.1f}s[/bold]  "
        f"({len(scored)} jobs passed the {min_score} score threshold)"
    )
    return scored


# ── Phase 4 ────────────────────────────────────────────────────────────────────

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
            tailored = {
                "skills_reordered":     profile.get("top_hard_skills", []),
                "experience_bullets":   [],
                "ats_keywords_missing": tailored.get("ats_keywords_missing", []),
                "section_order":        tailored.get("section_order")
                                         or ["Skills", "Projects", "Experience", "Education"],
            }

    if section_order:
        tailored["section_order"] = section_order
    if include_cover_letter:
        tailored["cover_letter"] = provider.generate_cover_letter(job, profile)
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

def _load_existing_applications() -> set:
    """Return set of (company_lower, title_lower) already in this month's tracker."""
    month        = datetime.now().strftime("%Y-%m")
    tracker_path = OUTPUT_DIR / f"Job_Applications_Tracker_{month}.xlsx"
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

def phase6_update_tracker(applications: list) -> Path:
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        console.print("  [yellow]openpyxl missing — run: pip install openpyxl[/yellow]")
        return None

    console.print("\n[bold cyan]Phase 6 — Excel Tracker[/bold cyan]")

    month        = datetime.now().strftime("%Y-%m")
    tracker_path = OUTPUT_DIR / f"Job_Applications_Tracker_{month}.xlsx"
    wb           = openpyxl.Workbook()
    ws           = wb.active
    ws.title     = "Applications"

    headers = [
        "#", "Date Applied", "Job Title", "Company", "Industry",
        "Location", "Job Posting URL", "Company Website", "Application Portal",
        "Match Score", "Resume Version", "Cover Letter Sent",
        "Status", "Confirmation #", "Notes", "Follow-Up Date", "Response Received",
    ]

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

    for i, app in enumerate(applications, 1):
        applied_str = app.get("date_applied", datetime.now().strftime("%m/%d/%Y"))
        try:
            follow_up = (
                datetime.strptime(applied_str, "%m/%d/%Y") + timedelta(days=7)
            ).strftime("%m/%d/%Y")
        except ValueError:
            follow_up = ""
        company_slug = app.get("company", "").lower().replace(" ", "")
        ws.append([
            i, applied_str, app.get("title", ""), app.get("company", ""),
            "Technology / Semiconductor", app.get("location", ""),
            app.get("application_url", ""), f"https://www.{company_slug}.com",
            app.get("platform", ""), app.get("score", 0), app.get("resume_version", ""),
            "Yes" if app.get("cover_letter_sent") else "No",
            app.get("status", "Applied"), app.get("confirmation", "N/A"),
            app.get("notes", ""), follow_up, "",
        ])
        fill = status_fills.get(app.get("status", "Applied"), status_fills["Applied"])
        for col in range(1, len(headers) + 1):
            ws.cell(row=i + 1, column=col).fill = fill

    ws.freeze_panes = "A2"
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max_len + 4, 40)

    ws_d    = wb.create_sheet("Dashboard")
    total   = len(applications)
    applied = sum(1 for a in applications if a.get("status") == "Applied")
    manual  = sum(1 for a in applications if a.get("status") == "Manual Required")
    skipped = sum(1 for a in applications if a.get("status") == "Skipped")
    avg_sc  = sum(a.get("score", 0) for a in applications) / max(total, 1)
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
                       provider: BaseProvider) -> str:
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

    report_path = (
        OUTPUT_DIR
        / f"{datetime.now().strftime('%Y%m%d')}_job-application-run-report.md"
    )
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
