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
    """Abstract base — all providers must implement these methods.

    `extract_profile` accepts an optional `heuristic_hint`: a profile dict
    pre-extracted by `pipeline.profile_extractor.scan_profile`. Providers
    that take an LLM should use it as a verified baseline (verify each
    field, correct mistakes, fill the gaps) instead of re-deriving from
    scratch — this is what eliminates the "boxes left blank" failure mode.
    Providers that ignore the hint fall back to their previous behavior.
    """

    def extract_profile(self, resume_text: str, preferred_titles: list = None,
                        heuristic_hint: dict = None) -> dict:
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

    def chat(self, system: str, messages: list, max_tokens: int = 1024,
             json_mode: bool = False) -> str:
        """Free-form conversational call. `messages` is [{role, content}, ...] with
        roles 'user' or 'assistant'. Used by Ask-Atlas and the resume-insights
        verifier. When `json_mode=True`, providers that support strict JSON
        output (Ollama via response_format, Anthropic via prefill) MUST honor
        it. Providers that can't degrade gracefully — they return whatever
        they normally would and the caller falls back to heuristics.
        """
        raise NotImplementedError


# ── Shared heuristic-priming block (Phase 1) ──────────────────────────────────
# Both Ollama and Anthropic providers prepend this block to their resume-
# parsing prompts. It hands the LLM the heuristic baseline so it can verify
# rather than re-derive every field — which is the failure mode the user
# described as "the boxes are not getting filled in".

def _build_heuristic_block(heuristic: dict | None) -> str:
    if not heuristic:
        return ""
    h = heuristic
    bits: list[str] = []
    contact = []
    for k in ("name", "email", "phone", "linkedin", "github", "location"):
        v = h.get(k)
        if v:
            contact.append(f"  {k:<10}: {v}")
    if contact:
        bits.append("[Contact — already extracted by regex; only correct if wrong]\n" + "\n".join(contact))
    skills = h.get("top_hard_skills") or []
    if skills:
        bits.append("[Hard skills found verbatim in the resume]\n  " + ", ".join(skills[:30]))
    edu = h.get("education") or []
    if edu:
        lines = []
        for e in edu[:5]:
            d = e.get("degree", "")
            i = e.get("institution", "")
            y = e.get("year", "")
            lines.append(f"  • {d} | {i} | {y}".strip())
        bits.append("[Education entries detected]\n" + "\n".join(lines))
    exp = h.get("experience") or []
    if exp:
        lines = []
        for e in exp[:6]:
            t = e.get("title", "")
            c = e.get("company", "")
            d = e.get("dates", "")
            lines.append(f"  • {t} | {c} | {d}".strip())
        bits.append("[Work / industry experience detected]\n" + "\n".join(lines))
    res = h.get("research_experience") or []
    if res:
        lines = []
        for e in res[:5]:
            t = e.get("title", "")
            c = e.get("company", "")
            d = e.get("dates", "")
            lines.append(f"  • {t} | {c} | {d}".strip())
        bits.append("[Research experience detected]\n" + "\n".join(lines))
    proj = h.get("projects") or []
    if proj:
        lines = []
        for p in proj[:6]:
            n = p.get("name", "")
            tools = ", ".join(p.get("skills_used") or [])[:80]
            lines.append(f"  • {n}" + (f"   ({tools})" if tools else ""))
        bits.append("[Projects detected]\n" + "\n".join(lines))
    if not bits:
        return ""
    return (
        "===== HEURISTIC BASELINE (do not duplicate, only verify/correct) =====\n"
        + "\n\n".join(bits)
        + "\n=====================================================================\n"
    )


# ── Shared rubric scorer (Phase 3) ─────────────────────────────────────────────
# Weighted categories: Required Skills 50 / Industry 30 / Location+Seniority 20.

RUBRIC_WEIGHTS = {"required_skills": 50, "industry": 30, "location_seniority": 20}


_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9+#.\-]+")


def _tokenize(text: str) -> set:
    """Lowercase token set with light normalization (handles c++, .net, node.js)."""
    if not text:
        return set()
    return {t for t in _TOKEN_SPLIT_RE.split(str(text).lower()) if t}


def compute_skill_coverage(job: dict, profile: dict) -> tuple[float, list, list]:
    """Deterministic skill-vs-job match — used both as the fast-score baseline
    AND as a grounded anchor for the LLM rubric scorer. Returns
    ``(coverage_0_to_1, matched_skills, missing_requirements)``.

    Result is cached on the job dict under ``_skill_coverage`` so Phase 3
    doesn't recompute it for the top-N jobs that get LLM-scored after the
    fast-score pass.
    """
    cached = job.get("_skill_coverage")
    if cached is not None:
        return cached

    skills = [str(s).strip() for s in (profile.get("top_hard_skills") or []) if s]
    skills_lower = {s.lower(): s for s in skills}
    if not skills_lower:
        result = (0.5, [], [])
        job["_skill_coverage"] = result
        return result

    reqs = [str(r).strip() for r in (job.get("requirements") or []) if r]
    # Reqs that look like ingestion seed strings ("[seed] internship role")
    # are noise — they pollute the haystack without telling us anything about
    # the actual JD. Drop them so coverage isn't dragged toward zero by a row
    # whose only "requirement" is the placeholder we wrote at fetch time.
    reqs = [r for r in reqs if not r.lower().startswith("[seed]")]
    title = str(job.get("title") or "").lower()
    # Include the title in the haystack. Most ingested rows store metadata
    # only — empty description, no real requirements — so the title is the
    # only signal we have. Without this, "FPGA Digital Design Verification
    # Intern" with no body matches nothing for a candidate whose top skill
    # is FPGA. Title weight is naturally bounded by token count vs body.
    full_haystack = " ".join([
        title,
        " ".join(reqs).lower(),
        str(job.get("description") or "").lower(),
    ]).strip()
    haystack_tokens = _tokenize(full_haystack)

    matched: list = []
    skill_tokens: set = set()
    for s_lower, s_orig in skills_lower.items():
        s_tokens = _tokenize(s_lower)
        skill_tokens |= s_tokens
        if not full_haystack:
            continue
        if s_lower in full_haystack or (s_tokens and s_tokens.issubset(haystack_tokens)):
            matched.append(s_orig)

    # Coverage = fraction of THIS JOB's stated requirements that the user
    # satisfies. Using len(skills) as the denominator (the previous behavior)
    # penalized broad profiles — a 30-skill candidate satisfying 3 of 3 reqs
    # scored 0.10, not 1.00, which crushed the entire downstream scale.
    missing: list = []
    if reqs:
        matched_reqs = 0
        for r in reqs:
            r_lower = r.lower()
            covered = False
            for s_lower in skills_lower:
                if s_lower in r_lower or r_lower in s_lower:
                    covered = True
                    break
            if not covered:
                r_tokens = {t for t in _tokenize(r_lower) if len(t) >= 3}
                if r_tokens and any(
                    r_tokens & {t for t in _tokenize(s) if len(t) >= 3}
                    for s in skills_lower
                ):
                    covered = True
            if covered:
                matched_reqs += 1
            else:
                missing.append(r.title())
        coverage = matched_reqs / len(reqs)
    elif full_haystack:
        # No structured reqs — saturate at ~5 distinct user-skill mentions in
        # the title/description so jobs without tags aren't pinned to 0.
        coverage = min(1.0, len(matched) / 5.0) if matched else 0.1
    else:
        coverage = 0.3

    # Title-match boost. A skill appearing in the JOB TITLE is a much
    # stronger signal than the same skill buried in a long description —
    # an "FPGA Verification Engineer" posting is essentially announcing
    # "we want someone who knows FPGA". Floor the coverage at 0.5 for a
    # single title match, scaling up to ~0.85 for three.
    if title and matched:
        title_hits = sum(1 for s in matched if s.lower() in title)
        if title_hits:
            coverage = max(coverage, min(0.85, 0.5 + 0.15 * (title_hits - 1) + 0.15))

    result = (coverage, matched[:8], missing[:8])
    job["_skill_coverage"] = result
    return result


def _build_rubric_result(job: dict, req_raw: float, industry_raw: float,
                          loc_seniority_raw: float, *,
                          matched: list = None, missing: list = None,
                          reasoning: str = "") -> dict:
    """Clamp sub-scores, compute weighted total, and assemble the standard
    rubric result dict consumed by Phase 3 and the tracker."""
    def _clamp(x: float) -> float:
        try:
            return max(0.0, min(1.0, float(x)))
        except (TypeError, ValueError):
            return 0.0
    req_raw = _clamp(req_raw)
    industry_raw = _clamp(industry_raw)
    loc_seniority_raw = _clamp(loc_seniority_raw)
    pts_skills = round(req_raw * RUBRIC_WEIGHTS["required_skills"])
    pts_ind    = round(industry_raw * RUBRIC_WEIGHTS["industry"])
    pts_loc    = round(loc_seniority_raw * RUBRIC_WEIGHTS["location_seniority"])
    total = max(0, min(100, pts_skills + pts_ind + pts_loc))
    if not reasoning:
        reasoning = (
            f"Skills {int(req_raw*100)}%, industry {int(industry_raw*100)}%, "
            f"location/seniority {int(loc_seniority_raw*100)}%."
        )
    return {
        "job_id": job.get("id", ""),
        "score": total,
        "score_breakdown": {
            "required_skills":    {"raw": req_raw,          "weight": 50, "points": pts_skills},
            "industry":           {"raw": industry_raw,     "weight": 30, "points": pts_ind},
            "location_seniority": {"raw": loc_seniority_raw,"weight": 20, "points": pts_loc},
        },
        "reasoning": reasoning,
        "matching_skills": (matched or [])[:6],
        "missing_skills":  (missing or [])[:6],
        "reason": reasoning,  # back-compat
    }


# ── 1. Anthropic (Claude) ──────────────────────────────────────────────────────

