"""
pipeline/providers.py
─────────────────────
LLM provider abstraction: base class, three concrete implementations
(Anthropic, Demo, Ollama), and the factory function.
"""

import json
import re
from datetime import date
from pathlib import Path

from .config import console, OWNER_NAME, DEMO_JOBS


# ── Base ───────────────────────────────────────────────────────────────────────

class BaseProvider:
    """Abstract base — all providers must implement these methods."""

    def extract_profile(self, resume_text: str, preferred_titles: list = None) -> dict:
        raise NotImplementedError

    def score_job(self, job: dict, profile: dict) -> dict:
        raise NotImplementedError

    def tailor_resume(self, job: dict, profile: dict, resume_text: str) -> dict:
        raise NotImplementedError

    def generate_cover_letter(self, job: dict, profile: dict) -> str:
        raise NotImplementedError

    def generate_report(self, summary_data: dict) -> str:
        raise NotImplementedError

    def generate_demo_jobs(self, profile: dict, titles: list, location: str) -> list:
        raise NotImplementedError


# ── 1. Anthropic (Claude) ──────────────────────────────────────────────────────

class AnthropicProvider(BaseProvider):
    """Uses Claude Opus 4.6 via the Anthropic SDK."""

    def __init__(self):
        import anthropic as _anthropic
        self.client = _anthropic.Anthropic()
        self.model = "claude-opus-4-6"

    def _tool_call(self, tool_def: dict, prompt: str,
                   max_tokens: int = 4096, thinking: bool = False) -> dict:
        kwargs = dict(
            model=self.model, max_tokens=max_tokens,
            tools=[tool_def],
            tool_choice={"type": "tool", "name": tool_def["name"]},
            messages=[{"role": "user", "content": prompt}],
        )
        if thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        resp = self.client.messages.create(**kwargs)
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        return {}

    def extract_profile(self, resume_text: str, preferred_titles: list = None) -> dict:
        tool = {
            "name": "save_profile",
            "description": "Save the extracted resume profile as structured data.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name":            {"type": "string"},
                    "email":           {"type": "string"},
                    "linkedin":        {"type": "string"},
                    "location":        {"type": "string"},
                    "target_titles":   {
                        "type": "array", "items": {"type": "string"},
                        "description": (
                            "5–8 specific job titles this candidate is best suited for, "
                            "based on their skills, experience, and education. "
                            "Use industry-standard title variants (e.g. 'IC Design Intern', "
                            "'VLSI Design Engineer', 'Photonics Engineer Intern'). "
                            "Weight heavily toward the candidate's stated preferences."
                        ),
                    },
                    "top_hard_skills": {"type": "array", "items": {"type": "string"}},
                    "top_soft_skills": {"type": "array", "items": {"type": "string"}},
                    "education": {
                        "type": "array",
                        "items": {"type": "object", "properties": {
                            "degree": {"type": "string"}, "institution": {"type": "string"},
                            "year":   {"type": "string"}, "gpa":         {"type": "string"},
                        }},
                    },
                    "experience": {
                        "type": "array",
                        "items": {"type": "object", "properties": {
                            "title":   {"type": "string"}, "company": {"type": "string"},
                            "dates":   {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                        }},
                    },
                    "projects": {
                        "type": "array",
                        "items": {"type": "object", "properties": {
                            "name":        {"type": "string"},
                            "description": {"type": "string"},
                            "skills_used": {"type": "array", "items": {"type": "string"}},
                        }},
                    },
                    "resume_gaps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "top_hard_skills", "top_soft_skills", "target_titles"],
            },
        }
        pref_hint = ""
        if preferred_titles:
            pref_hint = (
                f"\n\nThe candidate's preferred job titles are: {', '.join(preferred_titles)}. "
                "Use these as a strong signal when generating target_titles — include these "
                "and closely related industry-standard variants that match their background."
            )
        prompt = (
            "Parse this resume. Extract a complete structured profile. "
            "Build a Master Skills Profile: top 10 hard skills and top 5 soft skills "
            "ranked by frequency and recency. Flag any resume gaps.\n"
            "For target_titles: suggest 5–8 specific job titles this candidate is realistically "
            "suited for based on their skills, projects, and experience level. "
            "Use concrete, searchable titles (not generic ones like 'Engineer')."
            f"{pref_hint}\n\n"
            f"Resume:\n{resume_text}"
        )
        return self._tool_call(tool, prompt, thinking=True)

    def score_job(self, job: dict, profile: dict) -> dict:
        tool = {
            "name": "score_job",
            "description": "Score a job posting against the candidate profile.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "job_id":          {"type": "string"},
                    "score":           {"type": "integer", "minimum": 0, "maximum": 100},
                    "matching_skills": {"type": "array", "items": {"type": "string"}},
                    "missing_skills":  {"type": "array", "items": {"type": "string"}},
                    "reason":          {"type": "string"},
                },
                "required": ["job_id", "score", "matching_skills", "missing_skills", "reason"],
            },
        }
        profile_summary = json.dumps({
            "skills":    profile.get("top_hard_skills", []),
            "education": profile.get("education", []),
            "targets":   profile.get("target_titles", []),
        })
        prompt = (
            "Score this job (0-100) using: title alignment 25%, skills match 30%, "
            "experience 15%, industry 10%, education 10%, location 10%.\n\n"
            f"Candidate:\n{profile_summary}\n\n"
            f"Job Title: {job.get('title')}\nCompany: {job.get('company')}\n"
            f"Requirements: {', '.join(job.get('requirements', []))}\n"
            f"Description: {job.get('description', '')}\n"
            f"Location: {job.get('location')} (Remote: {job.get('remote', False)})"
        )
        return self._tool_call(tool, prompt, max_tokens=1024)

    def tailor_resume(self, job: dict, profile: dict, resume_text: str) -> dict:
        tool = {
            "name": "tailored_resume",
            "description": "Return tailored resume sections for this specific job.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "skills_reordered":   {"type": "array", "items": {"type": "string"}},
                    "experience_bullets": {
                        "type": "array",
                        "items": {"type": "object", "properties": {
                            "role":    {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                        }},
                    },
                    "ats_keywords_missing": {"type": "array", "items": {"type": "string"}},
                    "section_order":        {"type": "array", "items": {"type": "string"}},
                },
                "required": ["skills_reordered", "ats_keywords_missing"],
            },
        }
        prompt = (
            f"Tailor this resume for: {job['title']} at {job['company']}.\n"
            "Rules: reorder skills to front-load JD keywords, rephrase bullets naturally — "
            "NEVER fabricate, never change dates/titles/companies. "
            "Do NOT include a summary or objective section.\n"
            "ATS check: flag top JD keywords missing from resume.\n\n"
            f"JD Requirements: {', '.join(job.get('requirements', []))}\n"
            f"JD Description: {job.get('description', '')}\n\n"
            f"Candidate Skills: {', '.join(profile.get('top_hard_skills', []))}\n\n"
            f"Current Resume:\n{resume_text}"
        )
        return self._tool_call(tool, prompt, max_tokens=4096, thinking=True)

    def generate_cover_letter(self, job: dict, profile: dict) -> str:
        resp = self.client.messages.create(
            model=self.model, max_tokens=1024,
            messages=[{"role": "user", "content": (
                f"Write a 3-paragraph cover letter for {OWNER_NAME} applying to "
                f"{job['title']} at {job['company']}.\n"
                "Para 1: Hook + role name. Para 2: Top 2-3 achievements mapped to JD. "
                "Para 3: Enthusiasm + call to action.\n"
                f"Candidate skills: {', '.join(profile.get('top_hard_skills', [])[:5])}\n"
                f"JD requirements: {', '.join(job.get('requirements', [])[:5])}"
            )}],
        )
        return next(b.text for b in resp.content if b.type == "text")

    def generate_report(self, summary_data: dict) -> str:
        resp = self.client.messages.create(
            model=self.model, max_tokens=1024,
            messages=[{"role": "user", "content": (
                "Generate a concise job application run summary.\n\n"
                f"Data:\n{json.dumps(summary_data, indent=2)}\n\n"
                "Include: overall stats, top 3 applied jobs, manual items, "
                "2-3 recommended next steps. Plain text only."
            )}],
        )
        return next(b.text for b in resp.content if b.type == "text")

    def generate_demo_jobs(self, profile: dict, titles: list, location: str) -> list:
        skills = ", ".join(profile.get("top_hard_skills", [])[:5])
        resp = self.client.messages.create(
            model=self.model, max_tokens=4096,
            messages=[{"role": "user", "content": (
                "Generate 12 realistic internship job postings.\n"
                f"Titles: {', '.join(titles)}\nLocation: {location} or Remote\n"
                f"Key skills: {skills}\n\n"
                "Return a JSON array only (no markdown). Each object: "
                "id, title, company, location, remote (bool), "
                f"posted_date (ISO, last 14 days from {date.today().isoformat()}), "
                "description (2-3 sentences), requirements (array 5-8 strings), "
                "salary_range (string or null), application_url, "
                "platform (LinkedIn|Indeed|Glassdoor|Handshake|Company Site).\n"
                "Focus on IC design, photonics, FPGA, hardware at top EE companies."
            )}],
        )
        raw = next(b.text for b in resp.content if b.type == "text")
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        return json.loads(m.group()) if m else []


