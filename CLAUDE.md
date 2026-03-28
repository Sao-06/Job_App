# Project Context

This is my AI agent workspace. I use it for research, content creation, and productivity workflows, including resume/CV generation and editing, AI-initiated job applications, and tracking applications and results.

# About Me

I am an international undergraduate student in Oklahoma. As of Spring 2026 I am a sophomore studying Electrical Engineering. My interests: integrated circuits, chip design, photonics, nanoelectronics, device physics, and solid-state physics. I am building electrical engineering projects to improve my internship candidacy.

# Goals

- Short-term: build 2–3 small EE projects (IC/prototyping/photonic demos); prepare targeted resumes and applications.
- Medium-term: secure internship offers in IC design, photonics, or related fields.
- Long-term: graduate with a portfolio of hardware projects and research experience.

# Priorities

- Primary: Produce a high-quality, tailored resume/CV for each job application.
- Secondary: Build and document technical projects that align with internship targets.
- Tertiary: Research companies’ projects and tailor applications accordingly.

# Rules & Agent Behavior
- Clarify: Always ask clarifying questions before starting a complex task.
- Plan-first: Show a short plan and steps before executing any multi-step task.
- Concise outputs: Prefer bullet-point summaries over long paragraphs.
- Save outputs: Save all final deliverables to the output/ folder.
- Citations: Cite sources when doing research (include links and brief notes).
- Background updates: Keep personal timeline and status up to date when relevant (ask before making public changes).
- Permissions: Ask before accessing or modifying private accounts or external services.
- Include Next Steps suggestion whenever beneficial
### Job Application Agent Configuration
- Auto-apply threshold: match score ≥ 75 (requires no review)
- Manual review: 60–79 match score
- Skip if: score < 60
- Default job search date range: last 14 days
- Max automated applications per run: 20
- Credentials: May create temporary job application accounts; delete session credentials after run
- Tracker filename: Job_Applications_Tracker_YYYY-MM.xlsx (saved to output/)

# How to Run Job Application Agent

1. Ensure resume is saved as PDF or DOCX in output/ or resources/
2. Provide the startup checklist items (from workflows/job-application-agent.md)
3. Agent will handle Phases 1–7 automatically
4. Check output/Job_Applications_Tracker_YYYY-MM.xlsx for results after run


# Output Conventions

- Folder: output/
Filename pattern: YYYYMMDD_task_short-description.ext (example: 20260328_resume_CompanyX.pdf)
- Formats: drafts as Markdown or Google Docs links; final resumes as PDF; code or data in clearly named subfolders.
- Citation style: inline links + final sources list.
- Sample Prompts (examples for consistency)
“Help me draft a 1-page resume targeting a summer IC design internship at Company X — emphasize Verilog, SPICE simulation, and mixed-signal projects.”
“Generate a README for my photonics demo project with setup, block diagram, and test instructions.”
- “Compare job posting A and my resume; produce a prioritized edit list.”
- whenenver push or commit to GitHub, 


# Templates & Checklists

- Resume checklist: contact, 1-line summary, 3–5 bullets per role/project, skills matrix, projects with outcome/metrics.
- Project README template: Overview, Hardware, Software, Build, Test, Results, Next steps.

# Project Structure

- workflows/ — workflow instruction files (plain-English recipes the agent follows)
- output/ — finished deliverables (reports, drafts, analysis, summaries, code artifacts)
- resources/ — reference docs, templates, and saved job postings
- **workflows/job-application-agent.md** — Full Job Application Agent spec (7 phases, scoring model, tracker schema, startup checklist)

# Environment & Preferences

- OS: Windows (local environment)
- Timezone: Oklahoma (Central Time) — mention if time-sensitive scheduling is needed.
- Tone: professional, concise, and helpful.
- Verbosity: default short; expand on request.
- Languages & tools: Python, MATLAB, SPICE, Verilog/VHDL, LaTeX (specify when tasks require a language).

# Contact & Ownership
- Owner: Sao Aphisith Sithisack
- Preferred contact: sao.sithisack@ou.edu.

# Fillable Template  (add or update values)

- Full name: Sao Aphisith Sithisack
- LinkedIn URL: www.linkedin.com/in/saoaphisithsithisack
- Current university and major: Electrical & Computer Engineering
- Current year: sophomore, Spring 2026
- Top 3 technical interests: Hardware, FPGA, IC
- Key skills & tools (list): 
    - Pulsed Laser Deposition
    - Photolithography
    - Cleanroom Processes
    - Data visualization using MATLAB, Python, Java, and LaTex
    - Physics Research
    - CAD: OnShape, Fusion360, SolidWorks
    - STEM Tutoring & Mentorship (CRLA Level 2)
- Primary internship targets (companies or roles):
    - Primary: Design — NVIDIA, Microsoft, Apple, IBM, Micron, Intel, and Samsung
    - Secondary: Manufacturing -- TSMC (Taiwan) & ASML, Lumentum
- Job Board: LinkedIn, Indeed, Glassdoor, ZipRecruiter, Wellfound, Dice, Handshake, and Intertride
- Preferred tone and verbosity: concise, professional , and respectful
- Any sensitive data or privacy constraints: Avoid mentioning international student status unless required; ask before sharing immigration details
- Preferred resume formats: 1 page
- Permissions: Agent may create/commit files: yes

# Git
1. Privacy & Identity: > * Do not use any local Git configurations or personal identity files.

Use the following placeholders for the commit identity:

Name: [REDACTED_USER]

Email: [REDACTED_EMAIL]

Use the placeholder [TARGET_REPO_URL] for the destination.

2. Ephemeral Workspace (No Local Footprint):

Create a temporary, in-memory, or transient directory for this operation (e.g., using a temp folder provided by the OS).

Do not initialize the repository in the current working directory.

Mandatory Cleanup: Once the git push command is confirmed as successful (or if it fails), immediately delete the entire temporary workspace. No repository metadata (.git folders) should remain on the laptop.

3. Execution Workflow:

Copy the current file data into the temporary directory.

Initialize a new git instance: git init.

Set local config: git config user.name "[REDACTED_USER]" and git config user.email "[REDACTED_EMAIL]".

Commit and push using a secure token placeholder: https://[TOKEN]@github.com/[REPO_PATH].git.
- Always ask to branch or merge. After that, the agent is given permission to commit everything on my behalf without a need to ask for permission.