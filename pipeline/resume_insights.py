"""
pipeline/resume_insights.py
───────────────────────────
Real (non-hardcoded) resume scanner + optional AI verification.

The scanner reads the resume *preview text* and computes deterministic
metrics: word/bullet counts, quantification rate, action-verb usage,
weak-phrase frequency, section coverage, skill density, etc. From those
metrics it derives strengths, red flags, and targeted suggestions, and
writes a content-aware critical narrative.

When the active LLM provider (Anthropic, Ollama, or any future BaseProvider
subclass that implements `chat`) is reachable, `verify_with_provider` sends
the heuristic findings + a resume excerpt to the model and asks it to
double-check each item, drop inaccurate observations, surface 1-3
insights only an experienced reviewer would catch, and rewrite the
narrative to reference specific lines from the resume. The verification
pass is best-effort — when the model is offline / errors / returns
malformed JSON, the heuristic output is shown as-is with a neutral
"AI verification unavailable" note.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from .config import console


INSIGHTS_VERSION = 1
# Server-side Ollama in production (RPi); localhost in dev. Honour OLLAMA_URL
# so the same code works whether Ollama is on this host or another Tailnet box.
import os as _os
OLLAMA_URL = _os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")


# ── Action-verb / weak-language vocabularies ──────────────────────────────────

STRONG_ACTION_VERBS: set[str] = {
    "accelerated", "accomplished", "achieved", "analyzed", "architected",
    "assembled", "audited", "authored", "automated", "benchmarked", "boosted",
    "built", "calculated", "captured", "characterized", "coded", "collaborated",
    "compiled", "composed", "conceived", "conducted", "configured",
    "constructed", "created", "decreased", "delivered", "demonstrated",
    "deployed", "derived", "designed", "developed", "diagnosed", "directed",
    "documented", "drove", "engineered", "established", "evaluated",
    "examined", "executed", "expanded", "extracted", "fabricated", "filed",
    "forecasted", "formulated", "founded", "generated", "guided", "headed",
    "identified", "implemented", "improved", "increased", "initiated",
    "instrumented", "integrated", "interpreted", "introduced", "invented",
    "investigated", "iterated", "launched", "led", "leveraged", "managed",
    "mapped", "measured", "mentored", "migrated", "modeled", "modernized",
    "monitored", "negotiated", "operated", "optimized", "orchestrated",
    "owned", "performed", "pioneered", "planned", "predicted", "prepared",
    "presented", "prioritized", "produced", "programmed", "prototyped",
    "published", "quantified", "ran", "rebuilt", "redesigned", "reduced",
    "refactored", "released", "researched", "resolved", "restructured",
    "reviewed", "rolled", "scaled", "scheduled", "scoped", "scripted",
    "shipped", "simulated", "solved", "sourced", "spearheaded", "specified",
    "standardized", "streamlined", "supervised", "synthesized", "taught",
    "tested", "trained", "transformed", "translated", "tuned", "uncovered",
    "validated", "verified", "won", "wrote",
}

WEAK_PHRASES: list[str] = [
    r"responsible for",
    r"duties? included",
    r"helped\s+(?:with|to|in|out)?",
    r"worked on",
    r"assisted\s+(?:with|in|by)?",
    r"in charge of",
    r"tasks? included",
    r"participated in",
    r"familiar with",
    r"was\s+tasked",
    r"helped\s+create",
    r"a team player",
    r"hard[-\s]?working",
    r"think outside the box",
    r"go-?getter",
]
_WEAK_RE = re.compile(r"\b(?:" + "|".join(WEAK_PHRASES) + r")\b", re.IGNORECASE)

# Quantification: percent, currency, k/m/b suffix, multipliers, multi-digit
# integers, or numbers paired with a unit recruiters scan for.
_QUANT_RE = re.compile(
    r"(?:"
    r"\d+\.?\d*\s*%"                                 # 35%
    r"|\$\s?\d"                                      # $1, $5K
    r"|\b\d+\.?\d*\s*[kKmMbB]\b"                     # 12K, 1.5M
    r"|\b\d+\.?\d*\s*x\b"                            # 3x, 1.5x
    r"|\b\d{2,}\b"                                   # any 2+ digit number
    r"|\b\d+\s*(?:hours?|days?|weeks?|months?|years?|"
    r"users?|customers?|projects?|students?|members?|teams?|"
    r"papers?|publications?|patents?|nm|µm|um|mm|GHz|MHz|kHz|"
    r"V|mV|W|mW|dB|fps|FPS|samples?|trials?|simulations?)\b"
    r")"
)

_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-•·*]|\d+[.)])\s+")
_SECTION_HEADER_RE = re.compile(
    r"^(?:education|experience|work\s+experience|professional\s+experience|"
    r"research|research\s+experience|lab\s+experience|skills|technical\s+skills|"
    r"projects?|publications?|awards?|honors?|certifications?|summary|"
    r"objective|profile|coursework|relevant\s+coursework|interests|activities|"
    r"leadership|volunteer)\s*:?\s*$",
    re.IGNORECASE,
)


# ── Scanner core ──────────────────────────────────────────────────────────────

def _split_bullets(text: str) -> list[str]:
    """Return cleaned bullet strings. Lines that look like role headers
    (``Title | Company | Dates`` with no bullet marker) are skipped."""
    bullets: list[str] = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        if _BULLET_PREFIX_RE.match(raw):
            cleaned = _BULLET_PREFIX_RE.sub("", raw).strip()
            if cleaned and len(cleaned.split()) >= 3:
                bullets.append(cleaned)
    return bullets


def _detect_sections(text: str) -> list[str]:
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip().rstrip(":")
        if not line:
            continue
        if _SECTION_HEADER_RE.match(raw.strip()):
            seen.add(line.lower())
        # ALL-CAPS short headers ("EDUCATION", "EXPERIENCE")
        elif (
            line == line.upper()
            and 3 <= len(line) <= 40
            and any(c.isalpha() for c in line)
            and len(line.split()) <= 4
        ):
            seen.add(line.lower())
    return sorted(seen)


def _scan_metrics(text: str, profile: dict) -> dict:
    text = text or ""
    words = re.findall(r"\b[\w'-]+\b", text)
    word_count = len(words)
    bullets = _split_bullets(text)
    bullet_count = len(bullets)

    quantified = sum(1 for b in bullets if _QUANT_RE.search(b))
    action_verbs = 0
    weak = 0
    bullet_lengths: list[int] = []
    repeated_starts: dict[str, int] = {}
    for b in bullets:
        bullet_lengths.append(len(b.split()))
        m = re.match(r"\W*(\w+)", b)
        if m:
            first = m.group(1).lower()
            repeated_starts[first] = repeated_starts.get(first, 0) + 1
            if first in STRONG_ACTION_VERBS:
                action_verbs += 1
        if _WEAK_RE.search(b):
            weak += 1

    avg_bullet_len = round(sum(bullet_lengths) / len(bullet_lengths)) if bullet_lengths else 0
    longest_bullet = max(bullet_lengths) if bullet_lengths else 0
    repetitive_verb = max(
        ((v, n) for v, n in repeated_starts.items() if n >= 3),
        key=lambda kv: kv[1],
        default=None,
    )

    sections = _detect_sections(text)
    skill_count = len(profile.get("top_hard_skills") or [])
    skill_density = round(skill_count / (word_count / 100), 1) if word_count else 0.0

    has_email = bool((profile.get("email") or "").strip())
    has_linkedin = bool((profile.get("linkedin") or "").strip())
    has_github = bool((profile.get("github") or "").strip())
    has_phone = bool((profile.get("phone") or "").strip())
    has_location = bool((profile.get("location") or "").strip())

    # Buzzword count (separate from weak-phrase regex which targets passive voice)
    buzzwords_re = re.compile(
        r"\b(?:synergy|leverage(?:d)?|holistic|paradigm|disruptive|"
        r"go-?getter|self-?starter|results-?driven|detail-?oriented|"
        r"team\s+player|hardworking|passionate)\b",
        re.IGNORECASE,
    )
    buzzword_count = len(buzzwords_re.findall(text))

    return {
        "word_count": word_count,
        "bullet_count": bullet_count,
        "quantified_count": quantified,
        "quantified_pct": round(100 * quantified / bullet_count) if bullet_count else 0,
        "action_verb_count": action_verbs,
        "action_verb_pct": round(100 * action_verbs / bullet_count) if bullet_count else 0,
        "weak_phrase_count": weak,
        "buzzword_count": buzzword_count,
        "avg_bullet_len": avg_bullet_len,
        "longest_bullet": longest_bullet,
        "section_count": len(sections),
        "sections": sections,
        "skill_density": skill_density,
        "skill_count": skill_count,
        "reading_seconds": max(1, round(word_count / 4)),  # ~240 wpm scanner pace
        "repeated_verb": list(repetitive_verb) if repetitive_verb else None,
        "has_email": has_email,
        "has_linkedin": has_linkedin,
        "has_github": has_github,
        "has_phone": has_phone,
        "has_location": has_location,
    }


# ── Insight derivation ────────────────────────────────────────────────────────

def _derive_strengths(m: dict, profile: dict) -> list[str]:
    out: list[str] = []
    if m["quantified_pct"] >= 50:
        out.append(
            f"{m['quantified_pct']}% of bullets contain hard numbers — "
            f"you're already past the 40% bar most resumes never clear."
        )
    if m["action_verb_pct"] >= 60:
        out.append(
            f"{m['action_verb_pct']}% of bullets open with assertive action verbs "
            f"({m['action_verb_count']}/{m['bullet_count']}); recruiters scan exactly "
            f"that signal in the first 6 seconds."
        )
    if m["weak_phrase_count"] == 0 and m["bullet_count"] >= 5:
        out.append(
            "Zero weak phrasings detected — no 'responsible for' or 'helped with' filler."
        )
    if m["section_count"] >= 5:
        out.append(
            f"Clear {m['section_count']}-section structure: "
            + ", ".join(s.title() for s in m["sections"][:5])
            + " — easy to skim and ATS-parser friendly."
        )
    if m["skill_density"] >= 6:
        out.append(
            f"Skill density of {m['skill_density']} keywords per 100 words is well above the "
            f"~5.0 average for technical resumes — strong ATS surface area."
        )
    if (profile.get("projects") or []) and len(profile["projects"]) >= 2:
        out.append(
            f"{len(profile['projects'])} project(s) listed — gives reviewers concrete artifacts "
            f"beyond job titles, especially valuable for early-career applicants."
        )
    if m["buzzword_count"] == 0 and m["bullet_count"] >= 5:
        out.append(
            "No clichés or buzzwords ('team player', 'self-starter', 'synergy') — "
            "writing reads as substance over polish."
        )
    if m["has_linkedin"] and m["has_github"]:
        out.append(
            "Both LinkedIn and GitHub linked — reviewers can verify identity and inspect work in two clicks."
        )
    return out[:6]


def _derive_red_flags(m: dict, profile: dict) -> list[str]:
    out: list[str] = []
    if m["bullet_count"] < 5:
        out.append(
            f"Only {m['bullet_count']} bullets detected — most resumes need 8-15 to "
            f"establish credibility. Are sections being missed by the parser?"
        )
    if m["quantified_pct"] < 30 and m["bullet_count"] >= 5:
        out.append(
            f"Only {m['quantified_pct']}% of bullets have numbers — without metrics, "
            f"impact reads as opinion, not evidence."
        )
    if m["action_verb_pct"] < 50 and m["bullet_count"] >= 5:
        out.append(
            f"Only {m['action_verb_pct']}% of bullets lead with strong action verbs; "
            f"weak openers bury the achievement."
        )
    if m["weak_phrase_count"] > 0:
        out.append(
            f"{m['weak_phrase_count']} bullet(s) start with passive language "
            f"('responsible for', 'helped with', 'worked on') — replace with concrete verbs."
        )
    if m["buzzword_count"] >= 2:
        out.append(
            f"{m['buzzword_count']} cliché term(s) detected — they make the resume sound generic."
        )
    if m["word_count"] < 250 and m["bullet_count"] > 0:
        out.append(
            f"At {m['word_count']} words, the resume is sparse — most internships expect 350-650."
        )
    elif m["word_count"] > 850:
        out.append(
            f"At {m['word_count']} words, this risks being one-page-overflow; reviewers skim, then stop."
        )
    if m["avg_bullet_len"] >= 26:
        out.append(
            f"Average bullet is {m['avg_bullet_len']} words long — aim for 12-20 so the result lands fast."
        )
    if m["longest_bullet"] >= 35:
        out.append(
            f"At least one bullet is {m['longest_bullet']} words — split it or trim by half."
        )
    if m["section_count"] < 4:
        out.append(
            f"Only {m['section_count']} sections detected — Education, Skills, Experience, "
            f"and Projects are baseline."
        )
    if not m["has_email"]:
        out.append("No email address detected at the top — recruiters can't reply.")
    if not m["has_linkedin"]:
        out.append("No LinkedIn URL — reviewers expect one to verify identity in seconds.")
    if m["repeated_verb"]:
        verb, count = m["repeated_verb"]
        out.append(
            f"'{verb.title()}' opens {count} different bullets — vary your verbs so each "
            f"achievement reads as distinct."
        )
    titles = [str(t).lower() for t in (profile.get("target_titles") or [])]
    skills_l = {str(s).lower() for s in (profile.get("top_hard_skills") or [])}
    if any(("ic " in t or "vlsi" in t or "asic" in t) for t in titles):
        if not any(k in skills_l for k in ("verilog", "systemverilog", "vhdl")):
            out.append(
                "Target titles include IC/VLSI but Verilog/SystemVerilog is missing from skills — "
                "expect ATS rejection on most postings."
            )
    if any("photonics" in t for t in titles):
        if not any(k in skills_l for k in ("photolithography", "lithography", "cleanroom",
                                           "lumerical", "comsol")):
            out.append(
                "Photonics target with no fab/sim tools listed (Photolithography, COMSOL, Lumerical) — "
                "add the ones you've actually used."
            )
    return out[:7]


def _derive_suggestions(m: dict, profile: dict) -> list[str]:
    out: list[str] = []
    if m["quantified_pct"] < 60 and m["bullet_count"] > 0:
        deficit = max(1, round((60 - m["quantified_pct"]) * m["bullet_count"] / 100))
        out.append(
            f"Quantify {deficit} more bullet(s): %, $, time saved, sample size, throughput, error rate, etc."
        )
    if m["weak_phrase_count"] > 0:
        out.append(
            "Re-anchor weak bullets with verbs like Engineered, Designed, Implemented, "
            "Optimized, Validated — never 'responsible for'."
        )
    if m["action_verb_pct"] < 70 and m["bullet_count"] >= 5:
        out.append("Lead every bullet with a unique action verb; never repeat the same one twice in a section.")
    if m["repeated_verb"]:
        verb, _ = m["repeated_verb"]
        out.append(
            f"Replace duplicate uses of '{verb.title()}' with synonyms (Engineered/Architected/Built/Designed)."
        )
    if m["word_count"] > 750:
        out.append("Trim the longest 3 bullets by 30% — every word should justify its place on one page.")
    if m["avg_bullet_len"] >= 24:
        out.append("Tighten bullets to 12-20 words — split compound achievements into two cleaner lines.")
    if not m["has_linkedin"]:
        out.append("Add a LinkedIn URL to the contact line — non-negotiable for technical recruiting.")
    if not m["has_github"] and (profile.get("projects") or []):
        out.append("Link a GitHub or portfolio so reviewers can verify project claims in 30 seconds.")
    if (profile.get("resume_gaps") or []):
        out.append(
            "Phase-1 audit flagged: " + " · ".join(profile["resume_gaps"][:3])
        )
    if m["section_count"] < 4:
        out.append(
            "Add the missing baseline section(s); even a 3-line Skills block helps ATS keyword matching."
        )
    return out[:7]


def _overall_score(m: dict) -> int:
    """Weighted 0-100 score from the metric tiles."""
    parts: list[float] = []
    parts.append(min(25.0, m["quantified_pct"] * 25 / 60))    # 25 pts at 60%+
    parts.append(min(20.0, m["action_verb_pct"] * 20 / 70))   # 20 pts at 70%+
    parts.append(max(0.0, 15.0 - m["weak_phrase_count"] * 5)) # -5 each weak phrase
    wc = m["word_count"]
    if 350 <= wc <= 650:
        parts.append(15.0)
    elif 250 <= wc < 350 or 650 < wc <= 800:
        parts.append(10.0)
    elif wc < 250:
        parts.append(max(0.0, 15 * wc / 250))
    else:  # wc > 800
        parts.append(max(0.0, 15 - (wc - 800) * 0.02))
    parts.append(min(10.0, m["section_count"] * 2.0))
    parts.append(min(10.0, m["skill_density"] * 10 / 6))      # 10 pts at 6.0+
    contact = sum([m["has_email"], m["has_linkedin"], m["has_github"], m["has_phone"]])
    parts.append(contact * 1.25)                               # max 5 pts
    return max(0, min(100, round(sum(parts))))


def _baseline_narrative(m: dict, strengths: list[str], red_flags: list[str],
                        suggestions: list[str], profile: dict) -> str:
    titles = profile.get("target_titles") or []
    target = titles[0] if titles else "the target role"
    name = (profile.get("name") or "").split()[0] if profile.get("name") else "this candidate"

    p1 = (
        f"Scanned {m['word_count']} words across {m['bullet_count']} bullets in "
        f"{m['section_count']} sections. The resume reads as a "
        f"~{m['reading_seconds']}-second skim — recruiters typically allocate 6–15 seconds, "
        f"so the top third has to carry the strongest signals on its own."
    )

    competitive = m["quantified_pct"] >= 50 and m["action_verb_pct"] >= 60 and m["weak_phrase_count"] <= 1
    p2 = (
        f"On the rubric that matters most, quantification sits at "
        f"{m['quantified_pct']}% (industry benchmark is 60%+) and action-verb usage at "
        f"{m['action_verb_pct']}% (target 70%+). Weak phrasings detected: {m['weak_phrase_count']}, "
        f"buzzwords: {m['buzzword_count']}. "
        + ("This is competitive territory — focus refinement on the specifics, not the basics."
           if competitive else
           "Closing these gaps is the highest-leverage edit on this resume; "
           "they directly translate into interview conversion.")
    )

    p3 = (
        f"For {target} alignment: {m['skill_count']} hard skills extracted with density "
        f"{m['skill_density']} per 100 words. "
        + ("First red flag worth attention: " + red_flags[0].lower()
           if red_flags else
           "No structural red flags surfaced — strengths are durable and section coverage is solid.")
    )

    p4 = (
        "Highest-leverage edits next: " + " · ".join(suggestions[:3])
        if suggestions else
        "The resume hits all of the major rubric points — focus on per-role bullet quality next."
    )

    return "\n\n".join([p1, p2, p3, p4])


# ── Provider-agnostic verification ────────────────────────────────────────────

def _coerce_str_list(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        s = str(item).strip()
        if s:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _extract_json_object(raw: str) -> dict | None:
    """Find the largest balanced `{...}` block in *raw* and parse it."""
    if not raw:
        return None
    candidates = [
        raw,
        re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.M),
    ]
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except (ValueError, json.JSONDecodeError):
            pass
    # brace-balanced scan
    starts = [i for i, ch in enumerate(raw) if ch == "{"]
    for start in starts:
        depth = 0
        for end in range(start, len(raw)):
            if raw[end] == "{":
                depth += 1
            elif raw[end] == "}":
                depth -= 1
                if depth == 0:
                    blob = raw[start:end + 1]
                    blob = re.sub(r",\s*([}\]])", r"\1", blob)
                    try:
                        obj = json.loads(blob)
                        if isinstance(obj, dict):
                            return obj
                    except (ValueError, json.JSONDecodeError):
                        break
                    break
    return None


def _verifier_prompt(resume_text: str, insights: dict) -> str:
    """The verification prompt — provider-agnostic, asked of any LLM."""
    metrics_for_prompt = {k: v for k, v in (insights.get("metrics") or {}).items()
                         if k != "sections"}
    return (
        "You are a senior technical resume reviewer. A heuristic scanner "
        "produced the analysis below from a candidate's resume. Your job:\n"
        "  1. VERIFY each item — silently DROP any inaccurate/misleading entries.\n"
        "  2. ADD 1-3 sharp insights only an experienced human reviewer would catch "
        "(specific evidence from the resume, not generic advice).\n"
        "  3. REWRITE the narrative as 3-4 specific paragraphs, "
        "referencing actual phrases from the resume.\n\n"
        "Return ONLY a JSON object with these keys:\n"
        "  strengths        : array of strings\n"
        "  red_flags        : array of strings\n"
        "  suggestions      : array of strings\n"
        "  narrative        : string (3-4 paragraphs, blank line between)\n"
        "  verification_notes : one short sentence describing what you "
        "changed vs. the heuristic.\n\n"
        f"Heuristic metrics:\n{json.dumps(metrics_for_prompt, indent=2)}\n\n"
        f"Heuristic strengths: {json.dumps(insights.get('strengths') or [])}\n"
        f"Heuristic red_flags: {json.dumps(insights.get('red_flags') or [])}\n"
        f"Heuristic suggestions: {json.dumps(insights.get('suggestions') or [])}\n"
        f"Heuristic narrative:\n{insights.get('narrative', '')}\n\n"
        f"Resume excerpt (first 3000 chars):\n{(resume_text or '')[:3000]}"
    )


def verify_with_provider(resume_text: str, insights: dict, provider) -> dict:
    """Ask the active LLM provider to verify + enrich the heuristic insights.

    Returns either:
      - a dict with `strengths`, `red_flags`, `suggestions`, `narrative`,
        `verification_notes` populated from the LLM response (success), OR
      - a dict with `_error` (short code) and `_message` (neutral, non-
        provider-branded explanation) describing why verification was skipped.

    The provider name is intentionally not surfaced in the return value or
    log output — callers should report a uniform "AI verification" status to
    the user regardless of which backend produced (or failed to produce) it.
    """
    if provider is None:
        return {"_error": "no_provider", "_message": "AI verification unavailable."}

    chat_fn = getattr(provider, "chat", None)
    if not callable(chat_fn):
        return {"_error": "no_provider", "_message": "AI verification unavailable."}

    prompt = _verifier_prompt(resume_text or "", insights)

    try:
        # Try with strict JSON mode first; fall back to plain text on TypeError
        # (older provider implementations didn't accept the kwarg).
        try:
            raw = chat_fn(
                "You are a strict, careful resume reviewer. Reply with JSON only.",
                [{"role": "user", "content": prompt}],
                max_tokens=1500,
                json_mode=True,
            )
        except TypeError:
            raw = chat_fn(
                "You are a strict, careful resume reviewer. Reply with JSON only.",
                [{"role": "user", "content": prompt}],
                max_tokens=1500,
            )
    except (TimeoutError,) as exc:
        return {"_error": "timeout",
                "_message": "AI verification timed out — showing heuristic findings."}
    except (ConnectionError, OSError) as exc:
        return {"_error": "offline",
                "_message": "AI verification unavailable — showing heuristic findings."}
    except NotImplementedError:
        return {"_error": "no_provider",
                "_message": "AI verification not supported by the current provider."}
    except Exception as exc:                                              # noqa: BLE001
        # Print to the server console only; don't leak provider/error
        # specifics into the user-facing notes.
        console.print(f"  [dim]AI verification skipped: {exc.__class__.__name__}[/dim]")
        return {"_error": "request_failed",
                "_message": "AI verification unavailable — showing heuristic findings."}

    if not isinstance(raw, str) or not raw.strip():
        return {"_error": "empty_response",
                "_message": "AI verification returned an empty response — showing heuristic findings."}

    parsed = _extract_json_object(raw)
    if not parsed:
        return {"_error": "parse_content",
                "_message": "AI verification returned unreadable output — showing heuristic findings."}

    out: dict[str, Any] = {}
    strengths = _coerce_str_list(parsed.get("strengths"))
    red_flags = _coerce_str_list(parsed.get("red_flags"))
    suggestions = _coerce_str_list(parsed.get("suggestions"))
    if strengths:
        out["strengths"] = strengths
    if red_flags:
        out["red_flags"] = red_flags
    if suggestions:
        out["suggestions"] = suggestions
    narrative = parsed.get("narrative")
    if isinstance(narrative, str) and narrative.strip():
        out["narrative"] = narrative.strip()
    notes = parsed.get("verification_notes")
    if isinstance(notes, str) and notes.strip():
        out["verification_notes"] = notes.strip()

    if not out:
        return {"_error": "empty_response",
                "_message": "AI verification returned no usable fields — showing heuristic findings."}
    return out


# Back-compat shim. Older callers passed `ollama_model: str`. We no longer
# care which provider runs the verification, so return a neutral error.
def verify_with_ollama(*_args, **_kwargs) -> dict:                        # noqa: ARG001
    return {"_error": "deprecated",
            "_message": "AI verification unavailable — showing heuristic findings."}


# ── Public entry point ────────────────────────────────────────────────────────

def analyze_resume(resume_text: str, profile: dict,
                   provider=None,
                   ollama_model: str | None = None) -> dict:                  # noqa: ARG001
    """Compute deterministic insights, then optionally verify with the
    active LLM provider.

    Always returns a dict shaped:
      {
        version, overall_score, metrics, strengths, red_flags, suggestions,
        narrative, verified_by, verification_notes,
      }

    `verified_by` is one of:
      - ``"heuristic"`` — no provider attempted, or verification failed.
      - ``"ai"``        — an LLM provider successfully verified the findings.

    The `ollama_model` kwarg is accepted for back-compat with older callers
    but ignored; verification now uses whatever provider the caller passed in.
    """
    profile = profile or {}
    metrics = _scan_metrics(resume_text or "", profile)
    strengths = _derive_strengths(metrics, profile)
    red_flags = _derive_red_flags(metrics, profile)
    suggestions = _derive_suggestions(metrics, profile)
    overall = _overall_score(metrics)
    narrative = _baseline_narrative(metrics, strengths, red_flags, suggestions, profile)

    insights: dict[str, Any] = {
        "version": INSIGHTS_VERSION,
        "overall_score": overall,
        "metrics": metrics,
        "strengths": strengths,
        "red_flags": red_flags,
        "suggestions": suggestions,
        "narrative": narrative,
        "verified_by": "heuristic",
        "verification_notes": "",
    }

    if provider is not None and hasattr(provider, "chat"):
        verified = verify_with_provider(resume_text or "", insights, provider)
        if verified and "_error" not in verified:
            for key in ("strengths", "red_flags", "suggestions",
                        "narrative", "verification_notes"):
                if key in verified:
                    insights[key] = verified[key]
            insights["verified_by"] = "ai"
        else:
            # Verification failed. The heuristic output is already a complete,
            # valid analysis — don't surface an apology in the user-facing
            # verification_notes. Keep the error code only for diagnostics
            # (the UI ignores it; dev-mode can still inspect it).
            insights["verification_notes"] = ""
            insights["verification_error"] = (verified or {}).get("_error", "unknown")

    return insights
