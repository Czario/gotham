"""Backward-compatible re-export of hierarchy tools.

Canonical implementation has moved to `filings_agent.tools.hierarchy_tools`.
"""
from __future__ import annotations

from filings_agent.tools.hierarchy_tools import (
    PRIMARY_PREFIXES,
    build_unified_hierarchy_tools,
    fmt_row,
    looks_primary,
)

# Backward-compatible aliases
_fmt_row = fmt_row
_looks_primary = looks_primary
_PRIMARY_PREFIXES = PRIMARY_PREFIXES

__all__ = [
    "PRIMARY_PREFIXES",
    "_PRIMARY_PREFIXES",
    "_fmt_row",
    "_looks_primary",
    "build_unified_hierarchy_tools",
    "fmt_row",
    "looks_primary",
]
