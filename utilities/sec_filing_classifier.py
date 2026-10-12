"""SEC filing eligibility helpers for foreign-issuer annual reports.

This module deliberately classifies filings without scheduling or processing
any form. The annual 20-F ingestion path can use this helper in a later phase.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any, Mapping, Optional


ANNUAL_20F_FORMS = frozenset({"20-F", "20-F/A"})

# SEC Inline XBRL cover-page facts provide an unambiguous report-type signal.
_DEI_FACT_NAMES = {
    "document_type": "dei:DocumentType",
    "annual_report": "dei:DocumentAnnualReport",
    "registration_statement": "dei:DocumentRegistrationStatement",
    "transition_report": "dei:DocumentTransitionReport",
    "shell_company_report": "dei:DocumentShellCompanyReport",
    "period_end": "dei:DocumentPeriodEndDate",
    "fiscal_period_focus": "dei:DocumentFiscalPeriodFocus",
}
_INLINE_FACT_RE = re.compile(
    r"<ix:(?:nonNumeric|nonFraction)\b([^>]*)>(.*?)"
    r"</ix:(?:nonNumeric|nonFraction)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_ATTRIBUTE_RE = re.compile(r"\bname\s*=\s*(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _sec_date(value: Any) -> Optional[str]:
    """Return a normalized SEC YYYY-MM-DD date, or None when invalid/missing."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _extract_inline_dei_facts(document: str) -> dict[str, str]:
    """Extract the small set of Inline XBRL DEI cover facts used for routing."""
    wanted = {name: key for key, name in _DEI_FACT_NAMES.items()}
    facts: dict[str, str] = {}
    for match in _INLINE_FACT_RE.finditer(document):
        attr_match = _ATTRIBUTE_RE.search(match.group(1))
        if not attr_match:
            continue
        key = wanted.get(attr_match.group(2).strip())
        if not key or key in facts:
            continue
        value = _TAG_RE.sub(" ", match.group(2))
        facts[key] = " ".join(html.unescape(value).split())
    return facts


def _as_sec_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    normalized = value.strip().upper()
    if normalized in {"TRUE", "1", "YES"}:
        return True
    if normalized in {"FALSE", "0", "NO"}:
        return False
    return None


def _cover_marks_annual_report(document: str) -> bool:
    """Recognize a checked annual-report option on non-iXBRL/older covers."""
    # Handle filings that encode checkbox states as HTML inputs rather than glyphs.
    document = re.sub(
        r"<input\b(?=[^>]*\bchecked(?:\s|=|/?>))[^>]*>",
        " ☒ ",
        document,
        flags=re.IGNORECASE,
    )
    document = re.sub(r"<input\b[^>]*>", " ☐ ", document, flags=re.IGNORECASE)
    text = html.unescape(_TAG_RE.sub(" ", document)).upper()
    text = " ".join(text.split())

    annual_label = r"ANNUAL\s+REPORT\s+PURSUANT\s+TO\s+SECTION\s+13(?:\(A\))?\s+OR\s+15\(D\)"
    checked_marker = r"(?:☒|☑|✓|✔|\[\s*X\s*\]|\(\s*X\s*\))"
    return re.search(checked_marker + r"\s*" + annual_label, text) is not None


def is_annual_20f_filing(
    filing: Mapping[str, Any],
    primary_document: Optional[str] = None,
) -> bool:
    """Whether SEC submission metadata and its cover identify an annual 20-F.

    The classifier is intentionally conservative: a 20-F/20-F/A form name and
    report date alone are insufficient, because Form 20-F can also be used for
    registration, transition, and shell-company reports. When available, Inline
    XBRL DEI cover facts take precedence; otherwise a positively checked annual
    report cover option is required. A valid reporting-period end date must be
    available from SEC submissions metadata or Inline XBRL, and both dates must
    agree when both are present.

    ``primary_document`` is the decoded text/HTML of the filing's primary
    document. This helper does not fetch the document or alter filing selection.
    """
    form = str(filing.get("form") or filing.get("form_type") or "").strip().upper()
    if form not in ANNUAL_20F_FORMS:
        return False

    submission_report_date = _sec_date(
        filing.get("reportDate") or filing.get("report_date")
    )
    if not primary_document:
        return False

    facts = _extract_inline_dei_facts(primary_document)
    document_period_end = _sec_date(facts.get("period_end"))
    report_date = submission_report_date or document_period_end
    if report_date is None:
        return False

    if facts:
        document_type = facts.get("document_type", "").strip().upper()
        if document_type and document_type not in ANNUAL_20F_FORMS:
            return False

        annual_report = _as_sec_bool(facts.get("annual_report"))
        if annual_report is not None:
            if not annual_report:
                return False
            # These cover-page flags must not contradict annual-report status.
            if any(
                _as_sec_bool(facts.get(key)) is True
                for key in ("registration_statement", "transition_report", "shell_company_report")
            ):
                return False
            if (
                submission_report_date is not None
                and document_period_end is not None
                and document_period_end != submission_report_date
            ):
                return False
            fiscal_period_focus = facts.get("fiscal_period_focus")
            if fiscal_period_focus and fiscal_period_focus.strip().upper() != "FY":
                return False
            return True

    return _cover_marks_annual_report(primary_document)
