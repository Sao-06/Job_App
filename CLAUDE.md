# Project Context

This is my AI agent workspace. I use it for research, content creation, and productivity workflows, including resume/CV generation and editing, AI-initiated job applications, and tracking applications and results.

# About Me

<!-- TODO: Replace this section with a brief description of yourself -->
I am a [undergraduate/graduate] student at [University Name]. As of [Semester Year] I am a [year] studying [Major]. My interests: [interest 1], [interest 2], [interest 3]. I am building [field] projects to improve my internship/job candidacy.

# Goals

<!-- TODO: Update with your own goals -->
- Short-term: [e.g., build 2–3 small projects; prepare targeted resumes and applications]
- Medium-term: [e.g., secure internship offers in X or Y field]
- Long-term: [e.g., graduate with a portfolio of projects and research experience]

# Priorities

- Primary: Produce a high-quality, tailored resume/CV for each job application.
- Secondary: Build and document technical projects that align with internship targets.
- Tertiary: Research companies' projects and tailor applications accordingly.

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
- Filename pattern: YYYYMMDD_task_short-description.ext (example: 20260328_resume_CompanyX.pdf)
- Formats: drafts as Markdown or Google Docs links; final resumes as PDF; code or data in clearly named subfolders.
- Citation style: inline links + final sources list.
- Sample Prompts (examples for consistency)
  - "Help me draft a 1-page resume targeting a summer [role] internship at [Company] — emphasize [skill 1], [skill 2], and [skill 3]."
  - "Generate a README for my [project name] project with setup, block diagram, and test instructions."
  - "Compare job posting A and my resume; produce a prioritized edit list."


# Templates & Checklists

- Resume checklist: contact, 1-line summary, 3–5 bullets per role/project, skills matrix, projects with outcome/metrics.
- Project README template: Overview, Hardware/Software, Build, Test, Results, Next steps.

# Project Structure

- workflows/ — workflow instruction files (plain-English recipes the agent follows)
- output/ — finished deliverables (reports, drafts, analysis, summaries, code artifacts)
- resources/ — reference docs, templates, and saved job postings
- **workflows/job-application-agent.md** — Full Job Application Agent spec (7 phases, scoring model, tracker schema, startup checklist)

# Environment & Preferences

- OS: [Windows / macOS / Linux]
- Timezone: [Your timezone] — mention if time-sensitive scheduling is needed.
- Tone: professional, concise, and helpful.
- Verbosity: default short; expand on request.
- Languages & tools: [list your languages and tools, e.g., Python, MATLAB, C++]

# Contact & Ownership
- Owner: [YOUR FULL NAME]
- Preferred contact: [your.email@example.com]

# Fillable Template  ← fill in your details here

- Full name: [YOUR FULL NAME]
- LinkedIn URL: [https://www.linkedin.com/in/your-profile]
- Current university and major: [University Name — Major]
- Current year: [freshman / sophomore / junior / senior / graduate], [Semester Year]
- Top 3 technical interests: [Interest 1], [Interest 2], [Interest 3]
- Key skills & tools (list):
    - [Skill 1]
    - [Skill 2]
    - [Skill 3]
    - [Tool 1]
    - [Tool 2]
- Primary internship/job targets (companies or roles):
    - Primary: [Role type — Company 1, Company 2, Company 3]
    - Secondary: [Role type — Company 4, Company 5]
- Job Boards: LinkedIn, Indeed, Glassdoor, ZipRecruiter, Wellfound, Dice, Handshake (add/remove as needed)
- Preferred tone and verbosity: [concise / detailed], [professional / casual]
- Any sensitive data or privacy constraints: [e.g., do not mention X unless required]
- Preferred resume format: [1 page / 2 page]
- Permissions: Agent may create/commit files: [yes / no]
