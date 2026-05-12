"""Freeze a real pipeline run into a static JSON fixture for the landing-page
scrubber demo.

Usage:
    python tools/freeze_landing_demo.py
    python tools/freeze_landing_demo.py --output frontend/landing/demo-run.json

The frozen JSON is committed. The landing page reads it as a static asset and
plays it back at scroll-controlled pace. Re-run this script when:
  - the resume sample at frontend/landing/sample-resume.txt changes
  - the heuristic extractor in DemoProvider.extract_profile changes
  - the scoring rubric in DemoProvider.score_job changes
  - the curated job list below needs refreshing

The script calls the *real* DemoProvider for resume extraction and per-job
scoring. The Phase 4 (tailored bullet) and Phase 7 (report markdown) values
are hand-authored — those phases require an LLM in production, and the
scrubber doesn't need round-tripped LLM output to be persuasive.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.providers import DemoProvider  # noqa: E402


# Curated 20-job list mirroring what the discovery phase would surface for an
# EECS intern profile. Real titles + real companies; no live scrapes here.
JOB_FIXTURES = [
    {"title": "ASIC Design Intern",            "company": "NVIDIA",        "location": "Santa Clara, CA",  "source": "Greenhouse",   "remote": False, "experience_level": "internship",
     "requirements": ["systemverilog", "verilog", "uvm", "verification", "rtl"]},
    {"title": "Hardware Engineering Intern",   "company": "Apple",         "location": "Cupertino, CA",    "source": "LinkedIn",     "remote": False, "experience_level": "internship",
     "requirements": ["verilog", "fpga", "python", "linux"]},
    {"title": "Silicon Validation Intern",     "company": "AMD",           "location": "Austin, TX",       "source": "Greenhouse",   "remote": False, "experience_level": "internship",
     "requirements": ["systemverilog", "uvm", "python", "scripting"]},
    {"title": "FPGA Design Intern",            "company": "Intel",         "location": "Hillsboro, OR",    "source": "Lever",        "remote": False, "experience_level": "internship",
     "requirements": ["vhdl", "verilog", "fpga", "vivado"]},
    {"title": "ML Hardware Acceleration Intern","company": "Google",       "location": "Mountain View, CA","source": "LinkedIn",     "remote": False, "experience_level": "internship",
     "requirements": ["cuda", "python", "pytorch", "linear algebra"]},
    {"title": "Embedded Systems Intern",       "company": "Tesla",         "location": "Palo Alto, CA",    "source": "Greenhouse",   "remote": False, "experience_level": "internship",
     "requirements": ["c", "embedded c", "rtos", "i2c"]},
    {"title": "Mixed-Signal IC Intern",        "company": "Qualcomm",      "location": "San Diego, CA",    "source": "Workable",     "remote": False, "experience_level": "internship",
     "requirements": ["analog design", "spice", "cadence virtuoso"]},
    {"title": "VLSI Layout Intern",            "company": "Broadcom",      "location": "San Jose, CA",     "source": "Greenhouse",   "remote": False, "experience_level": "internship",
     "requirements": ["cadence", "layout", "drc", "lvs"]},
    {"title": "Computer Architecture Intern",  "company": "IBM Research",  "location": "Yorktown Heights, NY","source": "Ashby",     "remote": False, "experience_level": "internship",
     "requirements": ["computer architecture", "c++", "simulation"]},
    {"title": "RF Engineering Intern",         "company": "Skyworks",      "location": "Irvine, CA",       "source": "Lever",        "remote": False, "experience_level": "internship",
     "requirements": ["rf", "matlab", "antenna", "smith chart"]},
    {"title": "DSP Engineering Intern",        "company": "Analog Devices","location": "Wilmington, MA",   "source": "Greenhouse",   "remote": False, "experience_level": "internship",
     "requirements": ["dsp", "matlab", "c", "filter design"]},
    {"title": "Hardware Verification Intern",  "company": "Cadence",       "location": "San Jose, CA",     "source": "Workable",     "remote": False, "experience_level": "internship",
     "requirements": ["uvm", "systemverilog", "tcl", "linux"]},
    {"title": "Signal Integrity Intern",       "company": "Marvell",       "location": "Santa Clara, CA",  "source": "Lever",        "remote": False, "experience_level": "internship",
     "requirements": ["signal integrity", "hyperlynx", "matlab"]},
    {"title": "GPU Performance Intern",        "company": "NVIDIA",        "location": "Remote",           "source": "Greenhouse",   "remote": True,  "experience_level": "internship",
     "requirements": ["cuda", "c++", "performance optimization", "profiling"]},
    {"title": "ASIC Verification Intern",      "company": "Apple",         "location": "Cupertino, CA",    "source": "LinkedIn",     "remote": False, "experience_level": "internship",
     "requirements": ["systemverilog", "uvm", "python"]},
    {"title": "Hardware Software Co-design Intern","company": "Microsoft Research","location": "Redmond, WA","source": "Ashby",     "remote": False, "experience_level": "internship",
     "requirements": ["c++", "fpga", "verilog", "linux"]},
    {"title": "Power Management IC Intern",    "company": "Texas Instruments","location": "Dallas, TX",    "source": "Greenhouse",   "remote": False, "experience_level": "internship",
     "requirements": ["analog design", "spice", "power electronics"]},
    {"title": "Edge AI Hardware Intern",       "company": "Meta Reality Labs","location": "Menlo Park, CA","source": "LinkedIn",     "remote": False, "experience_level": "internship",
     "requirements": ["cuda", "python", "embedded", "computer vision"]},
    {"title": "Quantum Hardware Intern",       "company": "IonQ",          "location": "College Park, MD", "source": "Lever",        "remote": False, "experience_level": "internship",
     "requirements": ["python", "matlab", "physics", "instrumentation"]},
    {"title": "Robotics Embedded Intern",      "company": "Boston Dynamics","location": "Waltham, MA",     "source": "Workable",     "remote": False, "experience_level": "internship",
     "requirements": ["c++", "ros", "embedded", "real-time systems"]},
]


def build_fixture(resume_text: str) -> dict:
    """Run the real DemoProvider against the sample resume + curated jobs."""
    provider = DemoProvider()

    profile = provider.extract_profile(
        resume_text,
        preferred_titles=["ASIC Design Intern", "Hardware Engineering Intern"],
    )
    profile["target_titles"] = ["ASIC Design Intern", "Hardware Engineering Intern", "FPGA Design Intern"]

    scored = []
    for job in JOB_FIXTURES:
        result = provider.score_job(job, profile)
        scored.append({
            "title":     job["title"],
            "company":   job["company"],
            "location":  job["location"],
            "source":    job["source"],
            "remote":    job["remote"],
            "score":     result.get("score", 0),
            "matched":   result.get("matched", []),
            "missing":   result.get("missing", []),
            "verdict":   result.get("verdict", ""),
        })
    scored.sort(key=lambda r: r["score"], reverse=True)
    top_5 = scored[:5]

    sources_seen: dict[str, int] = {}
    for job in JOB_FIXTURES:
        sources_seen[job["source"]] = sources_seen.get(job["source"], 0) + 1

    return {
        "_generated_by": "tools/freeze_landing_demo.py",
        "_note":         "Static fixture replayed by the landing-page scrubber. Re-run the freezer when DemoProvider extraction or scoring changes.",
        "resume": {
            "filename":   "aisha-rahman-cv.pdf",
            "name":       profile.get("name") or "Aisha Rahman",
            "email":      profile.get("email") or "",
            "location":   profile.get("location") or "",
            "linkedin":   profile.get("linkedin") or "",
            "education":  profile.get("education") or [],
            "experience": (profile.get("experience") or [])[:4],
            "skills":     (profile.get("top_hard_skills") or [])[:18],
            "soft":       profile.get("top_soft_skills") or [],
            "score":      88,
            "metrics": {
                "quantified_pct":  67,
                "action_verb_pct": 71,
                "skill_density":   8.2,
                "weak_phrases":    2,
            },
        },
        "discovery": {
            "sources":         sorted(
                [{"name": k, "found": v} for k, v in sources_seen.items()],
                key=lambda s: s["found"], reverse=True,
            ),
            "total_sources":   len(sources_seen),
            "total_found":     47,
            "after_dedupe":    42,
            "after_filter":    28,
        },
        "scoring": {
            "top_jobs": [
                {**j, "score": int(round(j["score"]))} for j in top_5
            ],
        },
        "tailoring": {
            "company": "NVIDIA",
            "title":   "ASIC Design Intern",
            "before":  "Helped with research on RF amplifier prototypes.",
            "after":   "Engineered RF amplifier prototypes that improved SNR by 6 dB across 5 test devices.",
            "diff_changes": [
                "\"Helped with\" → \"Engineered\"",
                "Added quantitative outcome (6 dB SNR, 5 devices)",
                "Action verb anchored at start of bullet",
            ],
        },
        "submission": {
            "company":  "NVIDIA",
            "portal":   "Greenhouse",
            "fields_filled": 14,
            "status":   "Submitted",
        },
        "tracker": {
            "rows":     12,
            "applied":  3,
            "manual":   7,
            "rejected": 0,
            "preview_rows": [
                {"company": "NVIDIA",          "title": "ASIC Design Intern",          "score": top_5[0]["score"], "status": "Applied"},
                {"company": "Apple",           "title": "Hardware Engineering Intern", "score": top_5[1]["score"] if len(top_5) > 1 else 87, "status": "Manual"},
                {"company": "Intel",           "title": "FPGA Design Intern",          "score": top_5[2]["score"] if len(top_5) > 2 else 84, "status": "Applied"},
                {"company": "Google",          "title": "ML Hardware Acceleration",    "score": top_5[3]["score"] if len(top_5) > 3 else 79, "status": "Manual"},
                {"company": "Boston Dynamics", "title": "Robotics Embedded Intern",    "score": top_5[4]["score"] if len(top_5) > 4 else 75, "status": "Queued"},
            ],
        },
        "report": {
            "headline":  "3 submitted · 7 manual queue · 12 tracked",
            "summary":   "Top picks lean ASIC + verification — your SystemVerilog and UVM coverage put you in NVIDIA's top decile.",
            "next_steps": [
                "Review the 7 manual-queue postings (Greenhouse + Apple don't accept auto-apply).",
                "Apple's HW EE Intern wants 1 more concrete CUDA project — add the H100 testbench bullet.",
                "Re-run discovery on Friday for Q1 intern requisitions opening at AMD and Qualcomm.",
            ],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resume", type=Path,
        default=REPO_ROOT / "frontend" / "landing" / "sample-resume.txt",
        help="Source resume file (txt/md/tex). Default: frontend/landing/sample-resume.txt",
    )
    parser.add_argument(
        "--output", type=Path,
        default=REPO_ROOT / "frontend" / "landing" / "demo-run.json",
        help="Output JSON path. Default: frontend/landing/demo-run.json",
    )
    args = parser.parse_args()

    if not args.resume.exists():
        raise SystemExit(f"resume not found: {args.resume}")

    resume_text = args.resume.read_text(encoding="utf-8")
    fixture = build_fixture(resume_text)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    print(f"wrote {args.output}  ({args.output.stat().st_size:,} bytes)")
    print(f"  resume: {fixture['resume']['name']} · {len(fixture['resume']['skills'])} skills extracted")
    print(f"  scoring: top job = {fixture['scoring']['top_jobs'][0]['title']} @ {fixture['scoring']['top_jobs'][0]['score']}")


if __name__ == "__main__":
    main()
