"""
Global, lightweight source registry. Lives in its own module so source
files can register themselves without importing ``__init__`` (which
itself imports them).
"""

from __future__ import annotations

from typing import Dict, List

from .base import JobSource


_REGISTRY: Dict[str, JobSource] = {}


def register(source: JobSource) -> None:
    name = getattr(source, "name", None) or source.__class__.__name__
    _REGISTRY[name] = source


def registry() -> List[JobSource]:
    """Return all registered sources, sorted by name for stable dev UI."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def get(name: str) -> JobSource | None:
    return _REGISTRY.get(name)