# ── 2. Demo (regex / template, no API) ────────────────────────────────────────

class DemoProvider(BaseProvider):
    """Template/regex-based provider.  Zero cost, zero setup, fully offline."""

    _DEFAULT_KEYWORDS = [
        "verilog", "vhdl", "fpga", "spice", "matlab", "python", "java", "latex",
        "photolithography", "cleanroom", "pld", "cmos", "pcb", "ltspice",
        "onshape", "fusion360", "solidworks", "cad", "linux", "c++",
        "pulsed laser deposition", "thin film", "sem", "afm",
        "digital design", "analog design", "mixed-signal", "rtl", "synthesis",
    ]

    def __init__(self):
        self.SKILL_KEYWORDS = self._load_keywords()

    @staticmethod
    def _load_keywords() -> list:
        yaml_path = Path("config/skill_keywords.yaml")
        try:
            import yaml
            with open(yaml_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            keywords = []
            for group in data.values():
                if isinstance(group, list):
                    keywords.extend(group)
            return keywords
        except Exception:
            return list(DemoProvider._DEFAULT_KEYWORDS)

    def extract_profile(self, resume_text: str, preferred_titles: list = None) -> dict:
        text_lower = resume_text.lower()

        email_match = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}', resume_text)
        email = email_match.group() if email_match else ""

        linkedin_match = re.search(r'linkedin\.com/in/[\w-]+', resume_text, re.I)
        linkedin = linkedin_match.group() if linkedin_match else ""

        found_skills = [s for s in self.SKILL_KEYWORDS if s in text_lower]
        skill_display = {
            "verilog": "Verilog", "vhdl": "VHDL", "fpga": "FPGA", "spice": "SPICE",
            "matlab": "MATLAB", "python": "Python", "java": "Java", "latex": "LaTeX",
            "photolithography": "Photolithography", "cleanroom": "Cleanroom Processes",
            "pld": "Pulsed Laser Deposition", "cmos": "CMOS", "pcb": "PCB Design",
            "onshape": "OnShape", "fusion360": "Fusion360", "solidworks": "SolidWorks",
            "cad": "CAD", "linux": "Linux", "c++": "C++",
            "pulsed laser deposition": "Pulsed Laser Deposition",
            "thin film": "Thin Film Deposition", "sem": "SEM",
            "digital design": "Digital Design", "mixed-signal": "Mixed-Signal",
        }
        hard_skills = [skill_display.get(s, s.title()) for s in found_skills[:10]]
        if not hard_skills:
            hard_skills = ["MATLAB", "Python", "Verilog", "FPGA", "SPICE"]

        degree = "B.S. Electrical & Computer Engineering"
        institution = "University of Oklahoma"

        name = OWNER_NAME
        for line in resume_text.splitlines():
            line = line.strip()
            if line and len(line.split()) in (2, 3) and line[0].isupper() and "@" not in line:
                name = line
                break

        title_map = {
            "fpga":          "FPGA/Hardware Engineering Intern",
            "photolithography": "Photonics Engineering Intern",
            "photonics":     "Photonics Engineering Intern",
            "spice":         "IC Design Engineering Intern",
            "verilog":       "IC Design Engineering Intern",
            "vhdl":          "VLSI Design Engineering Intern",
            "cmos":          "IC Design Engineering Intern",
            "pcb":           "Hardware Engineering Intern",
            "mixed-signal":  "Mixed-Signal Design Intern",
            "semiconductor": "Semiconductor Process Engineering Intern",
            "thin film":     "Thin Film / Materials Engineering Intern",
        }
        inferred = list({title_map[k] for k in title_map if k in text_lower})
        seen: set = set()
        target_titles: list = []
        for t in (preferred_titles or []) + inferred:
            if t not in seen:
                seen.add(t)
                target_titles.append(t)
        if not target_titles:
            target_titles = ["IC Design Engineering Intern", "Hardware Engineering Intern"]

        gaps = []
        if "summary" not in text_lower and "objective" not in text_lower:
            gaps.append("Missing professional summary/objective")
        if not re.search(r'\d+%|\d+ students|\d+ projects', resume_text):
            gaps.append("Few quantified achievements — add metrics")

        return {
            "name": name, "email": email, "linkedin": linkedin,
            "location": "Oklahoma, USA",
            "target_titles": target_titles,
            "top_hard_skills": hard_skills,
            "top_soft_skills": ["Teamwork", "Problem-solving", "Communication",
                                 "Attention to detail", "Time management"],
            "education": [{"degree": degree, "institution": institution,
                            "year": "2028", "gpa": ""}],
            "experience": [], "projects": [], "resume_gaps": gaps,
        }

    def score_job(self, job: dict, profile: dict) -> dict:
        skills_lower = {s.lower() for s in profile.get("top_hard_skills", [])}
        reqs = [r.lower() for r in job.get("requirements", [])]

        matched = [r for r in reqs if any(s in r or r in s for s in skills_lower)]
        skills_score = min(30, int(len(matched) / max(len(reqs), 1) * 30))

        title_lower = job.get("title", "").lower()
        target_titles_lower = [t.lower() for t in profile.get("target_titles", [])]
        title_score = 20 if any(
            any(word in title_lower for word in t.split()) for t in target_titles_lower
        ) else 10

        edu_score   = 10
        loc         = job.get("location", "").lower()
        remote_ok   = job.get("remote", False)
        loc_score   = 10 if remote_ok or "oklahoma" in loc or "united states" in loc else 5
        industry_score = 8
        exp_score   = 10

        total = title_score + skills_score + exp_score + industry_score + edu_score + loc_score
        missing = [r.title() for r in reqs if r not in matched and len(r) > 3][:5]

        return {
            "job_id": job.get("id", ""),
            "score": min(total, 100),
            "matching_skills": [r.title() for r in matched][:6],
            "missing_skills": missing,
            "reason": (
                f"Matched {len(matched)}/{len(reqs)} requirements. "
                f"Skills score: {skills_score}/30, Title: {title_score}/25."
            ),
        }

    def tailor_resume(self, job: dict, profile: dict, resume_text: str) -> dict:  # noqa: ARG002
        jd_keywords  = [r.lower() for r in job.get("requirements", [])]
        skills       = profile.get("top_hard_skills", [])
        skills_lower = {s.lower(): s for s in skills}

        matching         = [skills_lower[k] for k in jd_keywords if k in skills_lower]
        other            = [s for s in skills if s not in matching]
        skills_reordered = matching + other

        missing_kw = [
            r.title() for r in jd_keywords
            if not any(r in s.lower() or s.lower() in r for s in skills)
        ]
        return {
            "skills_reordered":     skills_reordered,
            "experience_bullets":   [],
            "ats_keywords_missing": missing_kw[:5],
            "section_order":        ["Skills", "Projects", "Experience", "Education"],
        }

    def generate_cover_letter(self, job: dict, profile: dict) -> str:
        skills_str = ", ".join(profile.get("top_hard_skills", [])[:3])
        return (
            f"Dear {job['company']} Hiring Team,\n\n"
            f"I am writing to express my strong interest in the {job['title']} position at "
            f"{job['company']}. As an Electrical & Computer Engineering student at the "
            f"University of Oklahoma with experience in {skills_str}, I am excited to "
            f"contribute to your team.\n\n"
            "My hands-on lab work in photolithography, cleanroom fabrication, and data "
            "analysis with MATLAB and Python directly aligns with your requirements. "
            "I have applied these skills in research projects that mirror the challenges "
            "your team works on.\n\n"
            f"I would welcome the opportunity to discuss how I can contribute to "
            f"{job['company']}'s goals. Thank you for your consideration.\n\n"
            f"Sincerely,\n{OWNER_NAME}\nsao.sithisack@ou.edu"
        )

    def generate_report(self, summary_data: dict) -> str:
        top3 = summary_data.get("top3_applied", [])
        top3_lines = "\n".join(
            f"  {i+1}. {c} — {t} (score: {s})"
            for i, (c, t, s) in enumerate(top3)
        )
        manual = summary_data.get("manual", 0)
        return (
            f"Run Summary — {date.today().isoformat()}\n\n"
            "Results:\n"
            f"  • Jobs evaluated:       {summary_data.get('total_found', 0)}\n"
            f"  • Applications sent:    {summary_data.get('applied', 0)}\n"
            f"  • Manual review needed: {manual}\n"
            f"  • Skipped (low match):  {summary_data.get('skipped', 0)}\n\n"
            f"Top Jobs Applied To:\n{top3_lines}\n\n"
            + (f"Manual Review ({manual} item(s)):\n"
               + "\n".join(f"  - {r}" for r in summary_data.get("manual_reasons", []))
               + "\n\n" if manual else "")
            + "Recommended Next Steps:\n"
              "  1. Add quantified metrics to resume bullets (e.g., 'reduced error rate by 20%').\n"
              "  2. Follow up on applied jobs in 7 days via LinkedIn or email.\n"
              "  3. Update skills section with any ATS gaps flagged in tailored resumes.\n"
        )

    def generate_demo_jobs(self, profile: dict, titles: list, location: str) -> list:  # noqa: ARG002
        return DEMO_JOBS


