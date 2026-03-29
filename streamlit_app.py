#!/usr/bin/env python3
"""
Streamlit Web UI for the Job Application Agent.

Run:
    streamlit run streamlit_app.py

Prerequisites:
    pip install streamlit pandas
    pip install -r requirements.txt
"""

import streamlit as st
import sys
import os
import traceback
import tempfile
from pathlib import Path
from datetime import datetime

import pandas as pd

# ── Page config (must be first Streamlit call) ─────────────────────────────────
st.set_page_config(
    page_title="Job Application Agent",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Root path + agent import ───────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:
    import agent as _ag
    _ag_err = None
except Exception as _e:
    _ag = None
    _ag_err = f"{type(_e).__name__}: {_e}"

# ── Session state defaults ─────────────────────────────────────────────────────
_DEFAULTS: dict = {
    # Config
    "mode":         "demo",
    "api_key":      "",
    "ollama_model": "llama3.2",
    "resume_text":  "",
    "job_titles":   "IC Design Intern, Photonics Engineer Intern, FPGA/Hardware Intern",
    "location":     "Remote",
    "threshold":    75,
    "max_apps":     10,
    "cover_letter": False,
    "blacklist":    "",
    "whitelist":    "NVIDIA, Apple, Microsoft, Intel, IBM, Micron, Samsung, TSMC",
    # Phase results
    "profile":      None,
    "jobs":         None,
    "scored_jobs":  None,
    "tailored_map": {},
    "applications": [],
    "report":       None,
    "tracker_path": None,
    # Pipeline state
    "phase_done":   set(),
    "phase_error":  {},
    "run_all":      False,
}

for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ── Provider factory ───────────────────────────────────────────────────────────
def _make_provider():
    if _ag is None:
        raise RuntimeError(f"agent.py not loaded: {_ag_err}")
    mode = st.session_state.mode
    if mode == "demo":
        return _ag.DemoProvider()
    if mode == "anthropic":
        key = st.session_state.api_key.strip()
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
        return _ag.AnthropicProvider()
    if mode == "ollama":
        model = st.session_state.ollama_model.strip()
        if not model:
            raise ValueError(
                "Ollama model name is empty. "
                "Enter a model name in the sidebar (e.g. llama3.2)."
            )
        return _ag.OllamaProvider(model=model)
    raise ValueError(f"Unknown mode: {mode}")


# ── Phase helpers ──────────────────────────────────────────────────────────────
PHASE_LABELS = {
    1: "Resume Ingestion & Profile Extraction",
    2: "Job Discovery",
    3: "Relevance Scoring & Shortlisting",
    4: "Resume Tailoring",
    5: "Application Submission",
    6: "Excel Tracker Update",
    7: "Run Report",
}


def _done(n: int) -> bool:
    return n in st.session_state.phase_done


def _errored(n: int) -> bool:
    return n in st.session_state.phase_error


def _icon(n: int) -> str:
    if _done(n):    return "✅"
    if _errored(n): return "❌"
    return "⬜"


def _run_phase(n: int, fn, *args):
    """Call fn(*args), update phase_done/phase_error, return (result, error_str)."""
    try:
        result = fn(*args)
        st.session_state.phase_error.pop(n, None)
        st.session_state.phase_done.add(n)
        return result, None
    except (Exception, SystemExit) as exc:
        st.session_state.phase_error[n] = traceback.format_exc()
        return None, str(exc)


def _try_provider(phase_n: int):
    """Create the provider and return (provider, ok).
    On any error (including Ollama not running / sys.exit) stores the traceback
    in phase_error and returns (None, False) so the phase block can st.rerun().
    """
    try:
        return _make_provider(), True
    except (Exception, SystemExit) as exc:
        st.session_state.phase_error[phase_n] = traceback.format_exc()
        return None, False


# ── Visual helpers ─────────────────────────────────────────────────────────────
def _score_dot(score: int) -> str:
    thr = st.session_state.threshold
    if score >= thr: return "🟢"
    if score >= 60:  return "🟡"
    return "🔴"


def _status_dot(status: str) -> str:
    return {
        "Applied":         "🟢",
        "Manual Required": "🟡",
        "Approved":        "🔵",
        "Skipped":         "🔴",
    }.get(status, "⬜")


def _reset_pipeline():
    for k in ["profile", "jobs", "scored_jobs", "report", "tracker_path"]:
        st.session_state[k] = None
    st.session_state.tailored_map = {}
    st.session_state.applications = []
    st.session_state.phase_done   = set()
    st.session_state.phase_error  = {}
    st.session_state.run_all      = False


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## ⚙️ Configuration")

    # ── LLM backend ───────────────────────────────────────────────────────────
    st.markdown("### LLM Backend")
    st.selectbox(
        "Mode",
        options=["demo", "anthropic", "ollama"],
        format_func=lambda x: {
            "demo":      "Demo (no API key)",
            "anthropic": "Anthropic Claude",
            "ollama":    "Ollama (local LLM)",
        }[x],
        key="mode",
    )
    if st.session_state.mode == "anthropic":
        st.text_input("ANTHROPIC_API_KEY", type="password", key="api_key",
                      placeholder="sk-ant-...")
    elif st.session_state.mode == "ollama":
        st.text_input("Ollama model", key="ollama_model", placeholder="llama3.2")
        if not st.session_state.ollama_model.strip():
            st.warning("Enter a model name above, e.g. `llama3.2`")
        # Live Ollama connectivity check
        try:
            import urllib.request as _ur
            _ur.urlopen("http://localhost:11434/api/tags", timeout=2)
            st.success("Ollama is running ✓", icon="🟢")
        except Exception:
            st.error(
                "Ollama not reachable at localhost:11434\n\n"
                "Run these commands first:\n"
                f"```\nollama pull {st.session_state.ollama_model}\n"
                "ollama serve\n```",
                icon="🔴",
            )

    # ── Resume ────────────────────────────────────────────────────────────────
    st.divider()
    st.markdown("### Resume")
    resume_src = st.radio(
        "Resume source",
        ["Demo profile", "Upload file", "Paste text"],
        label_visibility="collapsed",
    )

    if resume_src == "Upload file":
        uploaded = st.file_uploader("Upload PDF / DOCX / TXT",
                                    type=["pdf", "docx", "txt"])
        if uploaded and _ag:
            try:
                suffix = Path(uploaded.name).suffix
                # Write to a temp file so _read_resume (and pdfplumber) can open it by path
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(uploaded.read())
                    tmp_path = Path(tmp.name)
                text = _ag._read_resume(tmp_path)
                # Detect silent fallback to demo resume (means extraction failed)
                demo = _ag._build_demo_resume()
                if text.strip() and text.strip() != demo.strip():
                    st.session_state.resume_text = text
                    st.success(f"Loaded: {uploaded.name}  ({len(text):,} chars)")
                else:
                    st.error(
                        f"Could not extract text from **{uploaded.name}**. "
                        "Possible causes: scanned/image-only PDF, corrupted file, "
                        "or pdfplumber not installed (`pip install pdfplumber`). "
                        "Try a text-based PDF, DOCX, or TXT file instead."
                    )
            except Exception as exc:
                st.error(f"Failed to read **{uploaded.name}**: {exc}")

    elif resume_src == "Paste text":
        pasted = st.text_area("Resume text", height=150,
                              placeholder="Paste your resume here…")
        if pasted:
            st.session_state.resume_text = pasted

    else:  # Demo profile
        if _ag and not st.session_state.resume_text:
            st.session_state.resume_text = _ag._build_demo_resume()
        if st.button("Reload demo profile", use_container_width=True):
            if _ag:
                st.session_state.resume_text = _ag._build_demo_resume()

    if st.session_state.resume_text:
        with st.expander("Preview resume"):
            preview = st.session_state.resume_text
            st.code(
                preview[:800] + ("…" if len(preview) > 800 else ""),
                language=None,
            )

    # ── Search settings ───────────────────────────────────────────────────────
    st.divider()
    st.markdown("### Search Settings")
    st.text_input("Job titles (comma-separated)", key="job_titles")
    st.text_input("Location / Region", key="location")
    st.slider("Auto-apply threshold (score)", 50, 100, key="threshold")
    st.number_input("Max applications per run", min_value=1, max_value=50,
                    key="max_apps")
    st.checkbox("Generate cover letters", key="cover_letter")

    with st.expander("Advanced filters"):
        st.text_input("Blacklist companies (comma-sep)", key="blacklist",
                      placeholder="e.g. Acme, Globex")
        st.text_input("Priority companies (comma-sep)", key="whitelist")

    # ── Reset ─────────────────────────────────────────────────────────────────
    st.divider()
    if st.button("🔄 Reset Pipeline", use_container_width=True, type="secondary"):
        _reset_pipeline()
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AREA
# ══════════════════════════════════════════════════════════════════════════════
st.title("💼 Job Application Agent")
st.caption("Autonomous 7-Phase Job Search & Application Pipeline")

if _ag_err:
    st.error(f"Failed to import agent.py — {_ag_err}")
    st.info("Make sure all dependencies are installed: `pip install -r requirements.txt`")
    st.stop()

tab_pipeline, tab_tracker, tab_files = st.tabs(["🚀 Pipeline", "📊 Tracker", "📁 Output Files"])


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE TAB
# ══════════════════════════════════════════════════════════════════════════════
with tab_pipeline:
    # ── Stop run_all BEFORE evaluating any should_runX condition ──────────────
    # st.rerun() restarts the script from the top, so a bottom-of-page check
    # is never reached after an error.  Moving the check here breaks the loop.
    if st.session_state.run_all and st.session_state.phase_error:
        st.session_state.run_all = False

    # Progress bar
    n_done = len(st.session_state.phase_done)
    st.progress(n_done / 7, text=f"Pipeline: {n_done} / 7 phases complete")

    col_btn, col_hint = st.columns([1, 5])
    if col_btn.button("▶ Run Full Pipeline", type="primary", use_container_width=True):
        _reset_pipeline()
        st.session_state.run_all = True
        st.rerun()
    col_hint.caption(
        "Runs all 7 phases automatically, or step through them individually below."
    )
    st.divider()

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 1 — Resume Ingestion
    # ─────────────────────────────────────────────────────────────────────────
    with st.expander(f"{_icon(1)} Phase 1 — {PHASE_LABELS[1]}",
                     expanded=not _done(1)):

        if not st.session_state.resume_text:
            st.warning("No resume loaded — select a source in the sidebar.")
        else:
            btn_p1 = None
            if not _done(1):
                btn_p1 = st.button("▶ Extract Profile", key="btn_p1")

            should_run1 = (
                (btn_p1 or (st.session_state.run_all and not _errored(1))) and
                not _done(1) and
                bool(st.session_state.resume_text)
            )

            if should_run1:
                _model_hint = (f" via Ollama ({st.session_state.ollama_model})"
                               if st.session_state.mode == "ollama" else "")
                with st.spinner(f"Extracting profile{_model_hint} — this may take up to 2 min…"):
                    provider, ok = _try_provider(1)
                    if ok:
                        profile, err = _run_phase(
                            1, _ag.phase1_ingest_resume,
                            st.session_state.resume_text, provider,
                        )
                        if profile is not None:
                            st.session_state.profile = profile
                st.rerun()

            if st.session_state.profile:
                p = st.session_state.profile
                c1, c2, c3 = st.columns(3)
                c1.metric("Name",     p.get("name", "—"))
                c2.metric("Email",    p.get("email", "—"))
                c3.metric("Location", p.get("location", "—"))

                left, right = st.columns(2)
                with left:
                    st.markdown("**Target titles**")
                    for t in p.get("target_titles", []):
                        st.write(f"• {t}")
                with right:
                    st.markdown("**Hard skills**")
                    st.write(", ".join(p.get("top_hard_skills", [])))
                    st.markdown("**Soft skills**")
                    st.write(", ".join(p.get("top_soft_skills", [])))

                if p.get("resume_gaps"):
                    st.warning("Resume gaps: " + " | ".join(p["resume_gaps"]))

        if _errored(1):
            with st.expander("Error details"):
                st.code(st.session_state.phase_error[1])

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 2 — Job Discovery
    # ─────────────────────────────────────────────────────────────────────────
    with st.expander(f"{_icon(2)} Phase 2 — {PHASE_LABELS[2]}",
                     expanded=_done(1) and not _done(2)):

        if not st.session_state.profile:
            st.info("Complete Phase 1 first.")
        else:
            # ── Cache status ───────────────────────────────────────────────────
            _cache_file = _ag.RESOURCES_DIR / "sample_jobs.json"
            if _cache_file.exists():
                import json as _json
                try:
                    _cached = _json.loads(_cache_file.read_text(encoding="utf-8"))
                    _first_url = (_cached[0].get("application_url", "") if _cached else "")
                    _is_demo   = any(
                        demo in _first_url
                        for demo in ["nvidia.com/careers", "intel.com/jobs",
                                     "microsoft.com/careers", "apple.com/jobs",
                                     "lumentum.com/careers", "micron.com/careers",
                                     "research.ibm.com", "samsung.com/us/careers"]
                    )
                except Exception:
                    _is_demo = False

                if _is_demo:
                    st.warning(
                        "**Cached jobs are demo/fake URLs** from a previous run.  "
                        "Click **Clear cache** to scrape real postings.",
                        icon="⚠️",
                    )
                else:
                    from datetime import datetime as _dt
                    _age = _dt.now().timestamp() - _cache_file.stat().st_mtime
                    _age_h = int(_age / 3600)
                    st.info(
                        f"Using cached jobs ({len(_cached)} postings, "
                        f"{_age_h}h old).  Click **Clear cache** to re-scrape.",
                        icon="📂",
                    )

                if st.button("🗑 Clear cache & re-scrape", key="btn_clear_cache"):
                    _cache_file.unlink(missing_ok=True)
                    # Also reset phase 2 so it re-runs
                    st.session_state.phase_done.discard(2)
                    st.session_state.phase_done.discard(3)
                    st.session_state.phase_done.discard(4)
                    st.session_state.phase_done.discard(5)
                    st.session_state.phase_done.discard(6)
                    st.session_state.phase_done.discard(7)
                    st.session_state.jobs        = None
                    st.session_state.scored_jobs = None
                    st.session_state.tailored_map = {}
                    st.session_state.applications = []
                    st.session_state.report       = None
                    st.rerun()

            btn_p2 = None
            if not _done(2):
                btn_p2 = st.button("▶ Discover Jobs", key="btn_p2")
                if not _cache_file.exists():
                    st.caption("Will scrape live job boards via JobSpy (LinkedIn, Indeed, Glassdoor, ZipRecruiter).")

            should_run2 = (
                (btn_p2 or (st.session_state.run_all and _done(1) and not _errored(2))) and
                not _done(2)
            )

            if should_run2:
                with st.spinner("Discovering jobs (scraping live boards — may take 30–60 s)…"):
                    provider, ok = _try_provider(2)
                    if ok:
                        titles = [t.strip() for t in
                                  st.session_state.job_titles.split(",") if t.strip()]
                        jobs, err = _run_phase(
                            2, _ag.phase2_discover_jobs,
                            st.session_state.profile, titles,
                            st.session_state.location, provider,
                        )
                        if jobs is not None:
                            st.session_state.jobs = jobs
                st.rerun()

            if st.session_state.jobs:
                jobs = st.session_state.jobs
                st.metric("Jobs found", len(jobs))
                df = pd.DataFrame([{
                    "Company":  j.get("company", ""),
                    "Title":    j.get("title", ""),
                    "Location": j.get("location", ""),
                    "Remote":   "Yes" if j.get("remote") else "No",
                    "Platform": j.get("platform", ""),
                    "Salary":   j.get("salary_range", ""),
                    "Link":     j.get("application_url", ""),
                } for j in jobs])
                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Link": st.column_config.LinkColumn("Link", display_text="Apply"),
                    },
                )

        if _errored(2):
            with st.expander("Error details"):
                st.code(st.session_state.phase_error[2])

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 3 — Relevance Scoring
    # ─────────────────────────────────────────────────────────────────────────
    with st.expander(f"{_icon(3)} Phase 3 — {PHASE_LABELS[3]}",
                     expanded=_done(2) and not _done(3)):

        if not st.session_state.jobs:
            st.info("Complete Phase 2 first.")
        else:
            thr = st.session_state.threshold
            btn_p3 = None
            if not _done(3):
                btn_p3 = st.button("▶ Score Jobs", key="btn_p3")
                st.caption(f"Min filter: 60  |  Auto-apply threshold: {thr}")

            should_run3 = (
                (btn_p3 or (st.session_state.run_all and _done(2) and not _errored(3))) and
                not _done(3)
            )

            if should_run3:
                _model_hint3 = (f" via Ollama ({st.session_state.ollama_model})"
                                if st.session_state.mode == "ollama" else "")
                with st.spinner(f"Scoring jobs{_model_hint3}…"):
                    provider, ok = _try_provider(3)
                    if ok:
                        scored, err = _run_phase(
                            3, _ag.phase3_score_jobs,
                            st.session_state.jobs, st.session_state.profile,
                            provider, 60,
                        )
                        if scored is not None:
                            st.session_state.scored_jobs = scored
                st.rerun()

            if st.session_state.scored_jobs:
                scored = st.session_state.scored_jobs
                auto   = [j for j in scored if j.get("score", 0) >= thr]
                review = [j for j in scored if 60 <= j.get("score", 0) < thr]

                m1, m2, m3 = st.columns(3)
                m1.metric("Auto-eligible",    len(auto),   help=f"Score ≥ {thr}")
                m2.metric("Review needed",    len(review), help="Score 60–74")
                m3.metric("Total shortlisted", len(scored))

                df = pd.DataFrame([{
                    "":         _score_dot(j.get("score", 0)),
                    "Company":  j.get("company", ""),
                    "Title":    j.get("title", ""),
                    "Score":    j.get("score", 0),
                    "Status":   ("Auto-eligible" if j.get("score", 0) >= thr
                                 else "Review needed"),
                    "Matching": ", ".join(j.get("matching_skills", [])),
                    "Missing":  ", ".join(j.get("missing_skills", [])),
                    "Reason":   j.get("reason", ""),
                    "Link":     j.get("application_url", ""),
                } for j in scored])
                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Link": st.column_config.LinkColumn("Link", display_text="Apply"),
                    },
                )

        if _errored(3):
            with st.expander("Error details"):
                st.code(st.session_state.phase_error[3])

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 4 — Resume Tailoring
    # ─────────────────────────────────────────────────────────────────────────
    with st.expander(f"{_icon(4)} Phase 4 — {PHASE_LABELS[4]}",
                     expanded=_done(3) and not _done(4)):

        if not st.session_state.scored_jobs:
            st.info("Complete Phase 3 first.")
        else:
            thr      = st.session_state.threshold
            top_jobs = [j for j in st.session_state.scored_jobs
                        if j.get("score", 0) >= thr][:st.session_state.max_apps]

            btn_p4 = None
            if not _done(4):
                btn_p4 = st.button("▶ Tailor Resumes", key="btn_p4")
                st.caption(
                    f"Tailoring for {len(top_jobs)} auto-eligible job(s) "
                    f"(score ≥ {thr}, max {st.session_state.max_apps})."
                )

            should_run4 = (
                (btn_p4 or (st.session_state.run_all and _done(3) and not _errored(4))) and
                not _done(4) and
                bool(top_jobs)
            )

            if should_run4:
                prog = st.progress(0, text="Tailoring resumes…")
                try:
                    provider, ok = _try_provider(4)
                    if not ok:
                        raise RuntimeError(st.session_state.phase_error.get(4, "Provider error"))
                    tailored_map = {}
                    total = len(top_jobs)
                    for i, job in enumerate(top_jobs, 1):
                        prog.progress(
                            i / total,
                            text=f"Tailoring ({i}/{total}): "
                                 f"{job.get('company')} — {job.get('title')}",
                        )
                        result = _ag.phase4_tailor_resume(
                            job, st.session_state.profile,
                            st.session_state.resume_text, provider,
                            include_cover_letter=st.session_state.cover_letter,
                        )
                        tailored_map[job.get("id", job.get("title", ""))] = {
                            "job": job, "tailored": result,
                        }
                    st.session_state.tailored_map = tailored_map
                    st.session_state.phase_done.add(4)
                    st.session_state.phase_error.pop(4, None)
                except Exception as exc:
                    st.session_state.phase_error[4] = traceback.format_exc()
                finally:
                    prog.empty()
                st.rerun()

            if st.session_state.tailored_map:
                for jk, data in st.session_state.tailored_map.items():
                    job = data["job"]
                    t   = data["tailored"]
                    with st.expander(
                        f"**{job.get('company')}** — {job.get('title')}  "
                        f"(score: {job.get('score')})"
                    ):
                        st.markdown(f"**Summary:** {t.get('summary', '—')}")
                        st.markdown(
                            "**Skills (ATS-ordered):** " +
                            " | ".join(t.get("skills_reordered", []))
                        )
                        st.markdown(
                            "**Section order:** " +
                            " → ".join(t.get("section_order", []))
                        )
                        if t.get("ats_keywords_missing"):
                            st.warning(
                                "ATS gaps — consider adding: " +
                                ", ".join(t["ats_keywords_missing"])
                            )
                        if t.get("cover_letter"):
                            st.markdown("---")
                            st.markdown("**Cover Letter**")
                            st.text(t["cover_letter"])

        if _errored(4):
            with st.expander("Error details"):
                st.code(st.session_state.phase_error[4])

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 5 & 6 — Submit + Track
    # ─────────────────────────────────────────────────────────────────────────
    with st.expander(
        f"{_icon(5)} Phase 5 & 6 — {PHASE_LABELS[5]} & {PHASE_LABELS[6]}",
        expanded=_done(4) and not _done(5),
    ):
        if not st.session_state.tailored_map:
            st.info("Complete Phase 4 first.")
        else:
            btn_p56 = None
            if not _done(5):
                btn_p56 = st.button("▶ Submit & Track", key="btn_p56", type="primary")
                st.caption(
                    "Simulates submissions (demo) and writes the Excel tracker.  "
                    "For real Playwright submission run `python agent.py --real-apply`."
                )

            should_run56 = (
                (btn_p56 or (st.session_state.run_all and _done(4) and not _errored(5))) and
                not _done(5)
            )

            if should_run56:
                with st.spinner("Submitting applications and building tracker…"):
                    try:
                        already_applied = _ag._load_existing_applications()
                        applications    = []
                        processed_ids   = set()

                        for jk, data in st.session_state.tailored_map.items():
                            job     = data["job"]
                            tailored = data["tailored"]
                            result  = _ag.phase5_simulate_submission(job, already_applied)

                            _ag.OUTPUT_DIR.mkdir(exist_ok=True)
                            resume_file = _ag._save_tailored_resume(job, tailored)
                            processed_ids.add(job.get("id"))

                            applications.append({
                                **job,
                                "date_applied":      datetime.now().strftime("%m/%d/%Y"),
                                "resume_version":    resume_file,
                                "cover_letter_sent": bool(tailored.get("cover_letter")),
                                "status":            result["status"],
                                "confirmation":      result["confirmation"],
                                "notes": (
                                    "ATS gaps: " +
                                    ", ".join(tailored.get("ats_keywords_missing", [])[:2])
                                    if tailored.get("ats_keywords_missing") else ""
                                ),
                            })

                        # Add skipped jobs (scored but below threshold or max reached)
                        for job in st.session_state.scored_jobs:
                            if job.get("id") not in processed_ids:
                                applications.append({
                                    **job,
                                    "date_applied":      datetime.now().strftime("%m/%d/%Y"),
                                    "resume_version":    "",
                                    "cover_letter_sent": False,
                                    "status":            "Skipped",
                                    "confirmation":      "N/A",
                                    "notes": (
                                        f"Score {job.get('score', 0)} below threshold "
                                        f"or max applications reached"
                                    ),
                                })

                        tracker_path = _ag.phase6_update_tracker(applications)
                        st.session_state.applications = applications
                        st.session_state.tracker_path = tracker_path
                        st.session_state.phase_done.add(5)
                        st.session_state.phase_done.add(6)
                        st.session_state.phase_error.pop(5, None)
                        st.session_state.phase_error.pop(6, None)

                    except Exception as exc:
                        st.session_state.phase_error[5] = traceback.format_exc()
                st.rerun()

            if st.session_state.applications:
                apps = st.session_state.applications
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Applied",         sum(1 for a in apps if a.get("status") == "Applied"))
                m2.metric("Manual Required", sum(1 for a in apps if a.get("status") == "Manual Required"))
                m3.metric("Skipped",         sum(1 for a in apps if a.get("status") == "Skipped"))
                m4.metric("Total processed", len(apps))

                df = pd.DataFrame([{
                    "":             _status_dot(a.get("status", "")),
                    "Company":      a.get("company", ""),
                    "Title":        a.get("title", ""),
                    "Score":        a.get("score", 0),
                    "Status":       a.get("status", ""),
                    "Confirmation": a.get("confirmation", ""),
                    "Resume File":  a.get("resume_version", ""),
                    "Link":         a.get("application_url", ""),
                } for a in apps])
                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Link": st.column_config.LinkColumn("Link", display_text="Apply"),
                    },
                )

                if st.session_state.tracker_path:
                    st.success(f"Tracker saved → `{st.session_state.tracker_path}`")

        if _errored(5):
            with st.expander("Error details"):
                st.code(st.session_state.phase_error[5])

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 7 — Run Report
    # ─────────────────────────────────────────────────────────────────────────
    with st.expander(f"{_icon(7)} Phase 7 — {PHASE_LABELS[7]}",
                     expanded=_done(5) and not _done(7)):

        if not st.session_state.applications:
            st.info("Complete Phases 5 & 6 first.")
        else:
            btn_p7 = None
            if not _done(7):
                btn_p7 = st.button("▶ Generate Report", key="btn_p7")

            should_run7 = (
                (btn_p7 or (st.session_state.run_all and _done(5) and not _errored(7))) and
                not _done(7)
            )

            if should_run7:
                with st.spinner("Generating end-of-run report…"):
                    provider, ok = _try_provider(7)
                    if ok:
                        report, err = _run_phase(
                            7, _ag.phase7_run_report,
                            st.session_state.applications,
                            st.session_state.tracker_path,
                            provider,
                        )
                        if report is not None:
                            st.session_state.report = report
                    st.session_state.run_all = False  # pipeline complete
                st.rerun()

            if st.session_state.report:
                st.markdown("### Run Summary")
                st.text(st.session_state.report)

                report_md = (
                    f"# Job Application Run Report\n"
                    f"**Date:** {datetime.now().date().isoformat()}\n\n"
                    f"{st.session_state.report}"
                )
                col_d1, col_d2 = st.columns(2)
                col_d1.download_button(
                    "⬇ Download Report (.md)",
                    data=report_md,
                    file_name=(
                        f"{datetime.now().strftime('%Y%m%d')}"
                        "_job-application-run-report.md"
                    ),
                    mime="text/markdown",
                )
                if st.session_state.tracker_path:
                    with open(st.session_state.tracker_path, "rb") as fh:
                        col_d2.download_button(
                            "⬇ Download Tracker (.xlsx)",
                            data=fh.read(),
                            file_name=Path(st.session_state.tracker_path).name,
                            mime=(
                                "application/vnd.openxmlformats-officedocument"
                                ".spreadsheetml.sheet"
                            ),
                        )

        if _errored(7):
            with st.expander("Error details"):
                st.code(st.session_state.phase_error[7])

    if st.session_state.phase_error and not st.session_state.run_all:
        st.warning("One or more phases have errors. Fix the issue and click the phase button to retry.")


