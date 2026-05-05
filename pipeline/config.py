"""
pipeline/config.py
──────────────────
Global constants, the Rich console, the CLI spinner, and the DEMO_JOBS list.
All other pipeline modules import from here instead of defining their own.
"""

import threading
import io
import sys
from pathlib import Path
from datetime import date

from rich.console import Console
from rich.panel import Panel  # noqa: F401 – re-exported for convenience

if hasattr(sys.stdout, "buffer") and "utf" not in (sys.stdout.encoding or "").lower():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer") and "utf" not in (sys.stderr.encoding or "").lower():
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── User-editable constants ────────────────────────────────────────────────────
OWNER_NAME       = "Your Name"   # TODO: replace with your full name
_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
OUTPUT_DIR       = _PROJECT_ROOT / "output"
RESOURCES_DIR    = _PROJECT_ROOT / "resources"
# Server-private state (auth tables, session JSON, job index). Lives OUTSIDE
# OUTPUT_DIR because OUTPUT_DIR is exposed by the GET /output/{path} static
# route — we never want a path-traversal-safe lookup to be able to surface
# this database.
DATA_DIR         = _PROJECT_ROOT / "data"
DB_PATH          = DATA_DIR / "jobs_ai_sessions.sqlite3"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
TODAY            = date.today().strftime("%m/%d/%Y")
MAX_SCRAPE_JOBS  = 50            # cap on jobs collected in Phase 2


def _legacy_db_in_use() -> bool:
    """Best-effort check: is some other process actively writing to the
    legacy ``output/jobs_ai_sessions.sqlite3``? If so, skip the rename —
    moving the main file out from under an open SQLite connection is the
    canonical way to produce a "database disk image is malformed" error.

    Detection is intentionally cheap and conservative: if we can take an
    exclusive lock on either the WAL or the SHM file (both of which only
    exist while a SQLite handle is open), we assume nobody else has them.
    On Windows, opening these files with write access fails when another
    process holds them — that failure is exactly the signal we want.
    """
    legacy = OUTPUT_DIR / "jobs_ai_sessions.sqlite3"
    for tail in ("-wal", "-shm"):
        sibling = legacy.with_name(legacy.name + tail)
        if not sibling.exists():
            continue
        try:
            # Try to open append-mode (write); if locked, OSError.
            with open(sibling, "a+b"):
                pass
        except OSError:
            return True
    return False


def migrate_db_path() -> None:
    """One-shot rename of the legacy ``output/jobs_ai_sessions.sqlite3*``
    files to the new private ``data/`` location.

    Three cases:
      1. Cold migration — DB only exists at the legacy path: rename it.
      2. Stale leftover — DB already moved, but the legacy file is still
         on disk from before the move. Delete it.
      3. Live writer — another process (typically a parallel uvicorn or
         CLI run) has the legacy DB open. Bail out completely and let
         the existing process keep using the legacy path. Subsequent boots
         will retry once the writer exits.
    """
    legacy = OUTPUT_DIR / "jobs_ai_sessions.sqlite3"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Skip everything if a live writer is detected. Better to leave the
    # legacy file alone than to corrupt it mid-write.
    if legacy.exists() and _legacy_db_in_use():
        return

    for suffix in ("", "-wal", "-shm"):
        old = legacy.with_name(legacy.name + suffix)
        new = DB_PATH.with_name(DB_PATH.name + suffix)
        if not old.exists():
            continue
        if not new.exists():
            # Case 1: rename across.
            try:
                old.rename(new)
                continue
            except OSError:
                import shutil
                try:
                    shutil.copy2(old, new)
                except OSError:
                    continue
        # Case 2 (or post-rename fallback): purge the legacy file.
        try:
            old.unlink()
        except OSError:
            # File still locked — best-effort only.
            pass


# Run the migration at import time so the rest of the app sees the new path.
# Tests set JOBS_AI_SKIP_MIGRATION=1 to neutralize this import-time side effect.
import os as _os_for_migration
if not _os_for_migration.environ.get("JOBS_AI_SKIP_MIGRATION"):
    migrate_db_path()

console = Console()


# ── CLI loading-spinner ────────────────────────────────────────────────────────

class _CliSpinner:
    """Background thread that prints a status message every N seconds."""

    _MESSAGES = [
        "Still working — LLM is processing…",
        "Hang tight — fetching results…",
        "Still running — this can take a moment…",
        "Processing in the background…",
        "Almost there — please wait…",
    ]

    def __init__(self, messages=None, interval: int = 160):
        self._msgs = messages or self._MESSAGES
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        idx = 0
        while not self._stop.wait(self._interval):
            console.print(f"  [dim]⏳ {self._msgs[idx % len(self._msgs)]}[/dim]")
            idx += 1

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()


