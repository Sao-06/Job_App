"""
Streamlit UI for Job Application Agent
Run:  streamlit run app.py
"""

import streamlit as st
import json
from pathlib import Path
from datetime import date, datetime

# Import core logic from agent.py
from agent import (
    OWNER_NAME, OUTPUT_DIR, RESOURCES_DIR, DEMO_JOBS,
    DemoProvider, OllamaProvider, AnthropicProvider,
    phase1_ingest_resume, phase2_discover_jobs, phase3_score_jobs,
    phase4_tailor_resume, phase5_simulate_submission, phase6_update_tracker,
    phase7_run_report, _build_demo_resume, _read_resume, _save_tailored_resume,
)

# ─── Page Config ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Job Application Agent",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Session State Defaults ──────────────────────────────────────────────────

for key, default in {
    "phase": 0,
    "profile": None,
    "jobs": [],
    "scored": [],
    "applications": [],
    "tracker_path": None,
    "report": None,
    "running": False,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ─── Sidebar: Configuration ─────────────────────────────────────────────────

with st.sidebar:
    st.title("⚙️ Configuration")

    # Mode selection
    st.subheader("1. Provider Mode")
    mode = st.radio(
        "Choose how the agent runs:",
        ["Demo (no API key)", "Ollama (local LLM)", "Claude (Anthropic API)"],
        index=0,
        help="Demo works instantly with no setup. Ollama requires a local install. Claude requires an API key.",
    )

    ollama_model = "llama3.2"
    if mode == "Ollama (local LLM)":
        ollama_model = st.text_input("Ollama model name", value="llama3.2")
    if mode == "Claude (Anthropic API)":
        api_key = st.text_input("ANTHROPIC_API_KEY", type="password")
        if api_key:
            import os
            os.environ["ANTHROPIC_API_KEY"] = api_key

    # Resume upload
    st.subheader("2. Resume")
    resume_source = st.radio(
        "Resume source:",
        ["Built-in demo profile", "Upload file"],
        index=0,
    )
    uploaded_file = None
    if resume_source == "Upload file":
        uploaded_file = st.file_uploader(
            "Upload resume (PDF, DOCX, or TXT)",
            type=["pdf", "docx", "txt"],
        )

    # Job search config
    st.subheader("3. Job Search")
    job_titles = st.text_input(
        "Target job titles (comma-separated)",
        value="IC Design Intern, Photonics Engineer Intern, FPGA/Hardware Intern",
    )
    location = st.text_input("Preferred location", value="Remote")
    threshold = st.slider("Auto-apply score threshold", 50, 100, 75, step=5)
    max_apps = st.slider("Max applications per run", 1, 20, 10)

    # Cover letter
    st.subheader("4. Options")
    cover_letter_mode = st.selectbox(
        "Cover letter",
        ["No", "Yes (all)", "Only for score ≥ 85"],
    )

    # Exclusions
    blacklist = st.text_input("Companies to exclude (comma-separated)", value="")

    st.markdown("---")
    run_button = st.button("▶ Run Agent", type="primary", use_container_width=True)
    reset_button = st.button("🔄 Reset", use_container_width=True)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_provider_from_mode(mode_str, model_name="llama3.2"):
    if "Demo" in mode_str:
        return DemoProvider()
    elif "Ollama" in mode_str:
        return OllamaProvider(model=model_name)
    else:
        return AnthropicProvider()


def get_resume_text(source, file):
    if source == "Upload file" and file is not None:
        suffix = Path(file.name).suffix.lower()
        if suffix in (".txt", ".md"):
            return file.read().decode("utf-8")
        elif suffix == ".docx":
            tmp = Path("_temp_resume.docx")
            tmp.write_bytes(file.read())
            text = _read_resume(tmp)
            tmp.unlink(missing_ok=True)
            return text
        else:
            return file.read().decode("utf-8", errors="replace")
    return _build_demo_resume()


def map_cover_letter_mode(selection):
    if selection == "Yes (all)":
        return "yes"
    if "85" in selection:
        return "only for ≥85"
    return "no"


# ─── Reset ───────────────────────────────────────────────────────────────────

if reset_button:
    for key in ["phase", "profile", "jobs", "scored", "applications", "tracker_path", "report", "running"]:
        if key == "phase":
            st.session_state[key] = 0
        elif key in ("applications", "jobs", "scored"):
            st.session_state[key] = []
        elif key == "running":
            st.session_state[key] = False
        else:
            st.session_state[key] = None
    st.rerun()


# ─── Main Area ───────────────────────────────────────────────────────────────

st.title("🤖 Job Application Agent")
st.caption("7-phase autonomous job search and application system")

# Phase progress bar
phase_names = [
    "Configure", "Ingest Resume", "Discover Jobs", "Score & Rank",
    "Tailor & Apply", "Excel Tracker", "Report",
]

cols = st.columns(len(phase_names))
for i, (col, name) in enumerate(zip(cols, phase_names)):
    if i < st.session_state["phase"]:
        col.markdown(f"✅ **{name}**")
    elif i == st.session_state["phase"]:
        col.markdown(f"🔵 **{name}**")
    else:
        col.markdown(f"⬜ {name}")

st.markdown("---")

# ─── Run Agent ───────────────────────────────────────────────────────────────

if run_button and not st.session_state["running"]:
    st.session_state["running"] = True
    st.session_state["phase"] = 0
    st.session_state["applications"] = []

    OUTPUT_DIR.mkdir(exist_ok=True)
    RESOURCES_DIR.mkdir(exist_ok=True)

    # Build config
    titles_list = [t.strip() for t in job_titles.split(",") if t.strip()]
    bl = [c.strip() for c in blacklist.split(",") if c.strip()]
    resume_text = get_resume_text(resume_source, uploaded_file)
    cl_mode = map_cover_letter_mode(cover_letter_mode)

    # Get provider
    try:
        provider = get_provider_from_mode(mode, ollama_model)
    except SystemExit:
        st.error("Could not connect to provider. Check your configuration.")
        st.session_state["running"] = False
        st.stop()

    # ── Phase 1: Ingest Resume ───────────────────────────────────────────
    st.session_state["phase"] = 1
    with st.status("Phase 1 — Ingesting Resume...", expanded=True) as status:
        profile = provider.extract_profile(resume_text)
        st.session_state["profile"] = profile

        if profile:
            st.write(f"**Name:** {profile.get('name', 'N/A')}")
            st.write(f"**Top Skills:** {', '.join(profile.get('top_hard_skills', [])[:6])}")
            if profile.get("resume_gaps"):
                st.warning(f"Resume gaps: {', '.join(profile['resume_gaps'])}")
            status.update(label="Phase 1 — Resume Ingested ✅", state="complete")
        else:
            st.error("Failed to parse resume.")
            st.session_state["running"] = False
            st.stop()

    # ── Phase 2: Discover Jobs ───────────────────────────────────────────
    st.session_state["phase"] = 2
    with st.status("Phase 2 — Discovering Jobs...", expanded=True) as status:
        jobs = phase2_discover_jobs(profile, titles_list, location, provider)
        if bl:
            jobs = [j for j in jobs if j.get("company", "").lower() not in {c.lower() for c in bl}]
        st.session_state["jobs"] = jobs
        st.write(f"Found **{len(jobs)}** job postings")
        status.update(label=f"Phase 2 — {len(jobs)} Jobs Found ✅", state="complete")

    # ── Phase 3: Score & Rank ────────────────────────────────────────────
    st.session_state["phase"] = 3
    with st.status("Phase 3 — Scoring Jobs...", expanded=True) as status:
        scored = phase3_score_jobs(jobs, profile, provider, min_score=60)
        st.session_state["scored"] = scored

        if scored:
            table_data = []
            for j in scored:
                s = j.get("score", 0)
                if s >= threshold:
                    badge = "✅ Auto-eligible"
                elif s >= 60:
                    badge = "⚠️ Review"
                else:
                    badge = "❌ Skip"
                table_data.append({
                    "Company": j.get("company", ""),
                    "Title": j.get("title", ""),
                    "Score": s,
                    "Location": j.get("location", ""),
                    "Status": badge,
                    "Platform": j.get("platform", ""),
                })
            st.dataframe(table_data, use_container_width=True, hide_index=True)

        auto_count = sum(1 for j in scored if j.get("score", 0) >= threshold)
        review_count = sum(1 for j in scored if 60 <= j.get("score", 0) < threshold)
        status.update(
            label=f"Phase 3 — {auto_count} auto-eligible, {review_count} for review ✅",
            state="complete",
        )

    # ── Phase 4 & 5: Tailor & Apply ─────────────────────────────────────
    st.session_state["phase"] = 4
    auto_eligible = [j for j in scored if j.get("score", 0) >= threshold]
    to_process = auto_eligible[:max_apps]

    if not to_process:
        st.warning("No jobs met the auto-apply threshold. Try lowering the score.")
        st.session_state["running"] = False
        st.stop()

    with st.status(f"Phase 4–5 — Tailoring & Applying ({len(to_process)} jobs)...", expanded=True) as status:
        progress = st.progress(0)
        applications = []

        for i, job in enumerate(to_process):
            st.write(f"**({i+1}/{len(to_process)})** {job['title']} @ {job['company']}  —  score: {job['score']}")

            include_cl = (
                cl_mode == "yes"
                or (cl_mode == "only for ≥85" and job.get("score", 0) >= 85)
            )

            tailored = phase4_tailor_resume(job, profile, resume_text, provider, include_cl)

            if tailored.get("ats_keywords_missing"):
                st.caption(f"ATS gaps: {', '.join(tailored['ats_keywords_missing'][:4])}")

            resume_file = _save_tailored_resume(job, tailored)
            result = phase5_simulate_submission(job)

            icon = "✅" if result["status"] == "Applied" else "⚠️"
            st.write(f"{icon} {result['status']}  •  Confirmation: `{result['confirmation']}`")

            applications.append({
                **job,
                "date_applied": datetime.now().strftime("%m/%d/%Y"),
                "resume_version": resume_file,
                "cover_letter_sent": bool(tailored.get("cover_letter")),
                "status": result["status"],
                "confirmation": result["confirmation"],
                "notes": (
                    "ATS gaps: " + ", ".join(tailored.get("ats_keywords_missing", [])[:2])
                    if tailored.get("ats_keywords_missing") else ""
                ),
            })

            progress.progress((i + 1) / len(to_process))

        # Add skipped/review jobs
        for job in scored:
            if job not in to_process:
                applications.append({
                    **job,
                    "date_applied": datetime.now().strftime("%m/%d/%Y"),
                    "resume_version": "", "cover_letter_sent": False,
                    "status": "Skipped", "confirmation": "N/A",
                    "notes": f"Score {job.get('score',0)} below threshold or max_apps reached",
                })

        st.session_state["applications"] = applications
        applied_count = sum(1 for a in applications if a["status"] == "Applied")
        status.update(
            label=f"Phase 4–5 — {applied_count} applications submitted ✅",
            state="complete",
        )

    # ── Phase 6: Excel Tracker ───────────────────────────────────────────
    st.session_state["phase"] = 5
    with st.status("Phase 6 — Creating Excel Tracker...", expanded=True) as status:
        tracker_path = phase6_update_tracker(applications)
        st.session_state["tracker_path"] = tracker_path
        if tracker_path and tracker_path.exists():
            st.write(f"Saved: `{tracker_path.name}`")
            with open(tracker_path, "rb") as f:
                st.download_button(
                    "📥 Download Tracker (.xlsx)",
                    data=f.read(),
                    file_name=tracker_path.name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
        status.update(label="Phase 6 — Tracker Created ✅", state="complete")

    # ── Phase 7: Report ──────────────────────────────────────────────────
    st.session_state["phase"] = 6
    with st.status("Phase 7 — Generating Report...", expanded=True) as status:
        applied_list = [a for a in applications if a.get("status") == "Applied"]
        manual_list = [a for a in applications if a.get("status") == "Manual Required"]
        skipped_list = [a for a in applications if a.get("status") == "Skipped"]
        top3 = sorted(applied_list, key=lambda x: x.get("score", 0), reverse=True)[:3]

        report_text = provider.generate_report({
            "total_found": len(applications),
            "applied": len(applied_list),
            "manual": len(manual_list),
            "skipped": len(skipped_list),
            "top3_applied": [(a["company"], a["title"], a["score"]) for a in top3],
            "manual_reasons": [a.get("notes", "Form requires manual review") for a in manual_list],
        })

        st.session_state["report"] = report_text
        status.update(label="Phase 7 — Report Generated ✅", state="complete")

    # ── Done ─────────────────────────────────────────────────────────────
    st.session_state["phase"] = 7
    st.session_state["running"] = False

    st.success("Agent run complete!")
    st.markdown("### 📊 Run Report")
    st.text(report_text)

    st.markdown("---")
    st.markdown("**Next steps:**")
    st.markdown("- Review any 'Manual Required' rows in the tracker")
    st.markdown("- Update 'Response Received' column as replies arrive")
    st.markdown("- Follow up on applied jobs in 7 days")

# ─── Show previous results if not running ────────────────────────────────────

if not run_button and st.session_state.get("report"):
    st.markdown("### 📊 Previous Run Report")
    st.text(st.session_state["report"])

    if st.session_state.get("tracker_path"):
        tp = st.session_state["tracker_path"]
        if tp.exists():
            with open(tp, "rb") as f:
                st.download_button(
                    "📥 Download Tracker (.xlsx)",
                    data=f.read(),
                    file_name=tp.name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

elif not run_button and st.session_state["phase"] == 0:
    st.info("👈 Configure settings in the sidebar and click **Run Agent** to start.")
