"""
tests/fakes.py
──────────────
Test doubles. Kept light: every fake is a plain class implementing the
real Protocol/ABC so mypy + the test reader can see exactly what it
returns. No magic.
"""
from __future__ import annotations

from typing import Iterable, Iterator
from datetime import datetime

from pipeline.providers import BaseProvider


# ── Fake LLM provider ────────────────────────────────────────────────────────


_DEFAULT_PROFILE = {
    "name": "Jane Tester",
    "email": "jane@example.com",
    "linkedin": "https://www.linkedin.com/in/jane-tester",
    "github": "",
    "phone": "",
    "location": "Remote",
    "summary": "EE student building tape-out experience.",
    "target_titles": [
        "IC Design Engineering Intern",
        "FPGA / Digital Design Engineering Intern",
    ],
    "top_hard_skills": ["Verilog", "SPICE", "Cadence Virtuoso", "Python", "MATLAB"],
    "top_soft_skills": ["Teamwork", "Technical writing"],
    "education": [
        {"degree": "B.S. in Electrical Engineering", "institution": "Test University",
         "year": "2026", "gpa": "3.85"},
    ],
    "experience": [],
    "research_experience": [],
    "work_experience": [],
    "projects": [
        {"name": "8-bit ALU", "description": "Verilog ALU + testbench",
         "skills_used": ["Verilog", "ModelSim"], "bullets": [], "dates": "", "url": ""},
    ],
    "resume_gaps": [],
}

_DEFAULT_SCORE = {
    "job_id": "test-job-1",
    "score": 82,
    "score_breakdown": {
        "required_skills":    {"raw": 0.8, "weight": 50, "points": 40},
        "industry":           {"raw": 0.9, "weight": 30, "points": 27},
        "location_seniority": {"raw": 0.75, "weight": 20, "points": 15},
    },
    "reasoning": "Skills 80%, industry 90%, location/seniority 75%.",
    "matching_skills": ["Verilog", "SPICE"],
    "missing_skills":  ["UVM"],
    "reason": "Strong fit on the digital design axis.",
}

_DEFAULT_TAILORED = {
    "skills_reordered": ["Verilog", "SPICE", "Python", "Cadence Virtuoso"],
    "experience_bullets": [],
    "ats_keywords_missing": ["UVM"],
    "section_order": ["Skills", "Projects", "Experience", "Education"],
}


class FakeProvider(BaseProvider):
    """Drop-in ``BaseProvider`` that returns canned, mutation-friendly data.

    Construct with overrides for any of the canned responses; tests can also
    flip ``self.profile`` / ``self.score`` / ``self.tailored`` mid-run to
    simulate provider variation.
    """

    def __init__(self, *, profile=None, score=None, tailored=None,
                 cover_letter="Generated cover letter.",
                 report="Run summary.\n",
                 chat_response="(fake chat)",
                 demo_jobs=None):
        self.profile = profile if profile is not None else dict(_DEFAULT_PROFILE)
        self.score = score if score is not None else dict(_DEFAULT_SCORE)
        self.tailored = tailored if tailored is not None else dict(_DEFAULT_TAILORED)
        self.cover_letter = cover_letter
        self.report = report
        self.chat_response = chat_response
        self.demo_jobs = demo_jobs if demo_jobs is not None else []

        # Call counters so tests can assert how many times each method ran.
        self.calls = {
            "extract_profile": 0, "score_job": 0, "tailor_resume": 0,
            "generate_cover_letter": 0, "generate_report": 0,
            "generate_demo_jobs": 0, "chat": 0,
        }

    def extract_profile(self, resume_text: str, preferred_titles=None,
                        heuristic_hint=None):
        self.calls["extract_profile"] += 1
        return dict(self.profile)

    def score_job(self, job: dict, profile: dict) -> dict:
        self.calls["score_job"] += 1
        out = dict(self.score)
        out["job_id"] = job.get("id", out.get("job_id", ""))
        return out

    def tailor_resume(self, job: dict, profile: dict, resume_text: str) -> dict:
        self.calls["tailor_resume"] += 1
        return dict(self.tailored)

    def generate_cover_letter(self, job: dict, profile: dict) -> str:
        self.calls["generate_cover_letter"] += 1
        return self.cover_letter

    def generate_report(self, summary_data: dict) -> str:
        self.calls["generate_report"] += 1
        return self.report

    def generate_demo_jobs(self, profile: dict, titles, location: str) -> list:
        self.calls["generate_demo_jobs"] += 1
        return list(self.demo_jobs)

    def chat(self, system: str, messages, max_tokens: int = 1024,
             json_mode: bool = False) -> str:
        self.calls["chat"] += 1
        return self.chat_response


# ── Fake JobSource ───────────────────────────────────────────────────────────


class FakeJobSource:
    """Drop-in ``JobSource`` Protocol implementation. Constructor takes the
    rows to yield from ``fetch()``. Tests can register multiple instances
    under different names.
    """

    def __init__(self, name: str, jobs: Iterable[dict],
                 cadence_seconds: int = 60, timeout_seconds: int = 5):
        self.name = name
        self.cadence_seconds = cadence_seconds
        self.timeout_seconds = timeout_seconds
        self._jobs = list(jobs)
        self.fetch_calls = 0

    def fetch(self, since: datetime | None) -> Iterator[dict]:
        self.fetch_calls += 1
        for j in self._jobs:
            yield dict(j)


# ── Sample raw-job factory ───────────────────────────────────────────────────


def make_raw_job(*, company="Acme Robotics", title="FPGA Intern",
                 url=None, location="Remote", remote=True,
                 description="Design and verify FPGA blocks for our flagship robot.",
                 requirements=None, salary_range="$30-$45/hr",
                 platform="Greenhouse", posted_date="2026-04-15") -> dict:
    """Produce a minimally-populated raw RawJob suitable for upsert_many.

    Caller can pass overrides for any keyword — defaults are deterministic
    and match the JobSource Protocol's RawJob TypedDict.
    """
    if url is None:
        url = (
            f"https://boards.greenhouse.io/{company.lower().replace(' ', '')}"
            f"/jobs/{abs(hash(title)) % 1_000_000}"
        )
    return {
        "application_url": url,
        "company": company,
        "title": title,
        "location": location,
        "remote": remote,
        "description": description,
        "requirements": requirements or ["Verilog", "Python", "FPGA"],
        "salary_range": salary_range,
        "platform": platform,
        "posted_date": posted_date,
        "source": "fake:test",
    }
