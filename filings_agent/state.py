"""Graph state for the filings agent.

Modelled on the earning_agent ``EarningsAgentState``: a single ``TypedDict``
that flows through every graph node.  Nodes are pure ``(state) -> state``
functions; ``with_hooks`` adds logging/timing and converts exceptions into a
``status="failed"`` short-circuit.

Only the P2 fields are used today (``statement_docs`` / ``filing_doc`` /
``company_doc`` inputs, ``bundles``, ``status``, ``persist_receipt``).  The
remaining fields are declared up front so the later phases (validate, repair,
hierarchy, guidance) can populate them without reshaping the state.
"""
from __future__ import annotations

from typing import Any, Optional

from typing_extensions import NotRequired, TypedDict


class FilingAgentState(TypedDict):
    """State passed between filings-agent graph nodes."""

    # ── identity ───────────────────────────────────────────────────────────
    cik: str
    ticker: str
    company_name: str
    form_type: str
    # pending → normalized → validated → repaired → saved | failed | skipped
    status: str
    accession_number: NotRequired[Optional[str]]
    filing_date: NotRequired[Optional[str]]
    sec_client: NotRequired[Optional[Any]]
    error: NotRequired[Optional[str]]

    # ── programmatic inputs (extraction/transform already done) ────────────
    # Statement dicts produced by the scraper's transform step.  Each is the
    # ``statement_doc`` accepted by ``normalize_statement_to_bundle``.
    statement_docs: NotRequired[Optional[list]]
    filing_doc: NotRequired[Optional[dict]]
    company_doc: NotRequired[Optional[dict]]
    reporting_period: NotRequired[Optional[dict]]

    # ── P1 output: in-memory bundles (no DB writes yet) ────────────────────
    bundles: NotRequired[Optional[list]]

    # ── sign conventions (post-extraction) ─────────────────────────────────
    # In-memory sign corrections applied by the sign subagent before validation:
    # [{statement_type, concept, old_value, new_value, required_sign, source}].
    sign_fixes: NotRequired[Optional[list]]
    # Covered rows the agent left unfixed (should be empty; reported, never blocking).
    sign_fix_unresolved: NotRequired[Optional[list]]

    # ── validation / repair (P3/P4) ────────────────────────────────────────
    # [{type, severity, message, evidence}] — same shape as the earning_agent.
    findings: NotRequired[Optional[list]]
    validation_report: NotRequired[Optional[dict]]
    repair_decisions: NotRequired[Optional[list]]
    repair_actions: NotRequired[Optional[dict]]

    # ── P8: the agent's explicit final write decision ──────────────────────
    write_plan: NotRequired[Optional[dict]]
    decision: NotRequired[Optional[dict]]
    # Optional callable invoked by the persist node immediately before the first
    # write — used by the scraper to defer the --reload delete until the agent
    # has actually decided to write (so a skip can never delete existing data).
    pre_write_hook: NotRequired[Optional[Any]]
    # True for a --reload: an existing row for the same period is overwritten
    # rather than skipped, so the reload actually replaces the period's values.
    replace_existing: NotRequired[Optional[bool]]

    # ── hierarchy (P5) ─────────────────────────────────────────────────────
    existing_concepts: NotRequired[Optional[list]]
    hierarchy_plan: NotRequired[Optional[dict]]
    hierarchy_seeded: NotRequired[Optional[bool]]

    # ── concept resolution (P5b) ───────────────────────────────────────────
    # Summary of the agent's concept-equivalence decisions for this filing.
    concept_resolution: NotRequired[Optional[dict]]
    # Newest-period promotions the resolve node decided: the incoming filing's
    # tag is newer than the stored concept it replaces, so the persister moves
    # the stored concept's values onto the new tag and deletes the old row.
    # [{cik, statement_type, form_type, from_concept, to_concept, reason, ...}]
    concept_promotions: NotRequired[Optional[list]]

    # ── guidance (P6) ──────────────────────────────────────────────────────
    mda_text: NotRequired[Optional[str]]
    html_text: NotRequired[Optional[str]]
    guidance_records: NotRequired[Optional[list]]
    guidance_extract: NotRequired[Optional[dict]]
    guidance_save: NotRequired[Optional[dict]]

    # ── persistence ────────────────────────────────────────────────────────
    persist_receipt: NotRequired[Optional[dict]]
    # Informational: set when an existing period will be replaced atomically.
    _pending_replace: NotRequired[Optional[dict]]


