"""Modular CLI entrypoint for filings extraction and agent execution.

Replaces the monolithic procedural execution with clean, decoupled commands.
"""
from __future__ import annotations

import argparse
import sys
from typing import Sequence


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Filings Extractor — Agentic SEC XBRL extraction & processing",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Command: filing
    filing_parser = subparsers.add_parser("filing", help="Process a single filing via LangGraph agent")
    filing_parser.add_argument("--cik", required=True, help="Company CIK (e.g. 0000320193)")
    filing_parser.add_argument("--accession", required=True, help="Filing Accession Number")
    filing_parser.add_argument("--form-type", default="10-K", help="Form type (10-K, 10-Q)")
    filing_parser.add_argument("--reload", action="store_true", help="Force reload existing records")

    # Command: company
    company_parser = subparsers.add_parser("company", help="Process filings for a company")
    company_parser.add_argument("--ticker", help="Company ticker (e.g. AAPL)")
    company_parser.add_argument("--cik", help="Company CIK")
    company_parser.add_argument("--year", type=int, default=2020, help="Start year")
    company_parser.add_argument("--end-year", type=int, help="End year")
    company_parser.add_argument("--reload", action="store_true", help="Reload existing filings")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "filing":
        print(f"Executing filing agent for CIK {args.cik} accession {args.accession}...")
        return 0

    if args.command == "company":
        print(f"Running pipeline for company CIK {args.cik or args.ticker}...")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
