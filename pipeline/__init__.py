"""
pipeline/__init__.py
────────────────────
Public API for the pipeline package.

Importing `pipeline` (or `import pipeline as _ag`) gives access to every
function, class, and constant needed by streamlit_app.py and agent.py.

The `helpers` sub-module is also re-exported as an attribute so callers can
read the live `_last_merge_count` via `pipeline.helpers._last_merge_count`.
"""

# Sub-module reference (needed for live access to mutable module-level state)
from . import helpers  # noqa: F401 — exposes pipeline.helpers._last_merge_count

# Constants & utilities
from .config import (  # noqa: F401
    OWNER_NAME, OUTPUT_DIR, RESOURCES_DIR, TODAY, MAX_SCRAPE_JOBS,
    console, _CliSpinner, DEMO_JOBS,
)

# Job helpers
from .helpers import (  # noqa: F401
    infer_experience_level, infer_education_required,
    infer_citizenship_required, deduplicate_jobs,
)

# LaTeX utilities
from .latex import (  # noqa: F401
    detect_latex, latex_to_plaintext,
    remove_summary_section, apply_tailoring_to_latex, compile_latex_to_pdf,
)

# Resume I/O
from .resume import (  # noqa: F401
    _build_demo_resume, _read_resume, _save_tailored_resume,
)

# LLM providers
from .providers import (  # noqa: F401
    BaseProvider, AnthropicProvider, DemoProvider, OllamaProvider, get_provider,
)

# Scrapers
from .scrapers import (  # noqa: F401
    JobBoardClient, JobSpyClient, IndeedClient, SimplifyJobsScraper,
)

# Phase functions & supporting classes
from .phases import (  # noqa: F401
    phase1_ingest_resume,
    phase2_discover_jobs,
    phase3_score_jobs,
    phase4_tailor_resume,
    PlaywrightSubmitter,
    _load_existing_applications,
    phase5_simulate_submission,
    phase6_update_tracker,
    phase7_run_report,
    _send_email_notification,
    _launch_dashboard_and_wait,
)