class AnthropicProvider(BaseProvider):
    """Uses Claude Opus 4.7 via the Anthropic SDK.

    Launch-ready configuration (2026-05):
      • Model: claude-opus-4-7 — most capable, 1M context, 128K max output.
      • Adaptive thinking only (`budget_tokens` is removed on Opus 4.7).
      • `output_config.effort = "high"` — best balance of quality vs cost for
        extraction / scoring / tailoring; xhigh / max are options for the
        most intelligence-sensitive paths.
      • Prompt caching on the tailoring tool call — the resume skeleton +
        JD context are reused across analyze/generate within one session
        and across multiple jobs in the same run, so we mark the system
        content with `cache_control: ephemeral`.
      • JSON mode uses `output_config.format = json_object` (the legacy
        assistant-turn `{` prefill returns 400 on Opus 4.6+).
    """

    MODEL = "claude-opus-4-7"
    DEFAULT_EFFORT = "high"  # low | medium | high | xhigh | max (max is Opus-tier only)

    def __init__(self, api_key: str = None):
        import anthropic as _anthropic
        self.client = _anthropic.Anthropic(api_key=api_key)
        self.model = self.MODEL

    def _output_config(self, *, effort: str | None = None,
                       json_object: bool = False) -> dict:
        """Build `output_config` for messages.create. Effort is required-ish
        for Opus 4.7 (it now matters more than any prior Opus). Pass
        `json_object=True` to enforce JSON output without an assistant prefill."""
        cfg: dict = {"effort": effort or self.DEFAULT_EFFORT}
        if json_object:
            cfg["format"] = {"type": "json_object"}
        return cfg

    def chat(self, system: str, messages: list, max_tokens: int = 1024,
             json_mode: bool = False) -> str:
        # Anthropic SDK takes system as a top-level kwarg, not a message role.
        clean = [
            {"role": m["role"], "content": str(m.get("content", ""))}
            for m in (messages or [])
            if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
        ]
        if not clean:
            return ""
        kwargs: dict = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system or "",
            messages=clean,
            output_config=self._output_config(json_object=json_mode),
        )
        resp = self.client.messages.create(**kwargs)
        out_parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                out_parts.append(getattr(block, "text", "") or "")
        return "".join(out_parts).strip()

    def _tool_call(self, tool_def: dict, prompt: str,
                   max_tokens: int = 4096, thinking: bool = False,
                   *, system: str | list | None = None,
                   effort: str | None = None) -> dict:
        """Invoke a forced tool call. `system` may be a string OR a list of
        content blocks (use the latter to attach `cache_control` to a stable
        prefix). `effort` overrides the default (e.g. "xhigh" for tailoring)."""
        kwargs: dict = dict(
            model=self.model, max_tokens=max_tokens,
            tools=[tool_def],
            tool_choice={"type": "tool", "name": tool_def["name"]},
            messages=[{"role": "user", "content": prompt}],
            output_config=self._output_config(effort=effort),
        )
        if system is not None:
            kwargs["system"] = system
        if thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        resp = self.client.messages.create(**kwargs)
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        return {}

    def extract_profile(self, resume_text: str, preferred_titles: list = None,
                        heuristic_hint: dict = None) -> dict:
        from .profile_audit import DOMAIN_TITLE_FAMILIES

        tool = {
            "name": "save_profile",
            "description": "Save the extracted resume profile as structured data.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name":     {"type": "string"},
                    "email":    {"type": "string"},
                    "linkedin": {"type": "string"},
                    "github":   {"type": "string"},
                    "phone":    {"type": "string"},
                    "location": {"type": "string"},
                    "target_titles": {
                        "type": "array",
                        "description": (
                            "5–8 job titles that fit the candidate's actual "
                            "experience based on what's IN the resume. The "
                            "candidate may be in software, hardware, data, "
                            "design, marketing, sales, healthcare, finance, "
                            "education, operations, etc. Pick titles drawn "
                            "from THEIR background — never default to a "
                            "domain you assume. Every title MUST include an "
                            "`evidence` line quoted from the resume that "
                            "justifies it."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "title":    {"type": "string"},
                                "family":   {"type": "string",
                                             "description": "Coarse family label (e.g. 'Software Engineering', 'Marketing', 'Clinical')."},
                                "evidence": {"type": "string",
                                             "description": "Exact line from the resume that justifies this title."},
                            },
                            "required": ["title", "family", "evidence"],
                        },
                    },
                    "top_hard_skills": {
                        "type": "array",
                        "description": (
                            "Concrete, verifiable competencies the candidate "
                            "actually exercises — programming languages, "
                            "software / SaaS tools, frameworks, lab / fab / "
                            "clinical equipment, measurement techniques, "
                            "domain-specific methodologies. NEVER include "
                            "interpersonal traits. Scan EVERY section — "
                            "coursework, projects, work bullets, skills list — "
                            "and extract every concrete competency you see. "
                            "Completeness over brevity. Categories cover "
                            "every professional domain, not just hardware: "
                            "use whichever category best fits each skill."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "skill":    {"type": "string"},
                                "category": {
                                    "type": "string",
                                    "enum": [
                                        "programming_language",
                                        "software_tool",
                                        "framework_library",
                                        "data_platform",
                                        "simulation_environment",
                                        "fab_process",
                                        "lab_instrument",
                                        "measurement_technique",
                                        "hardware_platform",
                                        "design_tool",
                                        "marketing_platform",
                                        "sales_crm",
                                        "finance_accounting",
                                        "healthcare_clinical",
                                        "methodology",
                                        "other",
                                    ],
                                },
                                "evidence": {
                                    "type": "string",
                                    "description": "Exact substring from the resume where this skill appears.",
                                },
                            },
                            "required": ["skill", "category", "evidence"],
                        },
                    },
                    "top_soft_skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Behavioral/interpersonal traits ONLY (e.g. Teamwork, "
                            "Technical Writing, Project Management). NEVER include "
                            "lab techniques, instruments, software, or languages."
                        ),
                    },
                    "education": {
                        "type": "array",
                        "items": {"type": "object", "properties": {
                            "degree": {"type": "string"}, "institution": {"type": "string"},
                            "year":   {"type": "string"}, "gpa":         {"type": "string"},
                        }},
                    },
                    "research_experience": {
                        "type": "array",
                        "description": "Academic / lab / research roles — anything with a PI, lab, or research group. Keep SEPARATE from work_experience.",
                        "items": {"type": "object", "properties": {
                            "title":   {"type": "string"}, "company": {"type": "string"},
                            "dates":   {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                        }},
                    },
                    "work_experience": {
                        "type": "array",
                        "description": "Industry / internship / part-time jobs (non-research).",
                        "items": {"type": "object", "properties": {
                            "title":   {"type": "string"}, "company": {"type": "string"},
                            "dates":   {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                        }},
                    },
                    "experience": {
                        "type": "array",
                        "description": "Back-compat: union of research_experience + work_experience.",
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
                    "critical_analysis": {
                        "type": "string",
                        "description": "A 3-4 paragraph brutally honest and detailed critique of the resume. Analyze: 1. Impact & Quantified Achievements (or lack thereof), 2. Skill Density vs. Industry Standards, 3. Structural Clarity for ATS and Human Reviewers, 4. Specific high-value action items to land top-tier roles."
                    },
                },
                "required": ["name", "top_hard_skills", "top_soft_skills", "target_titles", "critical_analysis"],
            },
        }

        pref_hint = ""
        if preferred_titles:
            pref_hint = (
                f"\nThe candidate's stated preferences are: {', '.join(preferred_titles)}. "
                "Use these as a tiebreaker only when the resume itself doesn't "
                "obviously favor one direction."
            )

        heur_block = _build_heuristic_block(heuristic_hint)

        prompt = (
            "Parse this resume in THREE ORDERED PASSES. Do not skip passes.\n\n"
            f"{heur_block}\n"
            "PASS 1 — Section map:\n"
            "Identify and label Education, Research Experience, Work Experience, "
            "Projects, Skills, and Publications. Separate research roles (lab / PI / "
            "research group) from industry roles.\n\n"
            "PASS 2 — Hard-skill extraction (cross-domain — DO NOT assume EE/CS):\n"
            "Hard skills = concrete, verifiable competencies the candidate "
            "actually exercises: programming languages, software / SaaS tools, "
            "frameworks, lab / fab / clinical equipment, measurement techniques, "
            "domain-specific methodologies. The candidate may be in software, "
            "hardware, data, design, marketing, sales, healthcare, finance, "
            "education, operations, etc. — extract whatever's actually there.\n"
            "Soft skills = behavioral / interpersonal traits ONLY (teamwork, "
            "communication, project management). NEVER place tools, techniques, "
            "or platforms under soft skills.\n"
            "Scan the ENTIRE resume. For each hard skill, include the exact "
            "substring from the resume as `evidence`. Completeness > brevity.\n\n"
            "PASS 3 — Target titles (GROUNDED IN RESUME, NOT IN A WHITELIST):\n"
            "Infer 5–8 titles that match the candidate's actual experience "
            "based on what's IN the resume — their most recent role, their "
            "education program, their dominant skill stack, their projects.\n"
            "Do NOT default to a domain you assume. A candidate with "
            "marketing experience gets marketing titles. A nurse gets clinical "
            "titles. A data scientist gets data titles. A hardware engineer "
            "gets hardware titles. Match the resume.\n"
            "For the `family` field on each title, you MAY pick from this "
            "menu of common families (or supply your own if none fits):\n"
            f"  {chr(10).join('  - ' + f for f in DOMAIN_TITLE_FAMILIES)}\n"
            "Every suggested title MUST be justified by a specific line from "
            "the resume — put that line in the `evidence` field. The line "
            "should come from Work Experience, Research Experience, "
            "Education, or Projects, whichever is most representative."
            f"{pref_hint}\n\n"
            f"Resume:\n{resume_text}"
        )
        return self._tool_call(tool, prompt, thinking=True)

    def score_job(self, job: dict, profile: dict) -> dict:
        # Deterministic skill coverage — the LLM does NOT get to invent this.
        det_cov, det_matched, det_missing = compute_skill_coverage(job, profile)
        tool = {
            "name": "score_job",
            "description": "Judge industry alignment and location/seniority fit. Skill coverage is computed deterministically and provided as input — DO NOT recompute it.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "industry":           {"type": "number", "minimum": 0, "maximum": 1,
                                           "description": "How well the job's domain/industry aligns with the candidate's target field and background. 1.0 = exact target field, 0.5 = adjacent/transferable, 0.0 = unrelated."},
                    "location_seniority": {"type": "number", "minimum": 0, "maximum": 1,
                                           "description": "Combined fit of location AND seniority. 1.0 = remote OR matches candidate location AND seniority matches candidate's level. 0.5 = one mismatch. 0.0 = both mismatch."},
                    "reasoning":          {"type": "string",
                                           "description": "ONE sentence grounded in actual JD/profile content — cite a specific requirement, skill, or detail. Do not generalize."},
                },
                "required": ["industry", "location_seniority", "reasoning"],
            },
        }
        edu = (profile.get("education") or [{}])[0] if profile.get("education") else {}
        candidate_block = (
            f"  Skills (top): {', '.join((profile.get('top_hard_skills') or [])[:15])}\n"
            f"  Target titles: {', '.join(profile.get('target_titles') or [])}\n"
            f"  Education: {edu.get('degree', '?')} at {edu.get('institution', '?')} ({edu.get('dates', '')})\n"
            f"  Location preference: {profile.get('location') or 'unspecified'}"
        )
        desc = (job.get("description") or "")
        if len(desc) > 1200:
            desc = desc[:1200] + "…"
        job_block = (
            f"  Title: {job.get('title')}\n"
            f"  Company: {job.get('company')}\n"
            f"  Location: {job.get('location')} (remote={bool(job.get('remote'))})\n"
            f"  Experience level: {job.get('experience_level', 'unknown')}\n"
            f"  Requirements: {', '.join((job.get('requirements') or [])[:12]) or '(none listed)'}\n"
            f"  Description: {desc or '(none)'}"
        )
        prompt = (
            "You are scoring how well a job posting fits a candidate. Skill "
            "coverage has ALREADY been computed deterministically — don't redo it. "
            "Your job is to judge two qualitative dimensions:\n"
            "  - industry (0.0-1.0): how aligned is the job's domain with the candidate?\n"
            "  - location_seniority (0.0-1.0): location and seniority fit.\n"
            "Be strict: 1.0 means perfect fit, 0.5 means partially aligned, 0.0 means clearly off. "
            "The reasoning MUST cite a concrete requirement, skill, or detail from the JD — "
            "not generic phrases like 'good match'.\n\n"
            f"=== Deterministic skill coverage (FYI, do not change): "
            f"{det_cov:.2f} ({len(det_matched)} matched / {len(det_missing)} missing) ===\n\n"
            f"Candidate:\n{candidate_block}\n\n"
            f"Job:\n{job_block}"
        )
        raw = self._tool_call(tool, prompt, max_tokens=512)
        return _build_rubric_result(
            job,
            det_cov,
            raw.get("industry", 0.5),
            raw.get("location_seniority", 0.5),
            matched=det_matched,
            missing=det_missing,
            reasoning=raw.get("reasoning", ""),
        )

    def tailor_resume(self, job: dict, profile: dict, resume_text: str,
                      *, selected_keywords: list[str] | None = None,
                      source_format: str | None = None) -> dict:
        from .tailored_schema import default_v2

        text_node = {
            "type": "object",
            "properties": {
                "text":     {"type": "string"},
                "diff":     {"type": "string", "enum": ["unchanged", "modified", "added"]},
                "original": {"type": "string"},
            },
            "required": ["text"],
        }
        role_obj = {
            "type": "object",
            "properties": {
                "title":    {"type": "string"},
                "company":  {"type": "string"},
                "dates":    {"type": "string"},
                "location": {"type": "string"},
                "bullets":  {"type": "array", "items": text_node},
            },
        }
        generic = {
            "type": "object",
            "properties": {
                "title":   text_node,
                "detail":  text_node,
                "bullets": {"type": "array", "items": text_node},
            },
        }
        project = {
            "type": "object",
            "properties": {
                "name":        {"type": "string"},
                "description": text_node,
                "skills_used": {"type": "array", "items": text_node},
                "bullets":     {"type": "array", "items": text_node},
                "dates":       {"type": "string"},
                "url":         {"type": "string"},
            },
        }
        education = {
            "type": "object",
            "properties": {
                "institution": {"type": "string"},
                "degree":      {"type": "string"},
                "dates":       {"type": "string"},
                "gpa":         {"type": "string"},
                "notes":       {"type": "array", "items": text_node},
            },
        }
        skill_cat = {
            "type": "object",
            "properties": {
                "name":  {"type": "string"},
                "items": {"type": "array", "items": text_node},
            },
        }
        custom = {
            "type": "object",
            "properties": {
                "name":  {"type": "string"},
                "items": {"type": "array", "items": generic},
            },
        }

        tool = {
            "name": "tailored_resume_v2",
            "description": (
                "Return a complete TailoredResume v2 covering every section in the source resume. "
                "Each TextNode carries a diff marker — unchanged | modified | added — so the renderer "
                "can paint changes in green. Never fabricate dates / titles / companies / institutions / GPAs."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "schema_version":       {"type": "integer"},
                    "name":                 {"type": "string"},
                    "email":                {"type": "string"},
                    "phone":                {"type": "string"},
                    "linkedin":             {"type": "string"},
                    "github":               {"type": "string"},
                    "location":             {"type": "string"},
                    "website":              {"type": "string"},
                    "summary":              text_node,
                    "skills":               {"type": "array", "items": skill_cat},
                    "experience":           {"type": "array", "items": role_obj},
                    "projects":             {"type": "array", "items": project},
                    "education":            {"type": "array", "items": education},
                    "awards":               {"type": "array", "items": generic},
                    "certifications":       {"type": "array", "items": generic},
                    "publications":         {"type": "array", "items": generic},
                    "activities":           {"type": "array", "items": generic},
                    "leadership":           {"type": "array", "items": generic},
                    "volunteer":            {"type": "array", "items": generic},
                    "coursework":           {"type": "array", "items": generic},
                    "languages":            {"type": "array", "items": generic},
                    "custom_sections":      {"type": "array", "items": custom},
                    "section_order":        {"type": "array", "items": {"type": "string"}},
                    "ats_keywords_added":   {"type": "array", "items": {"type": "string"}},
                    "ats_keywords_missing": {"type": "array", "items": {"type": "string"}},
                    "ats_score_before":     {"type": "integer"},
                    "ats_score_after":      {"type": "integer"},
                },
                "required": ["name", "skills", "experience", "education", "section_order"],
            },
        }

        skeleton = default_v2(profile)
        sel = list(selected_keywords or [])
        declined = [
            r for r in (job.get("requirements") or [])
            if isinstance(r, str) and r and r not in sel
        ]
        skeleton_json = json.dumps(skeleton, ensure_ascii=False)
        if len(skeleton_json) > 12000:
            skeleton_json = skeleton_json[:12000] + "…"

        # ── System prompt: stable across all tailoring calls in a session →
        # mark with `cache_control: ephemeral` so the second + onward jobs
        # served by /api/resume/tailor read instead of write the prefix.
        system_blocks = [
            {
                "type": "text",
                "text": (
                    "You are tailoring resumes for specific job applications. "
                    "Output the COMPLETE TailoredResume v2 — every section the "
                    "candidate's source resume had — including Awards, "
                    "Publications, Coursework, Activities, Leadership, "
                    "Volunteer, Languages, and any custom sections. NEVER "
                    "fabricate. NEVER change titles, companies, dates, "
                    "institutions, degrees, or GPAs. For each user-selected "
                    "keyword: REPHRASE an existing bullet (diff=modified) "
                    "when it fits, else ADD a new bullet (diff=added) under "
                    "the most relevant role. Reorder bullets within each role "
                    "by JD relevance (no diff change for reorder alone). Set "
                    "diff=unchanged on every TextNode you did not modify."
                ),
                "cache_control": {"type": "ephemeral"},
            },
        ]

        prompt = (
            f"Tailor this resume for: {job.get('title','')} at {job.get('company','')}.\n\n"
            "INPUT — full structured profile (you must preserve every section):\n"
            f"{skeleton_json}\n\n"
            f"JD Requirements: {', '.join(job.get('requirements', []) or [])}\n"
            f"JD Description: {(job.get('description') or '')[:2000]}\n\n"
            f"USER-SELECTED keywords to weave in: "
            f"{', '.join(sel) if sel else '(none — default to all must-have JD keywords missing from the resume)'}\n"
            f"USER-DECLINED keywords (do NOT include): {', '.join(declined[:20])}\n\n"
            f"Source format hint: {source_format or 'pdf'}\n"
        )
        # `xhigh` for tailoring — best for agentic / long-output rewriting on Opus 4.7.
        # 12288 max_tokens — Opus 4.7 token counting differs vs 4.6, give headroom.
        return self._tool_call(
            tool, prompt, max_tokens=12288, thinking=True,
            system=system_blocks, effort="xhigh",
        )

    def generate_cover_letter(self, job: dict, profile: dict) -> str:
        name = profile.get("name") or OWNER_NAME
        resp = self.client.messages.create(
            model=self.model, max_tokens=1024,
            messages=[{"role": "user", "content": (
                f"Write a 3-paragraph cover letter for {name} applying to "
                f"{job['title']} at {job['company']}.\n"
                "Para 1: Hook + role name. Para 2: Top 2-3 achievements mapped to JD. "
                "Para 3: Enthusiasm + call to action.\n"
                f"Candidate name: {name}\n"
                f"Candidate skills: {', '.join(profile.get('top_hard_skills', [])[:5])}\n"
                f"Candidate education: "
                f"{(profile.get('education') or [{}])[0].get('degree', '')} at "
                f"{(profile.get('education') or [{}])[0].get('institution', '')}\n"
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

# ── PDF / text extraction helpers (shared by all providers) ──────────────────

def _extract_name_from_text(text: str) -> str:
    """Three-tier name extraction from raw resume text.

    Tier 1 — spaCy NER (highest accuracy, optional):
        Uses the 'en_core_web_sm' model to find PERSON entities in the first
        ~300 characters. Skipped gracefully if spaCy is not installed.

    Tier 2 — First-line heuristic:
        The very first non-empty, non-contact line of a well-structured resume
        is almost always the candidate's name.  Applies strict sanity guards
        (no @, no digits, no URLs, no section-header / role-title words, 2–4
        tokens).

    Tier 3 — Scored window scan:
        Scans the first 30 lines with a confidence model:
        - higher score for lines appearing earlier
        - bonus for title-case or ALL-CAPS formatting (common in PDF headers)
        - must match a name-safe character pattern
        Best-scoring candidate wins.

    Falls back to "" if all three tiers fail (do NOT inject the placeholder
    OWNER_NAME — the upstream merger would treat the placeholder as truth,
    masking real extraction failures).
    """
    _SECTION_HEADERS = {
        "education", "experience", "skills", "projects", "objective",
        "summary", "profile", "about", "interests", "certifications",
        "publications", "awards", "references", "contact",
        "technical skills", "core competencies", "work experience",
        "professional experience", "research experience",
        "volunteer", "activities", "languages", "coursework",
        "achievements", "honors", "leadership",
    }
    # Words that almost always mark a job title or institution line — never
    # a person name. If a candidate line contains any of these, reject.
    _ROLE_OR_INSTITUTION_RE = re.compile(
        r"\b(?:engineer|developer|analyst|manager|director|scientist|"
        r"researcher|architect|consultant|designer|specialist|associate|"
        r"officer|coordinator|technician|fellow|assistant|administrator|"
        r"intern|internship|nurse|paralegal|accountant|teacher|tutor|"
        r"professor|trader|recruiter|operator|representative|"
        r"university|college|institute|school|academy|department|"
        r"corporation|company|inc|llc|ltd|gmbh)\b",
        re.IGNORECASE,
    )
    _BAD_RE = re.compile(
        r'[@/\\]|https?://|www\.|\.com|\.edu|\.org|\.io|\.net|'
        r'\d{3}[\s.\-]\d{3,4}|'          # phone fragments
        r'\b(?:gpa|grade|phone|tel|fax|email|cv|resume|curriculum)\b',
        re.I,
    )
    # Name-safe: each word must match one of:
    #   Title-case word          — Jane, Smith, Van
    #   Irish/hyphenated         — O'Brien, D'Angelo, Jean-Paul
    #   ALL-CAPS word            — JOHN, WILLIAMS
    #   Single initial (± dot)   — A, J, M.
    # Latin-extended ranges so accented names ("L\u00f3pez", "M\u00fcller", "Ji\u0159\u00ed")
    # match the title-case patterns just like ASCII names do.
    _UPPER = r"A-Z\u00c0-\u00d6\u00d8-\u00de\u0100-\u017f"
    _LOWER = r"a-z\u00df-\u00f6\u00f8-\u00ff\u0100-\u017f"
    _WORD_RE = re.compile(
        rf"^(?:"
        rf"[{_UPPER}][{_LOWER}\-]+"                                  # plain title-case
        rf"|[{_UPPER}]['\u2019][{_UPPER}][{_LOWER}]+"                # O'Brien / O\u2019Brien
        rf"|[{_UPPER}][{_LOWER}]*\-[{_UPPER}][{_LOWER}]+"            # hyphenated
        rf"|[{_UPPER}]{{2,}}"                                         # ALL-CAPS
        rf"|[{_UPPER}]\.?"                                            # single initial
        rf")$"
    )

    def _name_re_match(line: str) -> bool:
        words = line.split()
        if not (2 <= len(words) <= 4):
            return False
        return all(_WORD_RE.match(w) for w in words)

    _NAME_RE = _name_re_match  # callable, same interface as re.match

    # Two-word title-case "city-shaped" lines that match _NAME_RE but are
    # never personal names. Cities fronting a sidebar contact block (Colin's
    # CV puts "Hong Kong" four lines under CONTACT) are the canonical
    # collision; the section-aware rejection below handles the general case,
    # this list is belt-and-suspenders for resumes whose section markers got
    # stripped during PDF text extraction.
    _CITY_BLACKLIST = {
        s.lower() for s in (
            "Hong Kong", "New York", "Los Angeles", "San Francisco",
            "San Diego", "San Jose", "Las Vegas", "New Delhi", "New Orleans",
            "Mexico City", "Tel Aviv", "Cape Town", "Buenos Aires",
            "Sao Paulo", "Rio de Janeiro", "Hong Kong SAR", "Kuala Lumpur",
            "Saudi Arabia", "South Korea", "South Africa", "United Kingdom",
            "United States", "United Arab Emirates", "New Zealand",
            "Czech Republic", "Costa Rica", "Puerto Rico", "El Salvador",
        )
    }

    def _line_is_name_safe(line: str) -> bool:
        """Hard rejects regardless of which tier is checking."""
        if _BAD_RE.search(line):
            return False
        if _ROLE_OR_INSTITUTION_RE.search(line):
            return False
        if line.lower().rstrip(":.,") in _SECTION_HEADERS:
            return False
        if line.strip().rstrip(",.;:|").lower() in _CITY_BLACKLIST:
            return False
        return _NAME_RE(line)

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return ""

    # Build a per-line section label. The "header" zone is everything BEFORE
    # the first recognised section header. Lines after that point sit inside
    # CONTACT / SKILLS / EDUCATION / etc. and are NOT eligible to be the name —
    # this is what kills "Hong Kong" winning over the actual name in a sidebar
    # resume layout. If extraction is uncertain we'd rather return "" and let
    # the LLM merge fill the field than commit to a wrong guess.
    section_at: list[str] = []
    current_section = "header"
    for ln in lines:
        norm = ln.lower().rstrip(":.,;-").strip()
        if norm in _SECTION_HEADERS:
            current_section = norm
        section_at.append(current_section)

    # ── Tier 1: spaCy NER ────────────────────────────────────────────────────
    try:
        import spacy  # noqa: PLC0415
        try:
            nlp = spacy.load("en_core_web_sm")
        except OSError:
            nlp = None
        if nlp is not None:
            # Only run NER on the first ~300 chars; the name is always near the top.
            snippet = " ".join(lines[:10])[:300]
            doc = nlp(snippet)
            for ent in doc.ents:
                if ent.label_ == "PERSON" and len(ent.text.split()) >= 2:
                    candidate = ent.text.strip()
                    if _line_is_name_safe(candidate):
                        return candidate
    except ImportError:
        pass  # spaCy optional — move to next tier

    # ── Tier 2: first-line heuristic ─────────────────────────────────────────
    # The first non-empty line of a resume is the name ~80% of the time.
    if _line_is_name_safe(lines[0]):
        return lines[0]

    # ── Tier 3: scored window scan ────────────────────────────────────────────
    # Widened from 30 to 60 lines so sidebar resumes (where the name is drawn
    # in PDF order *after* the left-column CONTACT/EDUCATION blocks — like
    # Colin Tse's CV at line 41) still have a chance. The section-aware
    # rejection below keeps false-positives like "Hong Kong" or "Project
    # Management" from winning when they sit inside CONTACT/SKILLS blocks.
    candidates: list[tuple[int, str]] = []

    for i, line in enumerate(lines[:60]):
        if len(line) > 55:          # long lines are addresses / bullets
            continue
        if not _line_is_name_safe(line):
            continue
        # Only the "header" zone (before any section marker) is eligible.
        # Names inside CONTACT/SKILLS/etc. are virtually always false hits.
        if i < len(section_at) and section_at[i] != "header":
            continue

        score = max(0, 15 - i)                              # earlier → higher
        if line == line.title():
            score += 6                                       # Title Case bonus
        elif line == line.upper():
            score += 4                                       # ALL CAPS bonus
        if len(line.split()) == 3:
            score += 2                                       # first middle last
        candidates.append((score, line))

    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    return ""


def _extract_location_from_text(text: str) -> str:
    """Best-effort residence-location extraction from the resume header.

    Strategy:
      1. Restrict the search to the first 12 lines (header block) — anything
         later is almost certainly an Education/Experience location, not the
         candidate's residence.
      2. Skip any line that is part of an Education entry: lines that contain
         "university", "college", "institute", or a degree token.
      3. Try the inline contact-bar pattern first (City, ST | email | phone).
      4. Fall back to a "City, ST"/"City, State" / "City, Country" match
         in the same restricted header, again skipping institution lines.
      5. Last resort: a standalone state name on a contact-style line.
      6. Return empty string when nothing safely matches — never inject
         a hardcoded location.
    """
    _US_STATES = {
        "Alabama","Alaska","Arizona","Arkansas","California","Colorado",
        "Connecticut","Delaware","Florida","Georgia","Hawaii","Idaho",
        "Illinois","Indiana","Iowa","Kansas","Kentucky","Louisiana","Maine",
        "Maryland","Massachusetts","Michigan","Minnesota","Mississippi",
        "Missouri","Montana","Nebraska","Nevada","New Hampshire",
        "New Jersey","New Mexico","New York","North Carolina","North Dakota",
        "Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island",
        "South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont",
        "Virginia","Washington","West Virginia","Wisconsin","Wyoming",
        "District of Columbia",
    }
    _US_STATE_RE = (
        r'(?:' + '|'.join(re.escape(s) for s in _US_STATES) + r'|[A-Z]{2})'
    )
    bar_line_re = re.compile(
        r'\b([A-Z][a-z]+(?:[\s-][A-Z][a-z]+){0,2}),\s*' + _US_STATE_RE + r'\b'
    )
    # International: "City, Country" — explicit country list keeps us from
    # matching arbitrary "Word, Word" pairs (which previously matched things
    # like "Stanford, CA" and "Engineering, Inc").
    _COUNTRIES = (
        "Canada", "Mexico", "United Kingdom", "UK", "Ireland", "France",
        "Germany", "Spain", "Italy", "Portugal", "Netherlands", "Belgium",
        "Switzerland", "Austria", "Denmark", "Norway", "Sweden", "Finland",
        "Iceland", "Poland", "Czech Republic", "Hungary", "Romania", "Greece",
        "Turkey", "Israel", "United Arab Emirates", "UAE", "Saudi Arabia",
        "India", "Pakistan", "Bangladesh", "Nepal", "Sri Lanka",
        "China", "Japan", "South Korea", "Singapore", "Malaysia", "Thailand",
        "Vietnam", "Philippines", "Indonesia", "Australia", "New Zealand",
        "Brazil", "Argentina", "Chile", "Colombia", "Peru", "Mexico City",
        "South Africa", "Nigeria", "Kenya", "Egypt", "Morocco",
    )
    intl_re = re.compile(
        r'\b([A-Z][a-z]+(?:[\s-][A-Z][a-z]+){0,2}),\s*('
        + r'|'.join(re.escape(c) for c in _COUNTRIES)
        + r')\b'
    )
    institution_re = re.compile(
        r"\b(?:university|college|institute|institut|school|academy|"
        r"polytechnic|conservatory|seminary)\b",
        re.IGNORECASE,
    )
    degree_re = re.compile(
        r"\b(?:b\.?[as]\.?|bsc|m\.?[as]\.?|msc|m\.?eng|ph\.?d|doctorate|"
        r"bachelor|master|associate|diploma)\b",
        re.IGNORECASE,
    )

    def _line_is_institution(line: str) -> bool:
        # Education / school lines mention a city only as the school's city,
        # not the candidate's residence — skip them.
        return bool(institution_re.search(line) or degree_re.search(line))

    # Standalone metropolises that act as a complete location on their own
    # line — they don't need a "City, Country" comma. Sidebar-style PDFs
    # routinely list these alone (e.g. Colin's CV: "Hong Kong" sits one line
    # under his email). To avoid matching mid-sentence prose ("Worked in
    # Singapore"), Pass 4 below requires the standalone line to live within
    # a contact-block (email/phone/| within a few lines).
    _STANDALONE_CITIES = {
        "Hong Kong", "Singapore", "Macau", "Macao", "Dubai", "Abu Dhabi",
        "Doha", "Kuwait City", "Tel Aviv", "Tokyo", "Osaka", "Kyoto",
        "Seoul", "Busan", "Taipei", "Shanghai", "Beijing", "Shenzhen",
        "Bangkok", "Manila", "Jakarta", "Hanoi", "Ho Chi Minh City",
        "Mumbai", "Bangalore", "Bengaluru", "Delhi", "New Delhi",
        "Hyderabad", "Chennai", "Pune", "Kolkata",
        "Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne",
        "Paris", "Lyon", "Marseille", "Madrid", "Barcelona", "Valencia",
        "Rome", "Milan", "Naples", "Florence", "Vienna", "Zurich",
        "Geneva", "Brussels", "Amsterdam", "Rotterdam",
        "Prague", "Warsaw", "Krakow", "Budapest", "Bucharest",
        "Stockholm", "Oslo", "Copenhagen", "Helsinki", "Reykjavik",
        "Moscow", "Saint Petersburg", "Istanbul", "Athens", "Dublin",
        "Edinburgh", "Glasgow", "Manchester", "Birmingham", "London",
        "Sydney", "Melbourne", "Brisbane", "Perth", "Auckland",
        "Toronto", "Montreal", "Vancouver", "Ottawa", "Calgary",
        "Mexico City", "Guadalajara", "Monterrey",
        "Buenos Aires", "São Paulo", "Sao Paulo", "Rio de Janeiro",
        "Brasília", "Brasilia", "Bogotá", "Bogota", "Lima", "Santiago",
        "Cairo", "Lagos", "Nairobi", "Johannesburg", "Cape Town",
    }
    standalone_re = re.compile(
        r'^\s*(' + '|'.join(re.escape(c) for c in sorted(_STANDALONE_CITIES,
                                                          key=len, reverse=True)) + r')\s*$'
    )

    contact_signal = re.compile(r"[@|]|\d{3}[\s.\-]\d{3,4}", re.IGNORECASE)

    # Sidebar layouts (Colin's CV) push the contact block well past the first
    # 12 lines — extracted text reads main column first, then sidebar. 30
    # lines covers the common cases without venturing into experience prose.
    raw_header_lines = [l for l in text.splitlines()[:30] if l.strip()]
    header_lines = raw_header_lines

    # Pass 1: contact-bar — strongest signal because it sits next to email.
    for line in header_lines:
        if _line_is_institution(line):
            continue
        m = bar_line_re.search(line)
        if m:
            return m.group(0)

    # Pass 2: international city/country.
    for line in header_lines:
        if _line_is_institution(line):
            continue
        m = intl_re.search(line)
        if m:
            return m.group(0)

    # Pass 3: standalone state name on a contact-style line. We require the
    # line to look like contact info (has @ / phone / pipe) so we don't pull
    # "Worked at the California Institute of Technology" → "California".
    for line in header_lines:
        if _line_is_institution(line):
            continue
        if not contact_signal.search(line):
            continue
        for state in _US_STATES:
            if re.search(rf'\b{re.escape(state)}\b', line, re.I):
                return state

    # Pass 4: standalone metropolis on a line whose ±3-line neighborhood
    # contains a contact signal (email / phone / pipe). The neighborhood
    # gate is what stops "Worked in Tokyo on …" from registering.
    for i, line in enumerate(header_lines):
        if _line_is_institution(line):
            continue
        m = standalone_re.match(line)
        if not m:
            continue
        nearby = header_lines[max(0, i - 3):i + 4]
        if any(contact_signal.search(l) for l in nearby if l != line):
            return m.group(1)

    return ""


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

    # Map fuzzy header tokens to a canonical bucket name so callers can
    # request any synonym and find the matching content.
    _HEADER_ALIASES: dict = {
        "experience": "experience",
        "work experience": "experience",
        "professional experience": "experience",
        "relevant experience": "experience",
        "internship experience": "experience",
        "engineering experience": "experience",
        "career experience": "experience",
        "employment": "experience",
        "employment history": "experience",
        "industry experience": "experience",
        "work history": "experience",
        "career": "experience",
        "research experience": "research experience",
        "research": "research experience",
        "research projects": "research experience",
        "lab experience": "research experience",
        "laboratory experience": "research experience",
        "projects": "projects",
        "personal projects": "projects",
        "academic projects": "projects",
        "selected projects": "projects",
        "side projects": "projects",
        "education": "education",
        "academic background": "education",
        "academics": "education",
        "education and training": "education",
        "skills": "skills",
        "technical skills": "skills",
        "core competencies": "skills",
        "competencies": "skills",
        "coursework": "coursework",
        "relevant coursework": "coursework",
        "publications": "publications",
        "objective": "objective",
        "summary": "summary",
        "profile": "summary",
        "interests": "interests",
        "certifications": "certifications",
        "awards": "awards",
        "honors": "awards",
        "awards and honors": "awards",
    }

    @classmethod
    def _classify_header(cls, line: str) -> str | None:
        """Return the canonical section name for `line`, or None if not a header."""
        stripped = line.strip()
        if not stripped or len(stripped) > 60:
            return None
        # Strip trailing punctuation/colons and common decoration.
        cleaned = re.sub(r"[\s:_\-=•]+$", "", stripped).strip()
        if not cleaned:
            return None
        low = cleaned.lower()
        # Exact alias match
        if low in cls._HEADER_ALIASES:
            return cls._HEADER_ALIASES[low]
        # Permit decorated headers like "── EDUCATION ──" or "Education History"
        # by reducing to alphanumeric tokens and matching prefix.
        normalized = re.sub(r"[^a-z0-9 ]+", " ", low)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if normalized in cls._HEADER_ALIASES:
            return cls._HEADER_ALIASES[normalized]
        # All-caps heuristic: short lines that are mostly uppercase letters and
        # whose first word is a known alias root.
        is_caps = stripped == stripped.upper() and any(c.isalpha() for c in stripped)
        if is_caps and len(stripped) <= 40:
            first = normalized.split(" ", 1)[0] if normalized else ""
            for alias, canonical in cls._HEADER_ALIASES.items():
                if alias.startswith(first) and first:
                    return canonical
        return None

    @classmethod
    def _split_sections(cls, resume_text: str) -> dict:
        """Split a plain-text resume into {canonical_section_name: lines[]}.

        Section headers are detected via `_classify_header`. Lines before the
        first header land in the synthetic 'header' bucket.
        """
        sections: dict = {"header": []}
        current = "header"
        for raw in resume_text.splitlines():
            line = raw.rstrip()
            canonical = cls._classify_header(line) if line.strip() else None
            if canonical:
                current = canonical
                sections.setdefault(current, [])
            else:
                sections.setdefault(current, []).append(line)
        return sections

    @staticmethod
    def _parse_experience_block(lines: list) -> list:
        """Parse an EXPERIENCE section into [{title, company, dates, bullets}]."""
        roles: list = []
        cur: dict = None
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            indented = raw.startswith(("  ", "\t", "•", "  •", "- "))
            bullet_marker = line.startswith(("•", "-", "*"))
            if not indented and not bullet_marker and ("|" in line or any(
                ch.isdigit() for ch in line
            )):
                # New role header line e.g. "Title | Company | Dates"
                parts = [p.strip() for p in line.split("|")]
                title    = parts[0] if len(parts) > 0 else ""
                company  = parts[1] if len(parts) > 1 else ""
                dates    = parts[2] if len(parts) > 2 else ""
                cur = {"title": title, "company": company, "dates": dates, "bullets": []}
                roles.append(cur)
            else:
                text = line.lstrip("•-* ").strip()
                if not text:
                    continue
                if cur is None:
                    cur = {"title": "", "company": "", "dates": "", "bullets": []}
                    roles.append(cur)
                cur["bullets"].append(text)
        return roles

    # Tokens that very likely indicate a degree level on a line.
    _DEGREE_PATTERNS = re.compile(
        r"\b(?:"
        r"ph\.?d|d\.?phil|doctor(?:ate)?|"
        r"m\.?s\.?c?|m\.?eng|m\.?sc|m\.?phil|m\.?b\.?a|master(?:'s)?|"
        r"b\.?s\.?c?|b\.?eng|b\.?sc|b\.?a|bachelor(?:'s)?|"
        r"associate(?:'s)?|a\.?a\.?s?|"
        r"high school|h\.?s\.?\s*diploma|diploma|certificate"
        r")\b",
        re.IGNORECASE,
    )

    # Words that strongly suggest the line names an institution.
    _INSTITUTION_PATTERNS = re.compile(
        r"\b(?:university|college|institute|institut|school|academy|polytechnic"
        r"|conservatory|seminary)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _parse_projects_block(cls, lines: list) -> list:
        """Parse a PROJECTS section into [{name, description, bullets, skills_used, dates, url}].

        Heuristic:
          - A line that is NOT a bullet and that looks like a project title
            (Title Case, contains a separator like '|' / '—' / ':' / '@', or
            sits flush-left after a blank line) starts a new project.
          - Subsequent bullet lines (•, -, *, –) become entries in `bullets`.
          - The first non-bullet sentence after the title becomes `description`.
          - A trailing tech-tag list (Tools: …, Tech: …, Stack: …) populates
            `skills_used`.
          - Any GitHub/demo URL on the title line populates `url`.
          - Any date range on the title line populates `dates`.
        """
        date_re = re.compile(
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s*\d{0,4}"
            r"\s*[-–—]\s*(?:Present|Current|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s*\d{0,4}|\d{4})"
            r"|\b(?:19|20)\d{2}\s*[-–—]\s*(?:Present|Current|(?:19|20)\d{2})"
            r"|\b(?:19|20)\d{2}\b",
            re.IGNORECASE,
        )
        url_re = re.compile(r"https?://\S+|github\.com/\S+", re.IGNORECASE)
        tech_tag_re = re.compile(
            r"^\s*(?:tech(?:nologies)?|stack|tools|skills|languages)[\s:]+(.+)$",
            re.IGNORECASE,
        )

        def _looks_like_title(line: str, raw: str) -> bool:
            if line.startswith(("•", "-", "*", "–", "—", "·")):
                return False
            if raw.startswith(("  ", "\t")) and len(line) > 80:
                return False
            # Title-Case-ish: most letter-words start with uppercase, OR contains a
            # well-known separator pattern.
            if any(sep in line for sep in ("|", " — ", " – ", " - ", " @ ", " : ")):
                return True
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z'\-]*", line) if w]
            if not words:
                return False
            cap_ratio = sum(1 for w in words if w[0].isupper()) / len(words)
            return cap_ratio >= 0.6 and len(words) <= 12

        projects: list = []
        cur: dict | None = None

        def _new_project(title: str) -> dict:
            entry = {
                "name": title,
                "description": "",
                "bullets": [],
                "skills_used": [],
                "dates": "",
                "url": "",
            }
            # Pull URLs and dates out so the name stays clean.
            url_match = url_re.search(title)
            if url_match:
                entry["url"] = url_match.group(0).rstrip(".,;:")
            date_match = date_re.search(title)
            if date_match:
                entry["dates"] = date_match.group(0)
            cleaned = title
            for chunk in (entry["url"], entry["dates"]):
                if chunk:
                    cleaned = cleaned.replace(chunk, "")

            # Strip empty parens left over from date/url removal so they don't
            # become a "()" pseudo-tag downstream.
            cleaned = re.sub(r"\(\s*\)", "", cleaned)

            # Split on "|" / " — " / " – " to separate name from tag/date.
            # The first segment is the name; remaining segments that look
            # like tech-stack tokens become skills_used.
            season_re = re.compile(
                r"^(spring|summer|fall|autumn|winter)$", re.IGNORECASE
            )
            split_parts = [
                p.strip() for p in re.split(r"\s*[|—–]\s*", cleaned) if p.strip()
            ]
            if len(split_parts) > 1:
                cleaned = split_parts[0]
                for part in split_parts[1:]:
                    if not part:
                        continue
                    if "," in part or len(part.split()) <= 4:
                        for tok in re.split(r"[,/]+", part):
                            tok = tok.strip(" ()")
                            if not tok:
                                continue
                            if date_re.search(tok) or season_re.match(tok):
                                continue
                            entry["skills_used"].append(tok)
            cleaned = re.sub(r"[\s,|–—\-:()]+$", "", cleaned).strip()
            entry["name"] = cleaned or title
            return entry

        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            tag_match = tech_tag_re.match(line)
            if tag_match and cur is not None:
                tags = re.split(r"[;,/]+| - ", tag_match.group(1))
                cur["skills_used"].extend(t.strip() for t in tags if t.strip())
                continue
            bullet_marker = line[:1] in ("•", "-", "*", "–", "—", "·")
            if not bullet_marker and _looks_like_title(line, raw):
                cur = _new_project(line)
                projects.append(cur)
                continue
            text = line.lstrip("•-*–—· ").strip()
            if not text:
                continue
            if cur is None:
                cur = _new_project(text)
                projects.append(cur)
                continue
            if bullet_marker or cur["description"]:
                cur["bullets"].append(text)
            else:
                cur["description"] = text

        # Deduplicate skills_used per project.
        for p in projects:
            seen: set = set()
            ordered = []
            for s in p["skills_used"]:
                k = s.lower()
                if k and k not in seen:
                    seen.add(k)
                    ordered.append(s)
            p["skills_used"] = ordered

        return projects

    @classmethod
    def _parse_education_block(cls, lines: list) -> list:
        """Parse an EDUCATION section into [{degree, institution, year, gpa, location, coursework, honors}].

        Real-world resumes use a wide variety of formats. We split the section
        into one entry per "school chunk" — a contiguous block of non-empty
        lines separated by a blank line OR introduced by a fresh institution /
        degree line. Each entry is then post-processed to fill the structured
        fields.
        """
        # Pre-split the block into per-entry chunks separated by blank lines.
        chunks: list[list[str]] = []
        cur: list[str] = []
        for raw in lines:
            if raw.strip():
                cur.append(raw.strip())
            elif cur:
                chunks.append(cur)
                cur = []
        if cur:
            chunks.append(cur)

        # Heuristic re-split: only break a chunk when a new line repeats the
        # SAME primary field that's already been seen — e.g. a second
        # institution after the first one. Complementary lines (degree after
        # institution, or vice versa) stay in the same entry.
        refined_chunks: list[list[str]] = []
        for chunk in chunks:
            piece: list[str] = []
            piece_has_institution = False
            piece_has_degree = False
            for line in chunk:
                line_has_inst = bool(cls._INSTITUTION_PATTERNS.search(line))
                line_has_deg = bool(cls._DEGREE_PATTERNS.search(line))
                duplicate_field = (
                    (line_has_inst and piece_has_institution)
                    or (line_has_deg and piece_has_degree)
                )
                if piece and duplicate_field:
                    refined_chunks.append(piece)
                    piece = [line]
                    piece_has_institution = line_has_inst
                    piece_has_degree = line_has_deg
                else:
                    piece.append(line)
                    piece_has_institution = piece_has_institution or line_has_inst
                    piece_has_degree = piece_has_degree or line_has_deg
            if piece:
                refined_chunks.append(piece)

        entries: list = []
        for chunk in refined_chunks:
            entry = {
                "degree": "",
                "institution": "",
                "year": "",
                "gpa": "",
                "location": "",
                "coursework": [],
                "honors": [],
            }
            joined = " | ".join(chunk)

            # Extract year (graduation year or range)
            year_match = re.search(
                r"(?:(?:19|20)\d{2}\s*[-–—]\s*(?:Present|Current|(?:19|20)\d{2}))"
                r"|(?:19|20)\d{2}",
                joined,
                re.IGNORECASE,
            )
            if year_match:
                entry["year"] = year_match.group(0)

            # GPA — accept "GPA: 3.8", "GPA 3.8/4.0", "Cumulative GPA 3.85"
            gpa_match = re.search(
                r"GPA[\s:]*([0-4]\.\d{1,2})(?:\s*/\s*[0-4](?:\.\d+)?)?",
                joined,
                re.IGNORECASE,
            )
            if gpa_match:
                entry["gpa"] = gpa_match.group(1)

            # First pass: split "|" / "•" line-form (e.g. "Stanford University | B.S. EE | 2024")
            for line in chunk:
                parts = [p.strip() for p in re.split(r"\s*[|•]\s*", line) if p.strip()]
                if len(parts) >= 2:
                    for part in parts:
                        if not entry["degree"] and cls._DEGREE_PATTERNS.search(part):
                            entry["degree"] = part
                        elif not entry["institution"] and cls._INSTITUTION_PATTERNS.search(part):
                            entry["institution"] = part

            # Second pass: line-by-line classification when the pipe form failed.
            for line in chunk:
                low = line.lower()
                if low.startswith(("relevant coursework", "coursework")):
                    after = line.split(":", 1)[-1] if ":" in line else line
                    items = [c.strip() for c in re.split(r"[;,]+", after) if c.strip()
                             and not c.strip().lower().startswith("coursework")]
                    entry["coursework"].extend(items)
                    continue
                if low.startswith(("honors", "awards", "scholarship", "dean")):
                    after = line.split(":", 1)[-1] if ":" in line else line
                    items = [h.strip() for h in re.split(r"[;,]+", after) if h.strip()]
                    entry["honors"].extend(items)
                    continue
                if not entry["degree"] and cls._DEGREE_PATTERNS.search(line):
                    entry["degree"] = re.sub(r"\s{2,}", " ", line).strip()
                elif not entry["institution"] and cls._INSTITUTION_PATTERNS.search(line):
                    entry["institution"] = re.sub(r"\s{2,}", " ", line).strip()

            # Strip year/GPA artifacts out of degree/institution text.
            for field in ("degree", "institution"):
                if entry[field]:
                    cleaned = re.sub(
                        r"\bGPA[\s:]*[0-4]\.\d{1,2}(?:\s*/\s*[0-4](?:\.\d+)?)?\b",
                        "",
                        entry[field],
                        flags=re.IGNORECASE,
                    )
                    if entry["year"]:
                        cleaned = cleaned.replace(entry["year"], "")
                    entry[field] = re.sub(r"[\s,|]+$", "", cleaned).strip()

            # Fall back: if neither field matched, treat the first chunk line as
            # the degree (preserves data even when the format is unusual).
            if not entry["degree"] and not entry["institution"] and chunk:
                entry["degree"] = chunk[0]

            # Multi-line fallback: institutions like "MIT", "UCLA", "USC" are
            # acronyms and don't match _INSTITUTION_PATTERNS. If we have a
            # degree but no institution, scan the chunk for a short Title-
            # Case-or-acronym line that isn't itself a degree/coursework/honors.
            if entry["degree"] and not entry["institution"]:
                for line in chunk:
                    s = line.strip()
                    if not s or len(s) > 80:
                        continue
                    if cls._DEGREE_PATTERNS.search(s):
                        continue
                    low = s.lower()
                    if low.startswith(("relevant coursework", "coursework",
                                        "honors", "awards", "scholarship", "dean",
                                        "gpa")):
                        continue
                    # Strip trailing date / GPA so "MIT, 2024" → "MIT".
                    candidate = re.sub(r",?\s*(?:19|20)\d{2}.*$", "", s).strip()
                    candidate = re.sub(r",?\s*GPA[\s:].*$", "", candidate, flags=re.I).strip()
                    if not candidate or len(candidate) > 60:
                        continue
                    # Accept lines that are mostly uppercase (acronym) or that
                    # are short Title Case (≤4 words) — the typical pattern
                    # for institution-only lines above the degree.
                    is_acronym = candidate.isupper() and 2 <= len(candidate) <= 12
                    words = candidate.split()
                    is_title = (
                        1 <= len(words) <= 5
                        and sum(1 for w in words if w[:1].isupper()) >= max(1, len(words) - 1)
                    )
                    if is_acronym or is_title:
                        entry["institution"] = candidate
                        break

            # Skip pure noise entries.
            if any(entry[f] for f in ("degree", "institution", "year", "gpa")):
                entries.append(entry)

        return entries

    def _skills_from_text(self, text: str, limit: int = 40) -> list:
        """Scan *text* for tokens from the YAML lexicon, with word-boundary
        matching so short tokens don't false-positive inside larger words
        ('git' must not match in 'github', 'spice' must not match in
        'spice up', 'rie' must not match in 'enterprise')."""
        if not text:
            return []
        found: list[str] = []
        for s in self.SKILL_KEYWORDS:
            tok = (s or "").strip()
            if not tok or len(tok) < 2:
                continue
            if re.search(r"\W", tok):
                pattern = rf"(?<!\w){re.escape(tok)}(?!\w)"
            else:
                pattern = rf"\b{re.escape(tok)}\b"
            if re.search(pattern, text, re.IGNORECASE):
                found.append(tok)
        skill_display = {
            "verilog": "Verilog", "vhdl": "VHDL", "fpga": "FPGA", "spice": "SPICE",
            "matlab": "MATLAB", "python": "Python", "java": "Java", "latex": "LaTeX",
            "photolithography": "Photolithography", "cleanroom": "Cleanroom Processes",
            "pld": "Pulsed Laser Deposition", "cmos": "CMOS", "pcb": "PCB Design",
            "onshape": "OnShape", "fusion360": "Fusion360", "solidworks": "SolidWorks",
            "cad": "CAD", "linux": "Linux", "c++": "C++",
            "pulsed laser deposition": "Pulsed Laser Deposition",
            "thin film": "Thin Film Deposition", "sem": "SEM", "afm": "AFM",
            "digital design": "Digital Design", "analog design": "Analog Design",
            "mixed-signal": "Mixed-Signal",
        }
        out = []
        seen = set()
        for skill in found:
            label = skill_display.get(skill.lower(), skill.title())
            if label.lower() not in seen:
                seen.add(label.lower())
                out.append(label)
        return out[:limit]

    @staticmethod
    def _summary_from_profile(name: str, titles: list, skills: list, experience: list, research: list) -> str:
        role = titles[0] if titles else "candidate"
        skill_text = ", ".join(skills[:6])
        count = len(experience or []) + len(research or [])
        base = f"{name} is a {role}"
        if skill_text:
            base += f" with hands-on experience across {skill_text}"
        if count:
            base += f" and {count} structured resume role(s) extracted for job matching"
        return base + "."

    @staticmethod
    def _skills_from_skills_section(sections: dict, limit: int = 30) -> list[str]:
        """Field-agnostic fallback: when the YAML lexicon misses everything
        (e.g. a marketing/finance/healthcare resume in demo mode), pull tokens
        directly from the resume's own Skills section.

        Splits on commas, semicolons, slashes, pipes, and bullet markers so
        "SEO, Salesforce, HubSpot · Tableau" yields four entries. Strips
        category-label prefixes like "Tools:" / "Languages:" so they don't
        leak into the skill list.

        Returns an empty list when no Skills section exists — callers MUST
        accept that and not synthesize hardware-flavored placeholders.
        """
        lines = sections.get("skills") or []
        if not lines:
            return []
        out: list[str] = []
        seen: set[str] = set()
        label_prefix = re.compile(
            r"^\s*(?:tools?|languages?|frameworks?|technologies|tech|stack|skills?|"
            r"software|hardware|libraries|platforms?|databases?|cloud|other)\s*[:\-–]\s*",
            re.IGNORECASE,
        )
        for raw in lines:
            line = label_prefix.sub("", str(raw or "")).strip()
            line = line.lstrip("•-*–—· ").strip()
            if not line:
                continue
            for tok in re.split(r"[,;|/]+|\s•\s|\s·\s", line):
                t = tok.strip().strip(".").strip()
                # Ignore very long / very short shards.
                if not t or len(t) < 2 or len(t) > 40:
                    continue
                # Drop trailing parenthetical years / proficiencies.
                t = re.sub(r"\s*\([^)]*\)\s*$", "", t).strip()
                if not t:
                    continue
                key = t.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(t)
                if len(out) >= limit:
                    return out
        return out

    def extract_profile(self, resume_text: str, preferred_titles: list = None,
                        heuristic_hint: dict = None) -> dict:  # noqa: ARG002
        text_lower = resume_text.lower()

        email_match = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}', resume_text)
        email = email_match.group() if email_match else ""

        linkedin_match = re.search(r'linkedin\.com/in/[\w-]+', resume_text, re.I)
        linkedin = linkedin_match.group() if linkedin_match else ""

        github_match = re.search(r'github\.com/[\w-]+', resume_text, re.I)
        github = github_match.group() if github_match else ""

        phone_match = re.search(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', resume_text)
        phone = phone_match.group() if phone_match else ""

        # Pre-split sections so the skill-fallback path can see the resume's
        # own Skills block when the YAML lexicon doesn't recognise anything
        # (e.g. a marketing or finance resume in demo mode).
        sections = self._split_sections(resume_text)

        hard_skills = self._skills_from_text(resume_text)
        if not hard_skills:
            # Field-agnostic fallback. NEVER synthesize a hardcoded list — a
            # marketing resume should NOT come out the other side claiming
            # MATLAB/Verilog/FPGA expertise. Better to surface an empty
            # skill list and let the user know demo mode is field-narrow.
            hard_skills = self._skills_from_skills_section(sections)

        name = _extract_name_from_text(resume_text)
        location = _extract_location_from_text(resume_text)

        # Title inference — fires only on word-boundary matches against the
        # *extracted* hard skills (not raw text), so a resume that merely
        # mentions "spice" inside "spice up your campaigns" can't be tagged
        # as an IC Design candidate.
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
        skill_keys = {s.lower() for s in hard_skills}
        inferred = sorted({title_map[k] for k in title_map if k in skill_keys})
        seen: set = set()
        target_titles: list = []
        for t in (preferred_titles or []) + inferred:
            if t not in seen:
                seen.add(t)
                target_titles.append(t)
        # Empty target_titles is a valid state — Phase 2 falls back to the
        # user-configured `job_titles` setting. Never inject hardware
        # placeholders for non-hardware resumes.

        gaps = []
        if "summary" not in text_lower and "objective" not in text_lower:
            gaps.append("Missing professional summary/objective")
        if not re.search(r'\d+%|\d+ students|\d+ projects', resume_text):
            gaps.append("Few quantified achievements — add metrics")

        # ``sections`` was already computed above for the skill-fallback path.

        def _grab(*keys):
            for k in keys:
                if k in sections and sections[k]:
                    return sections[k]
            return []

        experience = self._parse_experience_block(
            _grab("experience", "work experience", "professional experience")
        )
        research = self._parse_experience_block(
            _grab("research experience", "research", "lab experience")
        )
        projects = self._parse_projects_block(
            _grab("projects", "personal projects", "academic projects")
        )
        for project in projects:
            text_for_skills = " ".join([
                project.get("name", ""),
                project.get("description", ""),
                " ".join(project.get("bullets") or []),
            ])
            inferred = self._skills_from_text(text_for_skills, limit=12)
            # Merge tag-derived skills (already on the project) with the
            # keyword-inferred ones; preserve order, dedupe case-insensitively.
            seen: set = set()
            merged: list = []
            for s in (project.get("skills_used") or []) + inferred:
                k = s.lower()
                if k and k not in seen:
                    seen.add(k)
                    merged.append(s)
            project["skills_used"] = merged
        education_parsed = self._parse_education_block(_grab("education"))
        summary = self._summary_from_profile(name, target_titles, hard_skills, experience, research)
        critical = (
            "Impact: add numeric outcomes to the strongest bullets wherever possible. "
            "Skill density: the parser found the technical keywords listed in hard skills; add any missing tools, instruments, and methods explicitly. "
            "ATS structure: keep Education, Experience, Projects, and Skills as clear headings. "
            "Next actions: add LinkedIn, work authorization, target salary, and target titles to improve matching and autofill."
        )

        # Soft skills are extracted from the resume text itself (same lexicon
        # the heuristic uses upstream). NEVER hand back a hardcoded list — a
        # marketing resume that doesn't say "Teamwork" anywhere should not
        # come out the other side claiming Teamwork is one of the
        # candidate's top soft skills. Empty list is the right answer when
        # the lexicon doesn't see any of these tokens.
        from .profile_extractor import _scan_soft_skills
        soft_skills = _scan_soft_skills(resume_text)

        return {
            "name": name, "email": email, "linkedin": linkedin, "github": github, "phone": phone,
            "location": location,
            "summary": summary,
            "target_titles": target_titles,
            "top_hard_skills": hard_skills,
            "top_soft_skills": soft_skills,
            "education":  education_parsed,
            "experience": experience,
            "work_experience": experience,
            "research": research,
            "research_experience": research,
            "projects":   projects,
            "resume_gaps": gaps,
            "critical_analysis": critical,
        }

    def score_job(self, job: dict, profile: dict) -> dict:
        skills_lower = {s.lower() for s in profile.get("top_hard_skills", [])}
        reqs = [r.lower() for r in job.get("requirements", [])]
        matched = [r for r in reqs if any(s in r or r in s for s in skills_lower)]
        req_raw = (len(matched) / len(reqs)) if reqs else 0.5

        title_lower = job.get("title", "").lower()
        targets_l = [t.lower() for t in profile.get("target_titles", [])]
        industry_raw = 1.0 if any(
            any(w in title_lower for w in t.split()) for t in targets_l
        ) else 0.5

        loc = job.get("location", "").lower()
        remote_ok = job.get("remote", False)
        # Score location dynamically: remote always passes; "united states"
        # is treated as a broad positive signal. No hardcoded city/state.
        loc_raw = 1.0 if (remote_ok or "united states" in loc or "us" == loc.strip()) else 0.5
        exp_ok = job.get("experience_level", "internship") in ("internship", "entry-level")
        loc_seniority_raw = (loc_raw + (1.0 if exp_ok else 0.5)) / 2

        missing = [r.title() for r in reqs if r not in matched and len(r) > 3][:5]
        return _build_rubric_result(job, req_raw, industry_raw, loc_seniority_raw,
                                    matched=matched, missing=missing)

    def tailor_resume(self, job: dict, profile: dict, resume_text: str,
                      *, selected_keywords: list[str] | None = None,
                      source_format: str | None = None) -> dict:
        # Demo mode IS the heuristic — delegate to the shared v2 module so the
        # output matches what phase4_tailor_resume falls back to when the
        # configured LLM glitches.  Demo always returns a TailoredResume v2
        # dict; downstream callers (renderer + frontend) consume v2 directly.
        from .heuristic_tailor import heuristic_tailor_resume_v2
        return heuristic_tailor_resume_v2(
            job, profile, resume_text,
            selected_keywords=selected_keywords,
        )

    def generate_cover_letter(self, job: dict, profile: dict) -> str:
        name       = profile.get("name") or OWNER_NAME
        email      = profile.get("email") or ""
        skills_str = ", ".join(profile.get("top_hard_skills", [])[:3])
        edu        = (profile.get("education") or [{}])[0]
        degree     = edu.get("degree") or "Engineering"
        university = edu.get("institution") or "my university"
        top_reqs   = ", ".join((job.get("requirements") or [])[:3])
        sign_off   = f"{name}" + (f"\n{email}" if email else "")
        return (
            f"Dear {job['company']} Hiring Team,\n\n"
            f"I am writing to express my strong interest in the {job['title']} position "
            f"at {job['company']}. As a {degree} student at {university} with experience "
            f"in {skills_str}, I am eager to contribute to your team.\n\n"
            f"My technical background in {skills_str} maps directly to your listed "
            f"requirements ({top_reqs}). I have applied these skills through coursework, "
            f"research, and hands-on projects and am confident I can add value quickly.\n\n"
            f"I would welcome the opportunity to discuss how my background aligns with "
            f"{job['company']}'s goals. Thank you for your consideration.\n\n"
            f"Sincerely,\n{sign_off}"
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

    def chat(self, system: str, messages: list, max_tokens: int = 1024,
             json_mode: bool = False) -> str:                              # noqa: ARG002
        # Demo mode has no LLM. Return a clear, honest message so the UI degrades
        # gracefully instead of bubbling a NotImplementedError.
        return (
            "Demo mode doesn't include a live chat assistant — switch to Ollama "
            "in Settings to enable Ask Atlas. Local models on the Pi work on the "
            "Free tier; Pro unlocks the higher-quality cloud models. (Anthropic "
            "Claude is in active development and will land in Pro when it ships.) "
            "The job description and your profile are still loaded for scoring "
            "and tailoring; only the chat advisor is gated."
        )


# ── 3. Ollama (local LLM) ──────────────────────────────────────────────────────

class OllamaProvider(BaseProvider):
    """Uses an Ollama instance — server-side in production, localhost in dev.

    The Ollama URL is read from the OLLAMA_URL env var so deployments can
    point at a different host (e.g. a beefier machine on the Tailnet) without
    code changes. Defaults to localhost:11434 — which on the production RPi
    deployment is the RPi's own Ollama, not the visiting user's laptop.
    """

    def __init__(self, model: str = "smollm2:135m"):
        # Read OLLAMA_URL per-instance so tests can monkeypatch the env after
        # import. Class-body reads happen at module-import time and are
        # effectively frozen for the process lifetime.
        import os as _os
        self.OLLAMA_URL = _os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
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

        # Ollama Turbo cloud models (`*-cloud` tag) are proxied through the
        # local daemon to Ollama's hosted servers. They aren't always listed
        # by /api/tags — skip the local-existence check and let the actual
        # chat call surface any auth/model errors with a real message.
        tag = self.model.split(":", 1)[1] if ":" in self.model else ""
        if tag.endswith("cloud") or tag == "cloud" or self.model.endswith("-cloud"):
            return

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

    def chat(self, system: str, messages: list, max_tokens: int = 1024,
             json_mode: bool = False) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "openai package required for Ollama mode.  Run: pip install openai"
            ) from exc
        oc = OpenAI(base_url=f"{self.OLLAMA_URL}/v1", api_key="ollama", timeout=180)
        msgs: list = []
        if system:
            msgs.append({"role": "system", "content": str(system)})
        for m in (messages or []):
            if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip():
                msgs.append({"role": m["role"], "content": str(m["content"])})
        if len(msgs) == (1 if system else 0):
            return ""
        kwargs: dict = {"model": self.model, "messages": msgs,
                        "max_tokens": max_tokens}
        if json_mode:
            # Ollama's OpenAI-compatible endpoint accepts response_format on
            # recent builds; older builds error so we retry once without it.
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = oc.chat.completions.create(**kwargs)
        except Exception as exc:
            if json_mode and "response_format" in str(exc):
                kwargs.pop("response_format", None)
                resp = oc.chat.completions.create(**kwargs)
            else:
                raise
        return (resp.choices[0].message.content or "").strip()

    def _chat(self, prompt: str, json_mode: bool = False) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "openai package required for Ollama mode.  Run: pip install openai"
            ) from exc

        import time as _time
        oc = OpenAI(base_url=f"{self.OLLAMA_URL}/v1", api_key="ollama", timeout=180)
        kwargs: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            # Ollama's OpenAI-compatible endpoint honors response_format=json_object.
            kwargs["response_format"] = {"type": "json_object"}
        for attempt in range(5):
            try:
                resp = oc.chat.completions.create(**kwargs)
                return resp.choices[0].message.content or ""
            except Exception as e:
                if "429" in str(e) or "too many concurrent" in str(e).lower():
                    wait = 2 ** attempt
                    console.print(
                        f"  [yellow]⏳ Ollama busy — retrying in {wait}s "
                        f"(attempt {attempt+1}/5)…[/yellow]"
                    )
                    _time.sleep(wait)
                elif json_mode and "response_format" in str(e):
                    # Older Ollama builds may not support response_format — retry
                    # without it instead of failing.
                    kwargs.pop("response_format", None)
                else:
                    raise
        raise RuntimeError("Ollama rate limit: exceeded 5 retry attempts")

    def _parse_json(self, text: str, fallback: dict) -> dict:
        def _try(s: str) -> dict | None:
            try:
                obj = json.loads(s.strip())
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
            return None

        def _fix_and_try(s: str) -> dict | None:
            # Fix trailing commas before ] or } — extremely common Ollama mistake.
            fixed = re.sub(r',\s*([}\]])', r'\1', s)
            return _try(fixed)

        candidates = [
            text,
            re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.M),
            # strip everything before the first { and after the last }
            text[text.find('{'):text.rfind('}')+1] if '{' in text else '',
        ]
        for c in candidates:
            if not c:
                continue
            result = _try(c) or _fix_and_try(c)
            if result:
                return result

        # Last-resort: find the largest {...} block
        for m in re.finditer(r'\{', text):
            depth, i = 0, m.start()
            for j, ch in enumerate(text[i:], i):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        blob = text[i:j+1]
                        result = _try(blob) or _fix_and_try(blob)
                        if result:
                            return result
                        break
        return fallback

    def extract_profile(self, resume_text: str, preferred_titles: list = None,
                        heuristic_hint: dict = None) -> dict:
        from .profile_audit import DOMAIN_TITLE_FAMILIES

        pref_hint = ""
        if preferred_titles:
            pref_hint = f"\nCandidate's stated title preferences (tiebreaker only): {', '.join(preferred_titles)}\n"

        # Education and projects sit near the bottom of most resumes. The
        # previous 3000-char truncation cut them off entirely. We keep a
        # generous cap to stay under the model's context window but include
        # enough text that those sections survive.
        excerpt = resume_text if len(resume_text) <= 16000 else (
            resume_text[:14000] + "\n[...truncated...]\n" + resume_text[-2000:]
        )

        heur_block = _build_heuristic_block(heuristic_hint)

        prompt = (
            "You are verifying a resume parser's output. A heuristic regex pass "
            "has already extracted a baseline profile (shown below). Your job: "
            "VERIFY each field, CORRECT mistakes, and FILL IN missing fields by "
            "reading the resume. Keep correct values verbatim — do not paraphrase.\n\n"
            f"{heur_block}\n"
            "Return ONE JSON object with exactly these keys "
            "(use [] / \"\" for missing fields):\n"
            '{"name": str, "email": str, "linkedin": str, "github": str, '
            '"phone": str, "location": str,\n'
            ' "target_titles": [str], "top_hard_skills": [str], "top_soft_skills": [str],\n'
            ' "education": [{"degree": str, "institution": str, "year": str, "gpa": str, '
            '"location": str, "coursework": [str], "honors": [str]}],\n'
            ' "research_experience": [{"title": str, "company": str, "dates": str, "bullets": [str]}],\n'
            ' "work_experience":     [{"title": str, "company": str, "dates": str, "bullets": [str]}],\n'
            ' "projects": [{"name": str, "description": str, "bullets": [str], '
            '"skills_used": [str], "dates": str, "url": str}],\n'
            ' "resume_gaps": [str],\n'
            ' "critical_analysis": str}\n\n'
            "EDUCATION RULES:\n"
            "  • One entry per degree/program. Common formats include:\n"
            "      'B.S. in EE, Stanford University, 2024, GPA 3.85'\n"
            "      'Stanford University — B.S. Electrical Engineering — Aug 2020 – May 2024'\n"
            "      Multi-line: institution on line 1, degree on line 2, dates/GPA on line 3.\n"
            "  • degree: keep the full degree (e.g. 'B.S. in Electrical Engineering').\n"
            "  • institution: full school name only (no degree, no city).\n"
            "  • year: graduation year (4 digits) or date range if explicit.\n"
            "  • gpa: numeric only (e.g. '3.85'); empty string if absent.\n"
            "  • coursework: bullet list under 'Relevant Coursework' if present.\n"
            "  • honors: scholarships, dean's list, awards under that program.\n"
            "  • Include EVERY school listed (undergrad + grad + study abroad).\n\n"
            "PROJECTS RULES:\n"
            "  • One entry per project. The project header line is usually a Title-Case "
            "name; following bullets / sentences describe it.\n"
            "  • name: the project title only (strip dates, tech stack, links).\n"
            "  • description: a one-sentence summary if present at the start of the entry.\n"
            "  • bullets: each bullet/achievement line as a separate string. Do NOT "
            "concatenate them into description.\n"
            "  • skills_used: technical nouns from the bullets — languages, frameworks, "
            "instruments, methods. Pull from the same project's text only.\n"
            "  • dates: any time range present on the project header.\n"
            "  • url: any GitHub/demo link tied to the project.\n"
            "  • Include EVERY project — personal, academic, course, hackathon.\n\n"
            "SKILLS RULES:\n"
            "  • Hard skills = technical nouns only (languages, tools, equipment, methods).\n"
            "  • Soft skills = behavioral traits only (teamwork, communication).\n"
            "  • Never put lab techniques or software under soft skills.\n\n"
            "TARGET TITLES (ground in the resume, NOT a fixed whitelist):\n"
            "  • Infer 5–8 titles that fit the candidate's actual experience.\n"
            "  • Pick from THEIR background — software / hardware / data / "
            "design / marketing / sales / healthcare / finance / education / "
            "operations / legal — whatever's in the resume.\n"
            "  • Use the recent work role + dominant skills + degree as cues.\n"
            "  • Common families to pick from (use your own if none fits):\n"
            f"      {', '.join(DOMAIN_TITLE_FAMILIES[:18])}\n"
            f"      {', '.join(DOMAIN_TITLE_FAMILIES[18:])}\n"
            f"{pref_hint}"
            "\ncritical_analysis: 3-4 paragraph honest critique covering impact & "
            "quantified achievements, skill density, ATS/structural clarity, and "
            "specific high-value action items.\n\n"
            "Return ONLY the JSON object — no prose, no markdown fences.\n\n"
            f"Resume:\n{excerpt}"
        )
        raw = self._chat(prompt, json_mode=True)
        result = self._parse_json(raw, {})
        if not result:
            console.print(
                "  [yellow]⚠  Ollama JSON parse failed — falling back to the heuristic baseline[/yellow]"
            )
            # The heuristic baseline has already been computed by the caller;
            # if it's missing we recompute on the spot so we never return {}.
            if heuristic_hint:
                return dict(heuristic_hint)
            return DemoProvider().extract_profile(
                resume_text, preferred_titles=preferred_titles,
            )
        return result

    def score_job(self, job: dict, profile: dict) -> dict:
        # Deterministic skill coverage — keep the LLM out of this.
        det_cov, det_matched, det_missing = compute_skill_coverage(job, profile)

        edu = (profile.get("education") or [{}])[0] if profile.get("education") else {}
        desc = (job.get("description") or "")
        if len(desc) > 1000:
            desc = desc[:1000] + "…"

        prompt = (
            "Score how well a job posting fits a candidate. Output ONLY a JSON "
            "object with these EXACT keys:\n"
            '  "industry": float 0.0-1.0 (domain/field alignment)\n'
            '  "location_seniority": float 0.0-1.0 (location + seniority fit)\n'
            '  "reasoning": one sentence citing a CONCRETE requirement or skill from the JD.\n\n'
            "Be strict. 1.0 = exact match, 0.5 = partial, 0.0 = clearly off. "
            "Skill coverage is already computed deterministically — don't include it.\n\n"
            f"Deterministic skill coverage (for context): {det_cov:.2f} "
            f"({len(det_matched)} matched, {len(det_missing)} missing)\n\n"
            "Candidate:\n"
            f"  Skills: {', '.join((profile.get('top_hard_skills') or [])[:12])}\n"
            f"  Target titles: {', '.join(profile.get('target_titles') or [])}\n"
            f"  Education: {edu.get('degree','?')} @ {edu.get('institution','?')}\n"
            f"  Location: {profile.get('location') or 'unspecified'}\n\n"
            "Job:\n"
            f"  Title: {job.get('title')}\n"
            f"  Company: {job.get('company')}\n"
            f"  Location: {job.get('location')} (remote={bool(job.get('remote'))})\n"
            f"  Experience level: {job.get('experience_level','unknown')}\n"
            f"  Requirements: {', '.join((job.get('requirements') or [])[:10]) or '(none)'}\n"
            f"  Description: {desc or '(none)'}\n"
        )
        raw = self._chat(prompt, json_mode=True)
        parsed = self._parse_json(raw, {
            "industry": 0.5, "location_seniority": 0.5,
            "reasoning": "Ollama returned non-JSON; using deterministic coverage only.",
        })
        # Clamp + grounding: even if the LLM returns garbage extremes, the
        # deterministic skill coverage owns the heaviest weight.
        return _build_rubric_result(
            job,
            det_cov,
            parsed.get("industry", 0.5),
            parsed.get("location_seniority", 0.5),
            matched=det_matched,
            missing=det_missing,
            reasoning=parsed.get("reasoning", ""),
        )

    def tailor_resume(self, job: dict, profile: dict, resume_text: str,
                      *, selected_keywords: list[str] | None = None,
                      source_format: str | None = None) -> dict:
        """Ollama: ask for v2 JSON via response_format json_object."""
        from .tailored_schema import default_v2

        skeleton = default_v2(profile)
        sel = list(selected_keywords or [])
        requirements = list(job.get("requirements") or [])
        declined = [
            r for r in requirements
            if isinstance(r, str) and r and r not in sel
        ]
        skeleton_json = json.dumps(skeleton, ensure_ascii=False)
        if len(skeleton_json) > 6000:
            skeleton_json = skeleton_json[:6000] + "…"

        system_msg = (
            "You are a resume tailoring assistant. Output ONLY valid JSON matching "
            "the TailoredResume v2 schema. Preserve every section from the input "
            "skeleton — do not drop any. Diff markers: 'unchanged' (default), "
            "'modified' (you rewrote the text), 'added' (new content). Never fabricate "
            "titles, companies, dates, institutions, degrees, or GPAs."
        )
        user_msg = (
            f"Tailor for: {job.get('title','')} at {job.get('company','')}\n\n"
            f"INPUT skeleton:\n{skeleton_json}\n\n"
            f"JD requirements: {', '.join(requirements)}\n"
            f"JD description: {(job.get('description') or '')[:1500]}\n\n"
            f"USER-SELECTED keywords: {', '.join(sel) or '(default to all missing must-haves)'}\n"
            f"USER-DECLINED keywords: {', '.join(declined[:15])}\n\n"
            "Return ONLY the full TailoredResume v2 JSON, no markdown, no commentary. "
            "Required top-level keys: schema_version, name, skills, experience, education, section_order."
        )
        prompt = system_msg + "\n\n" + user_msg
        raw = self._chat(prompt, json_mode=True)
        return self._parse_json(raw, {})

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
