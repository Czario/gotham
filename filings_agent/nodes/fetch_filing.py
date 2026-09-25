"""Filing discovery and dedup check node.

Discovers filings from SEC submissions or checks if the targeted filing
already exists in the database to prevent redundant extraction runs.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from filings_agent.tools.filing_tools import check_filing_exists_in_db

logger = logging.getLogger(__name__)


def make_fetch_filing_node(
    sec_client: Optional[Any] = None,
    db_collections: Optional[tuple[Any, ...]] = None,
) -> Callable[[dict], dict]:
    """Build the fetch_filing node."""

    def fetch_filing_node(state: dict) -> dict:
        cik = str(state.get("cik") or "")
        accession_number = state.get("accession_number")
        replace_existing = state.get("replace_existing", False)

        # Check if already in database (unless replace_existing/reload is set)
        if accession_number and not replace_existing:
            already_stored = check_filing_exists_in_db(
                cik=cik,
                accession_number=accession_number,
                db_collections=db_collections,
            )
            if already_stored:
                logger.info("fetch_filing: CIK %s accession %s already stored; skipping", cik, accession_number)
                return {
                    **state,
                    "status": "skipped",
                    "error": f"Filing {accession_number} already exists in database.",
                }

        # If filings_list is already present or single filing targeted, we proceed
        return {
            **state,
            "status": "discovered",
        }

    return fetch_filing_node
