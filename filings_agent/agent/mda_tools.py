"""Backward-compatible re-export of MD&A navigation tools.

Canonical implementation has moved to `filings_agent.tools.guidance_tools`.
"""
from __future__ import annotations

from filings_agent.tools.guidance_tools import build_mda_tools

__all__ = ["build_mda_tools"]
