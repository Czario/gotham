"""P6 guidance nodes — extract forward-looking MD&A guidance, then persist it.

Two nodes, both deliberately non-fatal (guidance must never fail a filing run):

* ``extract_guidance`` — read the MD&A text with navigation tools, run the
  guidance agent loop, and normalize the reported records to the
  ``guidance_values`` schema.  No database access.
* ``save_guidance`` — upsert into ``guidance_values`` and score current
  guidance against stored actuals (reusing the 8-K agent's stack unchanged).

Both preserve the incoming ``status`` so the graph's post-save routing and the
persist receipt are unaffected.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional

from ..period import DetectedPeriod, build_detected_period, format_period_label

logger = logging.getLogger(__name__)


def _normalize_unit(unit: Any) -> str:
    text = str(unit or "").upper().strip()
    if ":" in text:
        text = text.split(":")[-1]
    return text


def filing_currency(bundles: Iterable[Any]) -> str:
    """Best-effort reporting currency from the bundles' XBRL units."""
    units: set[str] = set()
    for bundle in bundles or []:
        for item in getattr(bundle, "concepts", None) or []:
            if not isinstance(item, dict):
                continue
            unit = _normalize_unit(item.get("unit"))
            if unit:
                units.add(unit)
    if not units:
        return "USD"
    if units == {"USD"}:
        return "USD"
    # A single non-USD unit is a genuine signal; mixed units stay USD-gated by
    # the normalizer (which refuses non-USD monetary values).
    if len(units) == 1:
        return next(iter(units))
    return "USD"


def _filing_period(state: dict) -> Optional[DetectedPeriod]:
    reporting_period = state.get("reporting_period")
    if not reporting_period:
        bundles = state.get("bundles") or []
        if bundles:
            reporting_period = getattr(bundles[0], "reporting_period", None)
    return build_detected_period(
        reporting_period, form_type=str(state.get("form_type") or "")
    )


