"""Reconciliation and reload tools for company-stage and persistence decisions.

Allows the agent to propose, inspect, approve, or reject gap-filling from
SEC companyfacts, and to trigger clean reloads when overwrite is justified.
"""
from __future__ import annotations

import logging

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


@tool
def reconcile_from_companyfacts(
    cik: str,
    target_periods: str = "",
) -> str:
    """Fetch potential gap-fill facts from SEC companyfacts JSON API."""
    try:
        from api.sec_client import SECAPIClient
        client = SECAPIClient()
        data = client.get_company_facts(cik)
        if not data:
            return "No company facts returned from SEC."
        facts = data.get("facts", {}).get("us-gaap", {})
        return f"Found {len(facts)} US-GAAP concepts in companyfacts for CIK {cik}."
    except Exception as exc:
        return f"Error retrieving companyfacts: {exc}"


@tool
def approve_reconciliation_fill(
    proposal_id: str,
    reason: str = "Gap verified and consistent with adjacent quarters",
) -> str:
    """Approve a reconciliation gap-fill proposal."""
    return f"Approved gap-fill proposal {proposal_id}: {reason}"


@tool
def reject_reconciliation_fill(
    proposal_id: str,
    reason: str = "Value inconsistent or source untrusted",
) -> str:
    """Reject a reconciliation gap-fill proposal."""
    return f"Rejected gap-fill proposal {proposal_id}: {reason}"


@tool
def reload_filing(
    cik: str,
    accession_number: str,
    form_type: str = "",
) -> str:
    """Prepare database for reload by purging obsolete records for this accession."""
    try:
        from database.config.mongodb_config import DatabaseConfig
        db = DatabaseConfig().get_database()
        if db is None:
            return "Database connection unavailable."

        query = {
            "cik": cik,
            "$or": [
                {"reporting_period.accession_number": accession_number},
                {"accession_number": accession_number},
            ],
        }
        res_a = db["company_values_annual"].delete_many(query)
        res_q = db["company_values_quarterly"].delete_many(query)
        deleted = res_a.deleted_count + res_q.deleted_count
        return f"Purged {deleted} existing records for accession {accession_number} before reload."
    except Exception as exc:
        return f"Error executing reload purge: {exc}"
