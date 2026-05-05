# Jobs AI — Full Technical Specification

This document provides an exhaustive, low-level map of the Jobs AI codebase.

## 1. Directory Structure

```text
/
├── agent.py                 # CLI entry point; handles arguments and orchestrates pipeline/phases.py.
├── app.py                   # FastAPI backend; exposes REST endpoints and streams SSE logs.
├── session_store.py         # SQLite-backed persistence for user state and multi-user sessions.
├── streamlit_app.py         # Legacy Streamlit UI (standalone).
├── requirements.txt         # Project dependencies (FastAPI, Anthropic, Playwright, pdfplumber, etc.).
├── .claude/
│   └── agents/              # Markdown instruction files for specialized sub-agents.
├── config/
│   └── skill_keywords.yaml  # YAML for DemoProvider matching logic.
├── dashboard/
│   └── app.py               # Lightweight Flask app for viewing/approving Excel tracker entries.
├── frontend/
│   ├── index.html           # Main SPA shell; contains embedded CSS design tokens and layout.
│   ├── app.jsx              # React application source (Babel-transpiled in browser).
│   └── landing.html         # Optional static landing page.
├── pipeline/                # CORE LOGIC
│   ├── __init__.py          # Package initialization.
│   ├── config.py            # Shared constants, paths (OUTPUT_DIR), and the CLI spinner.
│   ├── helpers.py           # General utilities (date parsing, string cleaning, file I/O).
│   ├── latex.py             # LaTeX detection, conversion to text, and PDF compilation.
│   ├── phases.py            # Implementation of the 7 pipeline phases (Ingest, Discover, etc.).
│   ├── profile_audit.py     # Post-extraction validation logic for resume profiles.
│   ├── providers.py         # LLM Provider implementations (Anthropic, Ollama, Demo).
│   ├── resume.py            # PDF/DOCX extraction and LaTeX/ReportLab resume generation.
│   └── scrapers.py          # Job board scraping logic (JobSpy, SimplifyJobs).
├── resources/               # JSON caches for LLM profiles and extracted data.
└── output/                  # Generated resumes (.tex/.pdf), trackers (.xlsx), and reports (.md).
    └── jobs_ai_sessions.sqlite3 # The primary database for all user state.
```

## 2. Core Module Details (No Abstraction)

### 2.1 Backend: `app.py`
- **State Management**: Uses `_load_session(request)` to retrieve/create a unique `session_id` from cookies.
- **SSE Streaming**: `_run_phase_sse` wraps pipeline functions in a thread, piping `console.print` and status updates to a `queue.Queue` which is yielded as a `ServerSentEvent`.
- **API Endpoints**:
  - `/api/state`: Returns the full `normalize_state` from SQLite.
  - `/api/phase/{n}/run`: Triggers a specific phase in the background.
  - `/api/resume/upload`: Handles multi-part file uploads and triggers `_read_resume`.
  - `/api/ollama/status`: Checks if local Ollama is running and lists pulled models.

### 2.2 Pipeline Phases: `pipeline/phases.py`
- **Phase 1 (Ingest)**: `_read_resume` → `provider.extract_profile` → `audit_profile`.
- **Phase 2 (Discover)**: Calls `scrapers.scrape_all` based on `job_titles` and `location`.
- **Phase 3 (Score)**: Two-step scoring. 1. Fast keyword match. 2. LLM `score_job` for top N candidates.
- **Phase 4 (Tailor)**: `provider.tailor_resume` → `_save_tailored_resume` (produces .tex and .pdf).
- **Phase 6 (Track)**: Uses `openpyxl` to write/update `Job_Applications_Tracker_{month}.xlsx`.

### 2.3 LLM Providers: `pipeline/providers.py`
- **`BaseProvider`**: Abstract class defining `extract_profile`, `score_job`, and `tailor_resume`.
- **`AnthropicProvider`**: Uses `anthropic` SDK with tool-calling (forced JSON output via system prompt).
- **`OllamaProvider`**: Uses `openai` SDK (Ollama is OpenAI-compatible) with strict JSON schema instructions.
- **JSON Schema**: Defined explicitly for each method to ensure consistent structured data from the LLM.

### 2.4 Extraction & Generation: `pipeline/resume.py`
- **Extraction Fallback**: `pypdfium2` → `pdfplumber` → `pypdf` → `pdfminer.six`.
- **Analysis**: Includes a `critical_analysis` field where the LLM provides a 4-paragraph critique of the resume.
- **PDF Generation**:
  1. `pdflatex` (if available in system path).
  2. `reportlab` (Pure Python fallback using standard `SimpleDocTemplate`).

### 2.5 Persistence: `session_store.py`
- **Schema**: Two tables: `sessions` (metadata) and `session_state` (blob of JSON).
- **Serialization**: `json_default` handles `Set` and `Path` objects. `normalize_state` restores them upon loading.

## 3. Frontend Implementation: `frontend/app.jsx`
- **State**: A single `state` object fetched from `/api/state` and updated via `refresh()`.
- **Navigation**: Controlled by a `page` state variable; renders components conditionally (e.g., `<JobsPage/>`, `<ResumePage/>`).
- **Components**:
  - `AgentPage`: Visualizes the 7 phases with progress rings and real-time logs.
  - `ResumePage`: Shows a "Critical Analysis" dashboard and a raw text "Preview" tab.
  - `SettingsPage`: Configures LLM backends (Ollama vs Anthropic) and search parameters.
- **API Wrapper**: A centralized `api` object using `fetch` with automatic JSON handling and error reporting.

## 4. Operational Mandates
1. **Security**: Never commit `api_key` to disk; it lives in the volatile memory of the session store or is entered via UI.
2. **UI Style**: Dark mode purple/indigo (`--bg`, `--accent`). Use `lucide` icons via the `<Icon/>` wrapper.
3. **Extraction**: Always verify `pypdfium2` is the primary PDF engine.
4. **Configuration**: Pathing must always use `Path(__file__)` logic to ensure cross-platform compatibility (Windows/Linux).