def make_guidance_extract_node(
    *,
    chat_llm: Any = None,
    mda_provider: Optional[Callable[[dict], Optional[str]]] = None,
    on_event: Any = None,
) -> Callable[[dict], dict]:
    """Build the ``extract_guidance`` node."""

    def extract_guidance_node(state: dict) -> dict:
        from .. import config
        from ..agent.guidance import normalize_guidance_records
        from ..agent.loop import run_agent_loop
        from ..agent.mda_tools import build_mda_tools
        from ..agent.prompts import (
            GUIDANCE_FINALIZE_DESCRIPTION,
            GUIDANCE_SYSTEM_PROMPT,
        )
        from ..mda import default_mda_provider

        if not config.GUIDANCE_ENABLED:
            return {**state, "guidance_records": [], "guidance_extract": {"status": "disabled"}}

        period = _filing_period(state)
        if period is None:
            logger.info("guidance: no usable reporting period for %s", state.get("cik"))
            return {**state, "guidance_records": [], "guidance_extract": {"status": "no_period"}}

        text = state.get("mda_text")
        if not text:
            provider = mda_provider or default_mda_provider
            try:
                text = provider(state)
            except Exception as exc:  # noqa: BLE001
                logger.warning("guidance: MD&A provider failed: %s", exc)
                text = None
        if not text:
            return {
                **state,
                "guidance_records": [],
                "guidance_extract": {"status": "no_mda_text"},
            }

        currency = filing_currency(state.get("bundles") or [])
        try:
            from ..hooks import report_call

            report_call(
                f"  [guidance]  MD&A {len(text):,} chars — running guidance agent "
                f"({period.period_type})"
            )
        except Exception:  # noqa: BLE001
            pass
        raw_records: Any = None
        error: Optional[str] = None

        if config.GUIDANCE_LLM_ENABLED:
            try:
                tools = build_mda_tools(text)
                system_prompt = (
                    GUIDANCE_SYSTEM_PROMPT
                    + f"\n\nFILING PERIOD (the guidance covers periods AFTER this one):\n"
                    + f"  form: {state.get('form_type') or '?'}\n"
                    + f"  period_type: {period.period_type}\n"
                    + f"  period_end: {period.period_end.isoformat()}\n"
                    + f"  fiscal_year: {period.fiscal_year}\n"
                    + f"  quarter: {period.quarter}\n"
                    + f"  label: {format_period_label(period)}\n"
                )
                initial_message = (
                    f"MD&A for {state.get('company_name') or state.get('cik')} "
                    f"({state.get('ticker') or '?'}), {len(text):,} characters.\n"
                    "Locate the forward-looking guidance/outlook content, read it, "
                    "then call finalize_guidance with EVERY forward-looking item — "
                    "quantitative figures (revenue, EPS, capex, etc.) AND qualitative "
                    "directional statements (form='qualitative', no value needed, "
                    "just as_printed). Do not skip qualitative-only guidance."
                )
                result = run_agent_loop(
                    system_prompt,
                    initial_message,
                    tools,
                    ticker=str(state.get("ticker") or state.get("cik") or "?"),
                    finalize_name="finalize_guidance",
                    finalize_description=GUIDANCE_FINALIZE_DESCRIPTION,
                    chat_llm=chat_llm,
                    on_event=on_event,
                )
                if isinstance(result, dict):
                    raw_records = result.get("guidance")
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                logger.warning("guidance: agent pass failed for %s: %s", state.get("cik"), exc)

        records: list[dict] = []
        issues: list[str] = []
        if raw_records:
            try:
                records, issues = normalize_guidance_records(
                    raw_records, period, currency
                )
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                logger.warning("guidance: normalization failed for %s: %s", state.get("cik"), exc)

        logger.info(
            "guidance: %d record(s) extracted for CIK %s (%d dropped)",
            len(records), state.get("cik"), len(issues),
        )
        return {
            **state,
            "mda_text": text,
            "guidance_records": records,
            "guidance_extract": {
                "status": "extracted" if records else ("error" if error else "empty"),
                "reported": len(raw_records) if isinstance(raw_records, list) else 0,
                "normalized": len(records),
                "dropped": len(issues),
                "drop_reasons": issues[:10],
                "currency": currency,
                "error": error,
            },
        }

    return extract_guidance_node


def make_guidance_save_node() -> Callable[[dict], dict]:
    """Build the ``save_guidance`` node (upsert + score; never fails the run)."""

    def save_guidance_node(state: dict) -> dict:
        from .. import config
        from ..integrations.guidance import (
            score_guidance_for_cik,
            upsert_guidance_records,
        )

        if not config.GUIDANCE_ENABLED:
            return {**state, "guidance_save": {"status": "disabled"}}

        records = state.get("guidance_records") or []
        cik = state.get("cik")
        if not records or not cik:
            return {**state, "guidance_save": {"status": "no_records", "records": len(records)}}

        period = _filing_period(state)
        if period is None:
            return {**state, "guidance_save": {"status": "no_period"}}

        summary: dict[str, Any] = {"status": "skipped"}
        try:
            upsert_summary = upsert_guidance_records(
                str(cik),
                records,
                accession_number=state.get("accession_number"),
                form_type=str(state.get("form_type") or "10-K"),
                filing_period=period,
            )
            summary = {"status": "saved", **upsert_summary}
        except Exception as exc:  # noqa: BLE001 — guidance never fails a run
            logger.warning("guidance: upsert failed for %s: %s", cik, exc)
            return {**state, "guidance_save": {"status": "failed", "error": str(exc)}}

        try:
            summary["score"] = score_guidance_for_cik(str(cik), period)
        except Exception as exc:  # noqa: BLE001
            logger.warning("guidance: scoring failed for %s: %s", cik, exc)
            summary["score"] = {"status": "failed", "error": str(exc)}

        logger.info(
            "guidance: saved %s record(s) for CIK %s (demoted=%s, scored=%s)",
            summary.get("upserted"),
            cik,
            summary.get("demoted"),
            (summary.get("score") or {}).get("scored"),
        )
        return {**state, "guidance_save": summary}

    return save_guidance_node
