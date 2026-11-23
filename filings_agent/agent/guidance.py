"""Shared forward-looking guidance normalization & scoring (filings_agent wrapper).

Canonical implementation lives in ``shared.guidance``.
"""
from __future__ import annotations

from typing import Any

from shared.guidance import *  # noqa: F401, F403
from shared.guidance import (
    _period_type_binary,  # noqa: F401
    normalize_guidance_records as _shared_normalize_guidance_records,
)


def normalize_guidance_records(
    raw_records: Any,
    filing_period: Any,
    doc_currency: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    return _shared_normalize_guidance_records(
        raw_records, filing_period, doc_currency, usd_only=True
    )