# ── 3. Ollama (local LLM) ──────────────────────────────────────────────────────

class OllamaProvider(BaseProvider):
    """Uses a local Ollama model.  Free, no API key, requires Ollama installed."""

    OLLAMA_URL = "http://localhost:11434"

    def __init__(self, model: str = "llama3.2"):
        self.model = model
        self._check_ollama()

    def _check_ollama(self):
        import urllib.request as _ur
        import json as _json

        try:
            resp = _ur.urlopen(f"{self.OLLAMA_URL}/api/tags", timeout=5)
            data = _json.loads(resp.read().decode())
        except Exception as e:
            raise ConnectionError(
                f"Ollama is not reachable at {self.OLLAMA_URL}.\n"
                f"Start it with:  ollama serve\n(error: {e})"
            ) from e

        models      = data.get("models", [])
        local_bases = {m.get("name", "").split(":")[0] for m in models}
        local_full  = {m.get("name", "") for m in models}
        req_base    = self.model.split(":")[0]

        if self.model not in local_full and req_base not in local_bases:
            available = ", ".join(sorted(local_bases)) or "none"
            raise ValueError(
                f"Model '{self.model}' is not pulled in Ollama.\n"
                f"Available: {available}\n"
                f"Fix: ollama pull {self.model}"
            )

    def _chat(self, prompt: str) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "openai package required for Ollama mode.  Run: pip install openai"
            ) from exc

        import time as _time
        oc = OpenAI(base_url=f"{self.OLLAMA_URL}/v1", api_key="ollama", timeout=120)
        for attempt in range(5):
            try:
                resp = oc.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                )
                return resp.choices[0].message.content or ""
            except Exception as e:
                if "429" in str(e) or "too many concurrent" in str(e).lower():
                    wait = 2 ** attempt
                    console.print(
                        f"  [yellow]⏳ Ollama busy — retrying in {wait}s "
                        f"(attempt {attempt+1}/5)…[/yellow]"
                    )
                    _time.sleep(wait)
                else:
                    raise
        raise RuntimeError("Ollama rate limit: exceeded 5 retry attempts")

    def _parse_json(self, text: str, fallback: dict) -> dict:
        for candidate in [text, re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.M)]:
            try:
                return json.loads(candidate.strip())
            except json.JSONDecodeError:
                pass
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return fallback

    def extract_profile(self, resume_text: str, preferred_titles: list = None) -> dict:
        pref_hint = ""
        if preferred_titles:
            pref_hint = (
                f"\nThe candidate's preferred roles are: {', '.join(preferred_titles)}. "
                "Weight these heavily when suggesting target_titles; include these and "
                "closely related industry-standard title variants."
            )
        prompt = (
            "Parse this resume and return ONLY a JSON object with these fields:\n"
            "name, email, linkedin, location, "
            "target_titles (array of 5-8 specific searchable job titles this candidate "
            "is best suited for based on their skills and experience level), "
            "top_hard_skills (array of 10), top_soft_skills (array of 5), "
            "education (array of {degree, institution, year, gpa}), "
            "experience (array of {title, company, dates, bullets[]}), "
            "projects (array of {name, description, skills_used[]}), "
            "resume_gaps (array of strings)."
            f"{pref_hint}\n\n"
            f"Resume:\n{resume_text[:3000]}"
        )
        raw = self._chat(prompt)
        return self._parse_json(raw, {
            "name": OWNER_NAME, "email": "", "linkedin": "", "location": "",
            "target_titles": preferred_titles or ["IC Design Intern", "Hardware Engineering Intern"],
            "top_hard_skills": ["MATLAB", "Python", "Verilog", "SPICE", "Photolithography"],
            "top_soft_skills": ["Teamwork", "Problem-solving", "Communication",
                                 "Detail-oriented", "Time management"],
            "education": [], "experience": [], "projects": [], "resume_gaps": [],
        })

    def score_job(self, job: dict, profile: dict) -> dict:
        prompt = (
            "Score this job against the candidate (0-100). "
            "Weights: title alignment 25%, skills match 30%, experience 15%, "
            "industry 10%, education 10%, location 10%.\n"
            "Return ONLY a JSON object with: job_id (string), score (int), "
            "matching_skills (array), missing_skills (array), reason (string).\n\n"
            f"Candidate skills: {', '.join(profile.get('top_hard_skills', []))}\n"
            f"Education: {profile.get('education', [{}])[0].get('degree', '') if profile.get('education') else ''}\n"
            f"Target titles: {', '.join(profile.get('target_titles', []))}\n\n"
            f"Job: {job.get('title')} at {job.get('company')}\n"
            f"Requirements: {', '.join(job.get('requirements', []))}\n"
            f"Location: {job.get('location')} (Remote: {job.get('remote', False)})"
        )
        raw = self._chat(prompt)
        result = self._parse_json(raw, {
            "job_id": job.get("id", ""), "score": 50,
            "matching_skills": [], "missing_skills": [], "reason": "Scored by Ollama",
        })
        result.setdefault("job_id", job.get("id", ""))
        return result

    def tailor_resume(self, job: dict, profile: dict, resume_text: str) -> dict:
        prompt = (
            f"Tailor this resume for '{job['title']}' at '{job['company']}'.\n"
            "Return ONLY a JSON object with:\n"
            "  skills_reordered (array, JD-matching skills first),\n"
            "  experience_bullets (array of {role, bullets[]}),\n"
            "  ats_keywords_missing (array of JD keywords not in resume),\n"
            "  section_order (array of section names, do NOT include Summary or Objective).\n"
            "Do NOT include a summary or objective field. "
            "NEVER fabricate experience. Only rephrase what exists.\n\n"
            f"JD Requirements: {', '.join(job.get('requirements', []))}\n"
            f"Candidate Skills: {', '.join(profile.get('top_hard_skills', []))}\n\n"
            f"Resume (excerpt):\n{resume_text[:2000]}"
        )
        raw = self._chat(prompt)
        return self._parse_json(raw, {
            "skills_reordered":     profile.get("top_hard_skills", []),
            "experience_bullets":   [],
            "ats_keywords_missing": [],
            "section_order":        ["Skills", "Projects", "Experience", "Education"],
        })

    def generate_cover_letter(self, job: dict, profile: dict) -> str:
        prompt = (
            f"Write a 3-paragraph cover letter for {OWNER_NAME} applying to "
            f"{job['title']} at {job['company']}. "
            "Para 1: hook + role name. Para 2: 2-3 achievements mapped to JD. "
            "Para 3: enthusiasm + CTA. Professional and concise.\n"
            f"Candidate skills: {', '.join(profile.get('top_hard_skills', [])[:5])}"
        )
        return self._chat(prompt)

    def generate_report(self, summary_data: dict) -> str:
        prompt = (
            "Write a concise job application run summary (plain text).\n"
            "Include: overall stats, top 3 jobs, manual items, 2-3 next steps.\n\n"
            f"Data:\n{json.dumps(summary_data, indent=2)}"
        )
        return self._chat(prompt)

    def generate_demo_jobs(self, profile: dict, titles: list, location: str) -> list:
        prompt = (
            f"Generate 10 realistic internship job postings for titles: {', '.join(titles)}. "
            f"Location: {location} or Remote. "
            f"Skills focus: {', '.join(profile.get('top_hard_skills', [])[:5])}.\n"
            "Return ONLY a JSON array. Each item must have: "
            "id, title, company, location, remote (bool), "
            f"posted_date (ISO, within last 14 days from {date.today().isoformat()}), "
            "description (2 sentences), requirements (array 5-8 strings), "
            "salary_range, application_url, platform."
        )
        raw = self._chat(prompt)
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return DEMO_JOBS


# ── Factory ────────────────────────────────────────────────────────────────────

def get_provider(args) -> BaseProvider:
    if args.demo:
        console.print("[dim]Mode: Demo (no API key required)[/dim]")
        return DemoProvider()
    if args.ollama:
        console.print(f"[dim]Mode: Ollama local LLM (model: {args.model})[/dim]")
        return OllamaProvider(model=args.model)
    console.print("[dim]Mode: Anthropic Claude Opus 4.6[/dim]")
    return AnthropicProvider()