def new_state(
    *,
    cik: str,
    ticker: str = "",
    company_name: str = "",
    form_type: str = "",
    accession_number: Optional[str] = None,
    statement_docs: Optional[list] = None,
    filing_doc: Optional[dict] = None,
    company_doc: Optional[dict] = None,
    filing_date: Optional[str] = None,
    sec_client: Optional[Any] = None,
) -> FilingAgentState:
    """Build an initial graph state for one filing."""
    return FilingAgentState(
        cik=cik,
        ticker=ticker or cik,
        company_name=company_name,
        form_type=form_type,
        status="pending",
        accession_number=accession_number,
        statement_docs=statement_docs or [],
        filing_doc=filing_doc or {},
        company_doc=company_doc or {},
        filing_date=filing_date,
        sec_client=sec_client,
    )


class CompanyAgentState(TypedDict):
    """State for the company-level (post-filing) agent stage.

    Runs once per company after its filings are processed, mirroring the
    scraper's former inline post-processing: quarterly deaccumulation, SEC
    companyfacts gap reconciliation, and finalization of validation reports.
    Every node is best-effort — a failure is recorded, never raised.
    """

    cik: str
    ticker: str
    # queued → deaccumulated → reconciled → finalized
    status: str
    run_id: NotRequired[Optional[str]]
    # Slim statement entries accumulated during the filing loop.
    quarterly_statements: NotRequired[Optional[list]]
    enable_reconciliation: NotRequired[Optional[bool]]
    deaccumulation: NotRequired[Optional[dict]]
    reconciliation: NotRequired[Optional[dict]]
    report_finalization: NotRequired[Optional[dict]]
    error: NotRequired[Optional[str]]


def new_company_state(
    *,
    cik: str,
    ticker: str = "",
    run_id: Optional[str] = None,
    quarterly_statements: Optional[list] = None,
    enable_reconciliation: Optional[bool] = None,
) -> CompanyAgentState:
    """Build an initial company-stage state.

    ``enable_reconciliation`` defaults to ``None`` so the graph-level gate is
    authoritative unless a caller explicitly overrides it per company.
    """
    return CompanyAgentState(
        cik=cik,
        ticker=ticker or cik,
        status="queued",
        run_id=run_id,
        quarterly_statements=quarterly_statements or [],
        enable_reconciliation=enable_reconciliation,
    )


def company_state_summary(state: CompanyAgentState) -> dict[str, Any]:
    """Small log/audit-friendly view of a company-stage state."""
    return {
        "cik": state.get("cik"),
        "ticker": state.get("ticker"),
        "status": state.get("status"),
        "quarterly_statements": len(state.get("quarterly_statements") or []),
        "deaccumulation": (state.get("deaccumulation") or {}).get("status"),
        "reconciliation": (state.get("reconciliation") or {}).get("status"),
        "reconciliation_filled": (state.get("reconciliation") or {}).get("filled"),
        "report_finalization": (state.get("report_finalization") or {}).get("status"),
    }


def state_summary(state: FilingAgentState) -> dict[str, Any]:
    """Small, log-friendly view of a filing state (no bundles/raw payloads)."""
    validation_report = state.get("validation_report") or {}
    return {
        "cik": state.get("cik"),
        "ticker": state.get("ticker"),
        "form_type": state.get("form_type"),
        "accession_number": state.get("accession_number"),
        "status": state.get("status"),
        "error": state.get("error"),
        "statement_docs": len(state.get("statement_docs") or []),
        "bundles": len(state.get("bundles") or []),
        "findings": len(state.get("findings") or []),
        "validation_status": validation_report.get("status"),
        "blocking_findings": validation_report.get("blocking_count"),
        "repaired": (state.get("repair_actions") or {}).get("applied"),
        "hierarchy_seeded": (state.get("hierarchy_plan") or {}).get("seeded_statement_types"),
        "guidance_records": len(state.get("guidance_records") or []),
        "guidance_extract": (state.get("guidance_extract") or {}).get("status"),
        "guidance_save": (state.get("guidance_save") or {}).get("status"),
        "decision": (state.get("decision") or {}).get("action"),
        "decided_by": (state.get("decision") or {}).get("decided_by"),
        "persist_receipt": state.get("persist_receipt"),
    }
