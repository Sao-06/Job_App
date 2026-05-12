"""
pipeline/sources/
─────────────────
Pluggable job-source providers. Each provider exposes the JobSource
protocol from :mod:`pipeline.sources.base` and registers itself on
import. The active registry is exposed via :func:`registry`.

Sources that need an API key skip registration silently when the env
var is missing — making the system "do its best with whatever is wired".
"""

from __future__ import annotations

from typing import List

from .base import JobSource, RawJob, canonical_url, normalize_company
from .registry import register, registry, get


# Importing each module triggers self-registration via ``register(...)``.
# Order doesn't matter for correctness, but we import deterministically
# so the dev-page source list is stable.
from . import github_readme       # noqa: F401
from . import api_themuse         # noqa: F401
from . import api_remoteok        # noqa: F401
from . import api_jobicy          # noqa: F401
from . import api_himalayas       # noqa: F401
from . import api_remotive        # noqa: F401
from . import api_arbeitnow       # noqa: F401
from . import api_weworkremotely  # noqa: F401  (global remote — RSS, no key)
# Keyed sources — each self-registers only when its env var is set.
from . import api_usajobs         # noqa: F401
from . import api_adzuna          # noqa: F401
from . import api_reed            # noqa: F401
from . import api_jooble          # noqa: F401
from . import api_findwork        # noqa: F401
# ATS sources
from . import ats_greenhouse      # noqa: F401
from . import ats_lever           # noqa: F401
from . import ats_ashby           # noqa: F401
from . import ats_workable        # noqa: F401
from . import ats_smartrecruiters # noqa: F401
from . import ats_recruitee       # noqa: F401
from . import ats_workday         # noqa: F401
# Library-backed scrapers — register only if the optional pip dep is installed.
from . import scraper_jobspy      # noqa: F401


__all__ = [
    "JobSource", "RawJob", "canonical_url", "normalize_company",
    "register", "registry", "get",
]
