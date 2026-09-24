"""Small numeric/currency helpers used by the guidance port.

These are the minimal pieces the guidance normalizer needs from the 8-K
agent's number-handling code (``agent/loop.py``, ``agent/currency.py``,
``agent/derive.py``).  They are kept here (rather than pulling in those large
modules) so the filings agent stays free of 8-K extraction machinery.
"""
from __future__ import annotations

from typing import Any

# Unit multipliers used by the guidance contract (scale is metadata, values are
# normalized to base units before storage).
SCALE_MULTIPLIERS: dict[str, int] = {
    "millions": 1_000_000,
    "thousands": 1_000,
    "billions": 1_000_000_000,
}


def is_usd_safe(currency: str | None) -> bool:
    """Return True when *currency* can be persisted as USD.

    Only an explicit or symbol-implied ``USD`` is safe.  ``unknown``, foreign
    codes, and ``mixed`` are never safe without resolution.
    """
    return currency == "USD"


def coerce_number(v: Any) -> float | None:
    """Coerce an LLM-emitted value into a signed float, or ``None``.

    Accepts JSON numbers and numeric strings in SEC/IR styles:
    ``-1234``, ``-1,234``, ``"1,234"``, ``"$1,234"``, ``"(1,234)"``,
    ``"($1,234)"``, ``"-(1,234)"``.

    Parenthesized amounts are NEGATIVE (SEC convention) — ``(175,685)``
    becomes ``-175685.0``.  Booleans and non-numeric text return ``None``.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    # Drop leading currency symbols + whitespace BEFORE the paren check so
    # "$ (1,234)" is recognised as parenthesized.
    s = s.lstrip("$\u20ac\u00a3 \t\n\r")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    s = (
        s.replace("$", "")
        .replace(",", "")
        .replace("\xa0", "")
        .replace(" ", "")
    )
    if not s:
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    return -abs(value) if negative else value
