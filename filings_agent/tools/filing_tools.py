"""Filing discovery, existence checking, and document retrieval tools.

These tools allow the filing agent to check if a filing exists, fetch filing
indexes, and download raw documents without hardcoded scraper loops.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def check_filing_exists_in_db(
    cik: str,
    accession_number: Optional[str] = None,
    db_collections: Optional[tuple[Any, ...]] = None,
) -> bool:
    """Check if a filing is already stored in the database."""
    if not accession_number:
        return False

    if db_collections is None:
        try:
            from database.config.mongodb_config import DatabaseConfig
            db = DatabaseConfig().get_database()
            if db is not None:
                db_collections = (
                    db["company_values_annual"],
                    db["company_values_quarterly"],
                )
        except Exception as exc:
            logger.debug(f"Could not connect to DB for filing check: {exc}")
            return False

    if not db_collections:
        return False

    accession_filter = {
        "cik": cik,
        "$or": [
            {"reporting_period.accession_number": accession_number},
            {"accession_number": accession_number},
        ],
    }

    for col in db_collections:
        try:
            if col.find_one(accession_filter, projection={"_id": 1}):
                return True
        except Exception as exc:
            logger.debug(f"Error checking filing existence: {exc}")

    return False


@tool
def check_filing_exists(cik: str, accession_number: str) -> str:
    """Check if a filing by accession number already exists in the database."""
    exists = check_filing_exists_in_db(cik, accession_number)
    return str({"exists": exists, "cik": cik, "accession_number": accession_number})


@tool
def fetch_company_filings(
    cik: str,
    start_year: int = 2010,
    end_year: Optional[int] = None,
) -> str:
    """Fetch recent submissions and filing metadata for a company CIK."""
    try:
        from api.sec_client import SECAPIClient
        client = SECAPIClient()
        submissions, _ = client.get_company_submissions(cik, start_year=start_year, end_year=end_year)
        filings = (submissions or {}).get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        return f"Found {len(forms)} filings for CIK {cik}."
    except Exception as exc:
        return f"Error fetching filings: {exc}"


@tool
def download_html_filing(
    cik: str,
    accession_number: str,
    filing_date: str = "2020-01-01",
    target_dir: str = "sec_html_filings",
) -> str:
    """Download HTML filing for a given CIK and accession number."""
    try:
        from api.sec_client import SECAPIClient
        client = SECAPIClient()
        path = client.download_html_filing(cik, accession_number, filing_date, target_dir)
        return f"HTML filing saved to {path}"
    except Exception as exc:
        return f"Error downloading HTML filing: {exc}"
