#!/usr/bin/env python3
"""Filings-agent entry point (P2 skeleton).

Runs ONE filing through the LangGraph agent pipeline:

    normalize_bundle → validate → persist

Input is a JSON fixture describing the already-extracted/transformed statements
plus filing/company metadata.  Without ``--persist`` the run is compute +
validate only (no statement writes) and shows the validation findings; with
``--persist`` the full graph runs and the ``persist`` node is the only writer.

Usage:
    uv run python main_filing_agent.py --fixture fixture.json
    uv run python main_filing_agent.py --fixture fixture.json --persist
    uv run python main_filing_agent.py --fixture fixture.json -v

Fixture shape (``statement_docs`` entries are the scrapers' transformed
statement dicts, i.e. what ``normalize_statement_to_bundle`` accepts)::

    {
      "cik": "0000320193",
      "ticker": "AAPL",
      "company_name": "Apple Inc.",
      "form_type": "10-K",
      "accession_number": "0000320193-24-000123",
      "company_doc": {"cik": "0000320193", "name": "Apple Inc."},
      "filing_doc":  {"form_type": "10-K", "accession_number": "..."},
      "statement_docs": [ {"cik": ..., "statement_type": "income", "data": [...]} ]
    }
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# Load the repo-root .env before anything reads the environment.  The
# normalization package's own loader looks next to its package directory, so we
# mirror sec_scraper_cli.py here.
load_dotenv(Path(__file__).resolve().parent / ".env")

from filings_agent.state import new_state, state_summary

logger = logging.getLogger("filings_agent.cli")


# ── fixture → state ─────────────────────────────────────────────────────────


def build_state_from_fixture(payload: dict) -> dict:
    """Convert a fixture payload into an initial ``FilingAgentState``."""
    cik = str(payload.get("cik") or "").strip()
    if not cik:
        raise ValueError("fixture must contain a non-empty 'cik'")

    filing_doc = dict(payload.get("filing_doc") or {})
    form_type = (
        payload.get("form_type")
        or filing_doc.get("form_type")
        or filing_doc.get("form")
        or "10-K"
    )
    accession_number = (
        payload.get("accession_number")
        or filing_doc.get("accession_number")
        or filing_doc.get("accessionNumber")
    )
    filing_doc.setdefault("form_type", form_type)
    if accession_number:
        filing_doc.setdefault("accession_number", accession_number)

    company_doc = dict(payload.get("company_doc") or {})
    company_doc.setdefault("cik", cik)
    company_doc.setdefault("name", payload.get("company_name") or cik)

    statement_docs = (
        payload.get("statement_docs")
        or payload.get("statements")
        or []
    )

    return new_state(
        cik=cik,
        ticker=payload.get("ticker") or cik,
        company_name=payload.get("company_name") or company_doc.get("name", ""),
        form_type=form_type,
        accession_number=accession_number,
        statement_docs=statement_docs,
        filing_doc=filing_doc,
        company_doc=company_doc,
    )


def load_fixture_state(path: str | Path) -> dict:
    """Read a JSON fixture file and return an initial state."""
    payload = json.loads(Path(path).read_text())
    if isinstance(payload, list):
        payload = {"statement_docs": payload}
    if not isinstance(payload, dict):
        raise ValueError("fixture must be a JSON object (or a list of statements)")
    return build_state_from_fixture(payload)


# ── service wiring ──────────────────────────────────────────────────────────


def _build_norm_service():
    """Build the normalization service from the environment (``.env``)."""
    from data_normalization_service.core.config import AppConfig
    from data_normalization_service.services.normalization_service import (
        FinancialNormalizationService,
    )

    return FinancialNormalizationService(AppConfig.from_env())


def _wire_progress_callbacks(detailed: bool = False):
    """Install the shared terminal presenter (quiet unless *detailed*)."""
    from filings_agent.presenter import install_presenter

    return install_presenter(detailed=True if detailed else None)


# ── main ────────────────────────────────────────────────────────────────────


def run_once(
    fixture: str,
    *,
    persist: bool,
    norm_service: Any = None,
    report_store: Any = None,
) -> dict:
    """Run one filing through the graph (or compute+validate) and return the state."""
    state = load_fixture_state(fixture)
    if norm_service is None:
        norm_service = _build_norm_service()

    if persist:
        from filings_agent.graph import build_filing_graph
        from filings_agent.reports import build_report_store

        if report_store is None:
            report_store = build_report_store()
        graph = build_filing_graph(norm_service, report_store=report_store)
        return graph.invoke(state)

    # Compute + validate only: no statement writes (the report store is not used).
    from filings_agent.nodes.normalize import make_normalize_node
    from filings_agent.nodes.validate import make_validate_node

    state = make_normalize_node(norm_service)(state)
    if state.get("status") in ("failed", "skipped"):
        return state
    return make_validate_node(None)(state)


def load_batch_states(path: str | Path) -> list[dict]:
    """Read a batch file into filing states.

    Accepts either a JSON list of filing payloads, or an object with a
    ``filings`` list (each entry has the same shape as the single fixture).
    The list is processed in the order given — the pipeline's natural order is
    newest → oldest.
    """
    payload = json.loads(Path(path).read_text())
    if isinstance(payload, dict):
        payload = payload.get("filings") or payload.get("statement_batches") or []
    if not isinstance(payload, list):
        raise ValueError('batch file must be a JSON list (or {"filings": [...]})')
    return [build_state_from_fixture(entry) for entry in payload]


def _accumulate_quarterly(states: list[dict]) -> dict[str, list]:
    """Per-CIK income/cashflow statement docs (mirrors the scraper accumulator)."""
    accumulated: dict[str, list] = {}
    for state in states:
        cik = str(state.get("cik") or "")
        for doc in state.get("statement_docs") or []:
            if str(doc.get("statement_type", "")).lower() in (
                "income", "income_statements", "cashflow", "cash_flow"
            ):
                accumulated.setdefault(cik, []).append(doc)
    return accumulated


def run_batch_mode(
    batch_path: str,
    *,
    norm_service: Any = None,
    audit_path: Optional[str] = None,
    session_path: Optional[str] = None,
    resume: bool = True,
    company_stage: bool = False,
) -> dict:
    """Run a batch of filings through the graph with audit + resume."""
    from filings_agent.audit import build_audit_log, install_audit
    from filings_agent.batch import run_batch, run_company_stage
    from filings_agent.graph import build_filing_graph
    from filings_agent.reports import build_report_store
    from filings_agent.session import build_session

    states = load_batch_states(batch_path)
    if norm_service is None:
        norm_service = _build_norm_service()

    audit = install_audit(audit_path)
    if audit is None:
        # Audit disabled via env, but an explicit path still records batch-level
        # events (node/tool events come from the hook layer).
        audit = build_audit_log(audit_path)
    session = build_session(session_path)
    graph = build_filing_graph(norm_service, report_store=build_report_store())

    out = run_batch(graph, states, session=session, audit=audit, skip_saved=resume)

    if company_stage:
        from filings_agent.company_graph import build_company_graph

        accumulated = _accumulate_quarterly(states)
        company_graph = build_company_graph(
            quarterly_service=_build_quarterly_service(norm_service),
            reconciliation_provider=None,
            report_store=build_report_store(),
        )
        summaries = []
        for cik, statements in accumulated.items():
            summaries.append(
                run_company_stage(
                    company_graph,
                    cik=cik,
                    ticker=cik,
                    statements=statements,
                    session=session,
                    audit=audit,
                )
            )
        out["companies"] = summaries

    out["session"] = session.summary()
    return out


def _build_quarterly_service(norm_service: Any):
    """Reuse the normalization service's configured quarterly calculation service."""
    service = getattr(norm_service, "quarterly_service", None)
    if service is not None:
        return service
    from data_normalization_service.services.quarterly_service import (
        PeriodBasedFinancialCalculationService,
    )

    return PeriodBasedFinancialCalculationService(norm_service.config, norm_service.db_tracker)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run filings through the filings-agent graph: one filing (--fixture) "
            "or a headless batch (--batch) with audit, resume and company stage."
        )
    )
    parser.add_argument("--fixture", help="Path to a single-filing JSON fixture.")
    parser.add_argument(
        "--batch",
        help="Path to a JSON list of filings to run through the graph (headless batch).",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Run the full graph and write to MongoDB (--fixture default: compute + validate only).",
    )
    parser.add_argument("--audit-log", help="JSONL audit trail path (default: AGENT_AUDIT_LOG or .filings_agent/audit.jsonl).")
    parser.add_argument("--session-file", help="Durable session path (default: AGENT_SESSION_FILE or .filings_agent/session.jsonl).")
    parser.add_argument("--no-resume", action="store_true", help="Ignore the session ledger; re-run everything.")
    parser.add_argument("--company-stage", action="store_true", help="Run the company stage after the batch.")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging.")
    args = parser.parse_args(argv)

    # Narrative-first: the presenter narrates every step, so the logger only
    # reports warnings+.  -v restores the (very chatty) INFO/DEBUG logs.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    _wire_progress_callbacks(detailed=args.verbose)

    if args.batch:
        print("filings-agent: BATCH (graph + audit + resume)")
        try:
            out = run_batch_mode(
                args.batch,
                audit_path=args.audit_log,
                session_path=args.session_file,
                resume=not args.no_resume,
                company_stage=args.company_stage,
            )
        except FileNotFoundError as exc:
            print(f"error: batch file not found: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001
            logger.error("batch run failed: %s", exc, exc_info=args.verbose)
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print("\n── batch summary ──")
        print(json.dumps(out["summary"], indent=2, default=str))
        print("\n── session ──")
        print(json.dumps({k: v for k, v in out["session"].items() if k != "companies"}, indent=2, default=str))
        for result in out["results"]:
            err = f" — {result.get('error')}" if result.get("error") else ""
            print(f"  {result.get('accession_number')}: {result.get('status')}{err}")
        failed = (out["summary"].get("counts") or {}).get("failed", 0)
        return 1 if failed else 0

    if not args.fixture:
        parser.error("one of --fixture or --batch is required")

    mode = "PERSIST (writes to MongoDB)" if args.persist else "compute + validate (no statement writes)"
    print(f"filings-agent: {mode}")

    try:
        final = run_once(args.fixture, persist=args.persist)
    except FileNotFoundError as exc:
        print(f"error: fixture not found: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.error("filing run failed: %s", exc, exc_info=args.verbose)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("\n── result ──")
    print(json.dumps(state_summary(final), indent=2, default=str))

    findings = final.get("findings") or []
    if findings:
        print("\n── findings ──")
        for f in findings:
            print(f"  [{f.get('severity'):>6}] {f.get('type')}: {f.get('message')}")

    ok = final.get("status") in ("normalized", "validated", "saved")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