# ══════════════════════════════════════════════════════════════════════════════
# TRACKER TAB
# ══════════════════════════════════════════════════════════════════════════════
with tab_tracker:
    st.header("Application Tracker")

    output_dir   = _ag.OUTPUT_DIR if _ag else ROOT / "output"
    month        = datetime.now().strftime("%Y-%m")
    tracker_path = output_dir / f"Job_Applications_Tracker_{month}.xlsx"

    col_r, col_p = st.columns([1, 5])
    if col_r.button("🔄 Refresh", use_container_width=True, key="tracker_refresh"):
        st.rerun()
    col_p.caption(f"Reading: `{tracker_path}`")

    if not tracker_path.exists():
        st.info("No tracker found for this month. Run the pipeline to create one.")
    else:
        try:
            import openpyxl

            wb = openpyxl.load_workbook(tracker_path, read_only=True)
            ws = wb.active
            headers = [cell.value for cell in next(ws.iter_rows(max_row=1))]
            rows = [
                dict(zip(headers, row))
                for row in ws.iter_rows(min_row=2, values_only=True)
                if any(row)
            ]
            wb.close()

            if not rows:
                st.info("Tracker is empty.")
            else:
                # Summary metrics
                statuses = [r.get("Status", "") for r in rows]
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Applied",         statuses.count("Applied"))
                m2.metric("Manual Required", statuses.count("Manual Required"))
                m3.metric("Approved",        statuses.count("Approved"))
                m4.metric("Skipped",         statuses.count("Skipped"))

                # Full table with status dot
                df = pd.DataFrame(rows)
                if "Status" in df.columns:
                    df.insert(0, "", df["Status"].map(_status_dot).fillna("⬜"))
                st.dataframe(df, use_container_width=True, hide_index=True)

                # Approve manual-review rows
                manual = [r for r in rows if r.get("Status") == "Manual Required"]
                if manual:
                    st.subheader("Pending Manual Review")
                    for r in manual:
                        c1, c2, c3, c4 = st.columns([2, 3, 1, 1])
                        c1.write(f"**{r.get('Company', '')}**")
                        c2.write(
                            f"{r.get('Job Title', '')}  |  "
                            f"Score: {r.get('Match Score', '')}"
                        )
                        url = r.get("Job Posting URL", "")
                        if url:
                            c3.link_button("View", url)
                        row_num = r.get("#")
                        if row_num and c4.button(
                            "Approve", key=f"approve_{row_num}", type="primary"
                        ):
                            wb2 = openpyxl.load_workbook(tracker_path)
                            ws2 = wb2.active
                            hdrs = [cell.value for cell in ws2[1]]
                            sc = (hdrs.index("Status") + 1
                                  if "Status" in hdrs else None)
                            if sc:
                                target = int(row_num) + 1
                                ws2.cell(row=target, column=sc).value = "Approved"
                                from openpyxl.styles import PatternFill
                                blue = PatternFill("solid", fgColor="BDD7EE")
                                for ci in range(1, len(hdrs) + 1):
                                    ws2.cell(row=target, column=ci).fill = blue
                                wb2.save(tracker_path)
                            st.success(
                                f"Approved: {r.get('Company')} — {r.get('Job Title')}"
                            )
                            st.rerun()

                # Download button
                with open(tracker_path, "rb") as fh:
                    st.download_button(
                        "⬇ Download Tracker (.xlsx)",
                        data=fh.read(),
                        file_name=tracker_path.name,
                        mime=(
                            "application/vnd.openxmlformats-officedocument"
                            ".spreadsheetml.sheet"
                        ),
                        key="tracker_dl",
                    )

        except Exception as exc:
            st.error(f"Failed to load tracker: {exc}")
            with st.expander("Details"):
                st.code(traceback.format_exc())


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT FILES TAB
# ══════════════════════════════════════════════════════════════════════════════
with tab_files:
    st.header("Output Files")

    output_dir = _ag.OUTPUT_DIR if _ag else ROOT / "output"

    if st.button("🔄 Refresh file list", key="files_refresh"):
        st.rerun()

    if not output_dir.exists() or not any(output_dir.iterdir()):
        st.info(
            "No output files yet. Run the pipeline to generate tailored resumes, "
            "the tracker, and the run report."
        )
    else:
        files    = sorted(output_dir.iterdir(),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        trackers = [f for f in files if "Tracker" in f.name]
        resumes  = [f for f in files if "Resume" in f.name]
        covers   = [f for f in files if "CoverLetter" in f.name]
        reports  = [f for f in files if "report" in f.name.lower()]
        others   = [f for f in files
                    if f not in trackers + resumes + covers + reports]

        def _file_section(title: str, file_list: list):
            if not file_list:
                return
            st.markdown(f"#### {title}")
            for fp in file_list:
                size_kb = max(fp.stat().st_size // 1024, 1)
                mtime   = datetime.fromtimestamp(
                    fp.stat().st_mtime
                ).strftime("%Y-%m-%d %H:%M")
                c1, c2 = st.columns([5, 1])
                c1.write(f"`{fp.name}`  —  {size_kb} KB  |  modified {mtime}")
                with open(fp, "rb") as fh:
                    c2.download_button(
                        "⬇",
                        data=fh.read(),
                        file_name=fp.name,
                        key=f"dl_{fp.name}",
                        use_container_width=True,
                    )

        _file_section("Trackers",        trackers)
        _file_section("Tailored Resumes", resumes)
        _file_section("Cover Letters",    covers)
        _file_section("Run Reports",      reports)
        _file_section("Other",            others)

    if st.session_state.resume_text:
        st.divider()
        st.subheader("Current Resume (loaded in sidebar)")
        st.text(st.session_state.resume_text)
