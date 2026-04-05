#!/usr/bin/env python3
"""
agent.py — entry point and CLI orchestrator
────────────────────────────────────────────
All pipeline logic lives in the `pipeline/` package.
This file contains only:
  • startup_checklist() — interactive terminal prompts
  • run_agent()         — phase orchestration
  • __main__ block      — argparse + entry point

Usage:
  python agent.py                              # Anthropic Claude
  python agent.py --demo                       # No API key
  python agent.py --ollama                     # Local Ollama LLM
  python agent.py --ollama --model mistral
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# Re-export everything from the pipeline package so that code which does
#   import agent as _ag
# continues to work exactly as before.
from pipeline import *          # noqa: F401,F403
from pipeline import helpers    # noqa: F401 — for _ag.helpers._last_merge_count
from pipeline.config import (
    console, OUTPUT_DIR, RESOURCES_DIR, MAX_SCRAPE_JOBS, OWNER_NAME,
)
from pipeline.phases import (
    phase1_ingest_resume, phase2_discover_jobs, phase3_score_jobs,
    phase4_tailor_resume, phase5_simulate_submission, phase6_update_tracker,
    phase7_run_report, PlaywrightSubmitter, _load_existing_applications,
    _launch_dashboard_and_wait,
)
from pipeline.providers import BaseProvider, get_provider
from pipeline.resume import _build_demo_resume, _read_resume, _save_tailored_resume


# ── Startup checklist ──────────────────────────────────────────────────────────

def startup_checklist() -> dict:
    from rich.panel import Panel
    console.print(Panel(
        "[bold white]Job Application Agent[/bold white]\n"
        "7-Phase Autonomous Run  •  Press Enter to accept [defaults]\n"
        "[dim]Modes: default (Claude) | --demo (no API) | --ollama (local LLM)[/dim]",
        border_style="bright_blue",
        title="[bold bright_blue]Startup Checklist[/bold bright_blue]",
    ))

    cfg: dict = {}

    console.print("\n[bold]1. Resume file[/bold]")
    path_str = input("   Path to resume (PDF/DOCX/TXT/TEX) [built-in demo profile]: ").strip()
    if path_str:
        resume_text, latex_source = _read_resume(Path(path_str))
        cfg["resume_path"]  = Path(path_str)
        cfg["resume_text"]  = resume_text
        cfg["latex_source"] = latex_source
    else:
        cfg["resume_path"]  = None
        cfg["resume_text"]  = _build_demo_resume()
        cfg["latex_source"] = None
        console.print("   [dim](i)[/dim]  Using built-in profile from CLAUDE.md")

    console.print("\n[bold]2. Target job titles[/bold] (comma-separated, up to 3)")
    raw = input(
        "   [IC Design Intern, Photonics Engineer Intern, FPGA/Hardware Intern]: "
    ).strip()
    cfg["job_titles"] = (
        [t.strip() for t in raw.split(",")]
        if raw else ["IC Design Intern", "Photonics Engineer Intern", "FPGA/Hardware Intern"]
    )

    console.print("\n[bold]3. Preferred location[/bold]")
    cfg["location"] = input("   [Remote / United States]: ").strip() or "Remote"

    console.print("\n[bold]4. Minimum match score for auto-apply[/bold]")
    raw = input("   [75]: ").strip()
    cfg["threshold"] = int(raw) if raw.isdigit() else 75

    console.print("\n[bold]5. Minimum salary[/bold] (optional)")
    cfg["min_salary"] = input("   [Enter to skip]: ").strip() or None

    console.print("\n[bold]6. Companies to exclude[/bold] (optional)")
    raw = input("   [Enter to skip]: ").strip()
    cfg["blacklist"] = [c.strip() for c in raw.split(",")] if raw else []

    console.print("\n[bold]7. Priority target companies[/bold]")
    raw = input(
        "   [NVIDIA, Apple, Microsoft, Intel, IBM, Micron, Samsung, TSMC]: "
    ).strip()
    cfg["whitelist"] = (
        [c.strip() for c in raw.split(",")]
        if raw else ["NVIDIA", "Apple", "Microsoft", "Intel", "IBM", "Micron", "Samsung", "TSMC"]
    )

    console.print("\n[bold]9. Cover letter preference[/bold]")
    raw = input("   yes / no / only for >=85 [no]: ").strip().lower()
    cfg["cover_letter_mode"] = raw if raw in ("yes", "no", "only for >=85") else "no"

    console.print("\n[bold]10. Max applications per run[/bold]")
    raw = input("   [10]: ").strip()
    cfg["max_apps"] = int(raw) if raw.isdigit() else 10

    return cfg


# ── Main orchestrator ──────────────────────────────────────────────────────────

def run_agent(config: dict, provider: BaseProvider) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    RESOURCES_DIR.mkdir(exist_ok=True)

    console.print("\n" + "─" * 64)
    console.print("[bold green]🤖  Job Application Agent — Run Starting[/bold green]")
    console.print("─" * 64)

    profile = phase1_ingest_resume(
        config["resume_text"], provider,
        preferred_titles=config.get("job_titles"),
    )
    if not profile:
        console.print("[red]Phase 1 failed — cannot parse resume.[/red]")
        return

    jobs = phase2_discover_jobs(
        profile, config["job_titles"], config["location"], provider,
        use_simplify=config.get("use_simplify", True),
        max_jobs=config.get("max_scrape_jobs", MAX_SCRAPE_JOBS),
    )
    if config.get("blacklist"):
        bl   = {c.lower() for c in config["blacklist"]}
        jobs = [j for j in jobs if j.get("company", "").lower() not in bl]

    threshold = config.get("threshold", 75)
    scored = phase3_score_jobs(
        jobs, profile, provider, min_score=60,
        experience_levels=config.get("experience_levels"),
        education_filter=config.get("education_filter"),
        citizenship_filter=config.get("citizenship_filter", "all"),
    )

    auto_eligible = [j for j in scored if j.get("score", 0) >= threshold]
    review_needed = [j for j in scored if 60 <= j.get("score", 0) < threshold]

    console.print(
        f"\n  📋 [bold]{len(auto_eligible)}[/bold] auto-eligible (≥{threshold})  |  "
        f"[yellow]{len(review_needed)}[/yellow] for review  |  "
        f"[red]{len(scored) - len(auto_eligible) - len(review_needed)}[/red] skipped"
    )

    console.print("\n[bold]Top auto-eligible jobs:[/bold]")
    for j in auto_eligible[:5]:
        console.print(
            f"  [{j['score']}] {j['company']} — {j['title']}  "
            f"[dim]{j.get('location', '?')} · {j.get('platform', '')}[/dim]"
        )

    if config.get("dashboard"):
        preliminary_apps = [
            {
                **j,
                "date_applied":      datetime.now().strftime("%m/%d/%Y"),
                "resume_version":    "",
                "cover_letter_sent": False,
                "status": (
                    "Auto-eligible"
                    if j.get("score", 0) >= config.get("threshold", 75)
                    else "Manual Required"
                ),
                "confirmation": "N/A",
                "notes":        "",
            }
            for j in scored
        ]
        phase6_update_tracker(preliminary_apps)
        _launch_dashboard_and_wait(
            OUTPUT_DIR / f"Job_Applications_Tracker_{datetime.now().strftime('%Y-%m')}.xlsx"
        )

    proceed = input("\nProceed with submissions? [Y/n]: ").strip().lower()
    if proceed == "n":
        console.print("[yellow]Run cancelled.[/yellow]")
        return

    to_process      = auto_eligible[:config.get("max_apps", 10)]
    already_applied = _load_existing_applications()
    applications    = []
    submitter       = PlaywrightSubmitter(profile) if config.get("real_apply") else None

    for i, job in enumerate(to_process, 1):
        console.print(
            f"\n[bold]({i}/{len(to_process)}) {job['title']} @ {job['company']}[/bold]"
            f"  score={job['score']}"
        )

        include_cl = (
            config.get("cover_letter_mode") == "yes"
            or (config.get("cover_letter_mode") == "only for >=85"
                and job.get("score", 0) >= 85)
        )

        console.print("  ✏️  Tailoring resume...")
        tailored = phase4_tailor_resume(
            job, profile, config["resume_text"], provider, include_cl,
            section_order=config.get("section_order"),
        )

        if tailored.get("ats_keywords_missing"):
            console.print(
                f"  ⚠️  ATS gaps: [yellow]"
                f"{', '.join(tailored['ats_keywords_missing'][:4])}[/yellow]"
            )

        resume_file = _save_tailored_resume(job, tailored, config.get("latex_source"))
        console.print(f"  💾 Resume → output/{resume_file}")

        if submitter:
            console.print("  🚀 Submitting via Playwright...")
            result = submitter.submit(job, str(OUTPUT_DIR / resume_file))
        else:
            console.print("  🚀 Submitting (demo mode)...")
            result = phase5_simulate_submission(job, already_applied)

        icon = "✅" if result["status"] == "Applied" else "⚠️"
        console.print(f"  {icon} {result['status']}  •  Confirmation: {result['confirmation']}")

        applications.append({
            **job,
            "date_applied":      datetime.now().strftime("%m/%d/%Y"),
            "resume_version":    resume_file,
            "cover_letter_sent": bool(tailored.get("cover_letter")),
            "status":            result["status"],
            "confirmation":      result["confirmation"],
            "notes": (
                "ATS gaps: " + ", ".join(tailored.get("ats_keywords_missing", [])[:2])
                if tailored.get("ats_keywords_missing") else ""
            ),
        })

    for job in scored:
        if job not in to_process:
            applications.append({
                **job,
                "date_applied":      datetime.now().strftime("%m/%d/%Y"),
                "resume_version":    "",
                "cover_letter_sent": False,
                "status":            "Skipped",
                "confirmation":      "N/A",
                "notes": f"Score {job.get('score', 0)} below threshold or max_apps reached",
            })

    tracker_path = phase6_update_tracker(applications)
    phase7_run_report(applications, tracker_path, provider)

    console.print("\n[bold green]✅  Agent run complete![/bold green]")
    if tracker_path:
        console.print(f"   📊 Tracker → [bold]{tracker_path}[/bold]")
    console.print(
        "\n[dim]Next: review Manual Required rows, "
        "update 'Response Received' as replies arrive.[/dim]"
    )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Job Application Agent — autonomous 7-phase job search system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python agent.py                   # Claude (needs ANTHROPIC_API_KEY)\n"
            "  python agent.py --demo            # No API key needed\n"
            "  python agent.py --ollama          # Local Ollama (free)\n"
            "  python agent.py --ollama --model mistral\n"
        ),
    )
    parser.add_argument("--demo",   action="store_true",
                        help="Run without any LLM — template/regex mode, zero cost")
    parser.add_argument("--ollama", action="store_true",
                        help="Use local Ollama LLM (free, requires ollama.com)")
    parser.add_argument("--model",  default="llama3.2",
                        help="Ollama model name (default: llama3.2)")
    parser.add_argument("--section-order", default=None,
                        help="Comma-separated resume section order")
    parser.add_argument("--real-apply", action="store_true",
                        help="Use Playwright for real form submission (Greenhouse boards)")
    parser.add_argument("--dashboard", action="store_true",
                        help="Launch Flask dashboard after scoring for manual review")
    parser.add_argument(
        "--experience", default="internship,entry-level",
        help="Comma-separated experience levels (default: internship,entry-level)",
    )
    parser.add_argument(
        "--education", default="bachelors,masters,unknown",
        help="Comma-separated education levels (default: bachelors,masters,unknown)",
    )
    parser.add_argument(
        "--citizenship", default="all",
        choices=["all", "exclude_required", "only_required"],
        help="Citizenship filter (default: all)",
    )
    parser.add_argument("--no-simplify", action="store_true",
                        help="Skip SimplifyJobs/GitHub internship listings")
    args = parser.parse_args()

    if not args.demo and not args.ollama and not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[red]Error: ANTHROPIC_API_KEY not set.[/red]\n\n"
            "Options:\n"
            "  1. Set key:  [bold]set ANTHROPIC_API_KEY=sk-ant-...[/bold]  (Windows)\n"
            "  2. No key?   [bold]python agent.py --demo[/bold]\n"
            "  3. Free LLM: [bold]python agent.py --ollama[/bold]  (install ollama.com first)"
        )
        sys.exit(1)

    provider = get_provider(args)
    config   = startup_checklist()
    config["section_order"]     = (
        [s.strip() for s in args.section_order.split(",")]
        if args.section_order else None
    )
    config["real_apply"]        = args.real_apply
    config["dashboard"]         = args.dashboard
    config["use_simplify"]      = not args.no_simplify
    config["experience_levels"] = [x.strip() for x in args.experience.split(",")]
    config["education_filter"]  = [x.strip() for x in args.education.split(",")]
    config["citizenship_filter"] = args.citizenship
    run_agent(config, provider)