# ── Hardcoded demo jobs ────────────────────────────────────────────────────────

DEMO_JOBS = [
    {
        "id": "job_001", "title": "IC Design Engineering Intern",
        "company": "NVIDIA", "location": "Santa Clara, CA", "remote": False,
        "posted_date": date.today().isoformat(),
        "description": "Join NVIDIA's IC design team to work on next-gen GPU silicon.",
        "requirements": ["Verilog", "SPICE", "CMOS", "digital logic", "MATLAB", "Python"],
        "salary_range": "$40–$55/hr",
        "application_url": "https://nvidia.com/careers/intern-ic",
        "platform": "LinkedIn",
    },
    {
        "id": "job_002", "title": "Photonics Engineering Intern",
        "company": "Lumentum", "location": "San Jose, CA", "remote": False,
        "posted_date": date.today().isoformat(),
        "description": "Work on photonic integrated circuits and laser component characterization.",
        "requirements": ["Photolithography", "Optical characterization", "MATLAB",
                         "thin film", "Python", "cleanroom"],
        "salary_range": "$35–$45/hr", "application_url": "https://lumentum.com/careers",
        "platform": "Indeed",
    },
    {
        "id": "job_003", "title": "FPGA/Hardware Engineering Intern",
        "company": "Intel", "location": "Hillsboro, OR", "remote": True,
        "posted_date": date.today().isoformat(),
        "description": "Develop and verify FPGA designs for Intel's programmable solutions group.",
        "requirements": ["Verilog", "VHDL", "FPGA", "digital design", "Python", "Linux"],
        "salary_range": "$38–$50/hr", "application_url": "https://intel.com/jobs",
        "platform": "Handshake",
    },
    {
        "id": "job_004", "title": "Semiconductor Process Engineering Intern",
        "company": "Micron Technology", "location": "Boise, ID", "remote": False,
        "posted_date": date.today().isoformat(),
        "description": "Support semiconductor fabrication process development and yield improvement.",
        "requirements": ["Cleanroom processes", "SPICE", "data analysis",
                         "Python", "MATLAB", "SEM"],
        "salary_range": "$36–$48/hr", "application_url": "https://micron.com/careers",
        "platform": "Glassdoor",
    },
    {
        "id": "job_005", "title": "Mixed-Signal IC Design Intern",
        "company": "Apple", "location": "Cupertino, CA", "remote": False,
        "posted_date": date.today().isoformat(),
        "description": "Design and simulate mixed-signal circuits for Apple Silicon.",
        "requirements": ["SPICE", "Verilog", "analog design", "CMOS", "Python", "MATLAB"],
        "salary_range": "$45–$60/hr", "application_url": "https://apple.com/jobs",
        "platform": "LinkedIn",
    },
    {
        "id": "job_006", "title": "Hardware Engineering Intern",
        "company": "Microsoft", "location": "Redmond, WA", "remote": True,
        "posted_date": date.today().isoformat(),
        "description": "Contribute to custom silicon and hardware design for Azure infrastructure.",
        "requirements": ["FPGA", "Verilog", "Python", "C++", "digital design", "schematic"],
        "salary_range": "$42–$55/hr", "application_url": "https://microsoft.com/careers",
        "platform": "Indeed",
    },
    {
        "id": "job_007", "title": "Nanoelectronics Research Intern",
        "company": "IBM Research", "location": "Yorktown Heights, NY", "remote": False,
        "posted_date": date.today().isoformat(),
        "description": "Research novel semiconductor devices and nanoelectronics fabrication methods.",
        "requirements": ["Cleanroom processes", "Photolithography", "device physics",
                         "MATLAB", "Python", "SEM"],
        "salary_range": "$38–$50/hr",
        "application_url": "https://research.ibm.com/careers",
        "platform": "Handshake",
    },
    {
        "id": "job_008", "title": "EE Hardware Design Intern",
        "company": "Samsung Semiconductors", "location": "San Jose, CA", "remote": False,
        "posted_date": date.today().isoformat(),
        "description": "Support hardware design and verification for Samsung's memory products.",
        "requirements": ["Verilog", "SPICE", "MATLAB", "Python", "digital logic", "PCB design"],
        "salary_range": "$40–$52/hr", "application_url": "https://samsung.com/us/careers",
        "platform": "LinkedIn",
    },
]
