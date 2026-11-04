"""Regression tests for quarterly cash flow deaccumulation (full-year, single-quarter, and cashflow-only)."""

from datetime import datetime
from unittest.mock import MagicMock, Mock

import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from data_normalization_service.core.config import AppConfig
from data_normalization_service.services.quarterly_service import (
    PeriodBasedFinancialCalculationService,
)


class InMemoryCollection:
    """Minimal in-memory MongoDB collection supporting unique index on (cik, concept_id, fy, q)."""

    def __init__(self, docs=None, enforce_unique=False):
        self.docs = list(docs or [])
        self.enforce_unique = enforce_unique

    def _matches(self, doc, query):
        for k, v in query.items():
            if k == "$or":
                if not any(self._matches(doc, sub) for sub in v):
                    return False
                continue
            # Support dotted path like 'reporting_period.fiscal_year'
            cur = doc
            for part in k.split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            if isinstance(v, dict):
                if "$in" in v and cur not in v["$in"]:
                    return False
                if "$ne" in v and cur == v["$ne"]:
                    return False
            else:
                if cur != v:
                    return False
        return True

    def find_one(self, query, projection=None):
        for d in self.docs:
            if self._matches(d, query):
                return d
        return None

    def find(self, query=None, projection=None):
        query = query or {}
        matched = [d for d in self.docs if self._matches(d, query)]

        class _Cursor(list):
            def limit(self, n):
                return _Cursor(self[:n])

        return _Cursor(matched)

    def distinct(self, field, query=None):
        matched = self.find(query)
        vals = set()
        for d in matched:
            cur = d
            for part in field.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is not None:
                vals.add(cur)
        return list(vals)

    def insert_one(self, doc):
        if self.enforce_unique:
            rp = doc.get("reporting_period") or {}
            for existing in self.docs:
                erp = existing.get("reporting_period") or {}
                if (
                    existing.get("cik") == doc.get("cik")
                    and existing.get("concept_id") == doc.get("concept_id")
                    and erp.get("fiscal_year") == rp.get("fiscal_year")
                    and erp.get("quarter") == rp.get("quarter")
                ):
                    raise DuplicateKeyError("E11000 duplicate key error idx_unique_value")
        new_doc = dict(doc)
        new_doc.setdefault("_id", ObjectId())
        self.docs.append(new_doc)
        res = Mock()
        res.inserted_id = new_doc["_id"]
        return res

    def update_one(self, filter_q, update_doc):
        target = self.find_one(filter_q)
        if target and "$set" in update_doc:
            for k, v in update_doc["$set"].items():
                if "." in k:
                    head, tail = k.split(".", 1)
                    target.setdefault(head, {})[tail] = v
                else:
                    target[k] = v


@pytest.fixture
def qcom_setup(mocker):
    mocker.patch("data_normalization_service.services.quarterly_service.DatabaseConnection")
    config = Mock(spec=AppConfig)
    config.database = Mock()
    service = PeriodBasedFinancialCalculationService(config, None)

    op_cf_id = ObjectId()
    capex_id = ObjectId()
    concepts_col = InMemoryCollection([
        {
            "_id": op_cf_id,
            "cik": "0000804328",
            "statement_type": "cashflow",
            "concept": "us-gaap:NetCashProvidedByUsedInOperatingActivities",
            "form_type": "10-Q",
        },
        {
            "_id": capex_id,
            "cik": "0000804328",
            "statement_type": "cashflow",
            "concept": "us-gaap:PaymentsToAcquireProductiveAssets",
            "form_type": "10-Q",
        },
    ])
    values_col = InMemoryCollection([], enforce_unique=True)

    service.quarterly_concept_repo = Mock(collection=concepts_col)
    service.annual_concept_repo = Mock(collection=InMemoryCollection([]))
    service.quarterly_value_repo = Mock(collection=values_col)

    return service, values_col, op_cf_id, capex_id


def _make_cf_stmt(cik, fy, quarter, period_date, op_cf_val, capex_val, statement_type="cashflow"):
    return {
        "_id": ObjectId(),
        "cik": cik,
        "statement_type": statement_type,
        "reporting_period": {
            "fiscal_year": fy,
            "quarter": quarter,
            "period_date": period_date,
            "end_date": datetime.strptime(period_date, "%Y-%m-%d"),
        },
        "filing_id": ObjectId(),
        "data": [
            {
                "concept": "us-gaap:NetCashProvidedByUsedInOperatingActivities",
                "value": op_cf_val,
                "period": f"{period_date} 00:00:00",
            },
            {
                "concept": "us-gaap:PaymentsToAcquireProductiveAssets",
                "value": capex_val,
                "period": f"{period_date} 00:00:00",
            },
        ],
        "_filing_doc": {
            "_id": ObjectId(),
            "form_type": "10-Q",
            "accession_number": f"0000804328-26-00000{quarter}",
        },
    }


def _seed_db_row(values_col, cik, concept_id, fy, quarter, period_date, value, calculated=False):
    values_col.docs.append({
        "_id": ObjectId(),
        "cik": cik,
        "concept_id": concept_id,
        "statement_type": "cashflow",
        "form_type": "10-Q",
        "dimension_value": False,
        "reporting_period": {
            "fiscal_year": fy,
            "quarter": quarter,
            "period_date": period_date,
            "end_date": datetime.strptime(period_date, "%Y-%m-%d"),
        },
        "value": float(value),
        "calculated": calculated,
        "accession_number": f"0000804328-26-00000{quarter}",
    })


def test_full_year_deaccumulation_updates_existing_ytd_rows_in_place(qcom_setup):
    """When persist_statement_bundle has already inserted raw YTD rows (calculated=False),
    process_company_from_statements must update Q1, Q2, Q3 in place with 3M values and calculated=True."""
    service, values_col, op_cf_id, capex_id = qcom_setup
    cik = "0000804328"

    # Pre-seed raw YTD values as persist_statement_bundle does before company stage runs
    _seed_db_row(values_col, cik, op_cf_id, 2026, 1, "2025-12-28", 4965000000, calculated=False)
    _seed_db_row(values_col, cik, op_cf_id, 2026, 2, "2026-03-29", 7414000000, calculated=False)
    _seed_db_row(values_col, cik, op_cf_id, 2026, 3, "2026-06-28", 8405000000, calculated=False)
    _seed_db_row(values_col, cik, capex_id, 2026, 1, "2025-12-28", -549000000, calculated=False)
    _seed_db_row(values_col, cik, capex_id, 2026, 2, "2026-03-29", -1082000000, calculated=False)
    _seed_db_row(values_col, cik, capex_id, 2026, 3, "2026-06-28", -1578000000, calculated=False)

    stmts = [
        _make_cf_stmt(cik, 2026, 1, "2025-12-28", 4965000000, -549000000),
        _make_cf_stmt(cik, 2026, 2, "2026-03-29", 7414000000, -1082000000),
        _make_cf_stmt(cik, 2026, 3, "2026-06-28", 8405000000, -1578000000),
    ]
    service.process_company_from_statements(cik, stmts)

    q1_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 1})
    q2_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 2})
    q3_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 3})

    assert q1_op["value"] == 4965000000.0 and q1_op["calculated"] is True
    assert q2_op["value"] == 2449000000.0 and q2_op["calculated"] is True
    assert q3_op["value"] == 991000000.0 and q3_op["calculated"] is True

    q1_cap = values_col.find_one({"concept_id": capex_id, "reporting_period.quarter": 1})
    q2_cap = values_col.find_one({"concept_id": capex_id, "reporting_period.quarter": 2})
    q3_cap = values_col.find_one({"concept_id": capex_id, "reporting_period.quarter": 3})

    assert q1_cap["value"] == -549000000.0 and q1_cap["calculated"] is True
    assert q2_cap["value"] == -533000000.0 and q2_cap["calculated"] is True
    assert q3_cap["value"] == -496000000.0 and q3_cap["calculated"] is True


def test_single_quarter_q3_deaccumulates_using_already_calculated_db_quarters(qcom_setup):
    """Extracting Q3 cashflow alone when Q1 and Q2 in DB already have calculated=True (3M values)
    must subtract (Q1_3m + Q2_3m) from Q3_9m without double-subtracting Q1 from Q2."""
    service, values_col, op_cf_id, capex_id = qcom_setup
    cik = "0000804328"

    # Q1 and Q2 in DB are already deaccumulated (3M values, calculated=True)
    _seed_db_row(values_col, cik, op_cf_id, 2026, 1, "2025-12-28", 4965000000, calculated=True)
    _seed_db_row(values_col, cik, op_cf_id, 2026, 2, "2026-03-29", 2449000000, calculated=True)
    # Q3 was just persisted by persist_statement_bundle as raw 9M YTD (calculated=False)
    _seed_db_row(values_col, cik, op_cf_id, 2026, 3, "2026-06-28", 8405000000, calculated=False)

    _seed_db_row(values_col, cik, capex_id, 2026, 1, "2025-12-28", -549000000, calculated=True)
    _seed_db_row(values_col, cik, capex_id, 2026, 2, "2026-03-29", -533000000, calculated=True)
    _seed_db_row(values_col, cik, capex_id, 2026, 3, "2026-06-28", -1578000000, calculated=False)

    # Only Q3 is in the in-memory accumulator (single-quarter cashflow-only run)
    stmts = [_make_cf_stmt(cik, 2026, 3, "2026-06-28", 8405000000, -1578000000, statement_type="cash_flow")]
    service.process_company_from_statements(cik, stmts)

    q2_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 2})
    q3_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 3})
    assert q2_op["value"] == 2449000000.0 and q2_op["calculated"] is True
    assert q3_op["value"] == 991000000.0 and q3_op["calculated"] is True

    q3_cap = values_col.find_one({"concept_id": capex_id, "reporting_period.quarter": 3})
    assert q3_cap["value"] == -496000000.0 and q3_cap["calculated"] is True


def test_single_quarter_q3_deaccumulates_when_db_q2_is_still_cumulative(qcom_setup):
    """Extracting Q3 cashflow alone when Q1 and Q2 in DB have calculated=False (raw YTD)
    must deaccumulate both Q2 and Q3 to 3-month values."""
    service, values_col, op_cf_id, capex_id = qcom_setup
    cik = "0000804328"

    _seed_db_row(values_col, cik, op_cf_id, 2026, 1, "2025-12-28", 4965000000, calculated=False)
    _seed_db_row(values_col, cik, op_cf_id, 2026, 2, "2026-03-29", 7414000000, calculated=False)
    _seed_db_row(values_col, cik, op_cf_id, 2026, 3, "2026-06-28", 8405000000, calculated=False)

    _seed_db_row(values_col, cik, capex_id, 2026, 1, "2025-12-28", -549000000, calculated=False)
    _seed_db_row(values_col, cik, capex_id, 2026, 2, "2026-03-29", -1082000000, calculated=False)
    _seed_db_row(values_col, cik, capex_id, 2026, 3, "2026-06-28", -1578000000, calculated=False)

    stmts = [_make_cf_stmt(cik, 2026, 3, "2026-06-28", 8405000000, -1578000000)]
    service.process_company_from_statements(cik, stmts)

    q1_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 1})
    q2_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 2})
    q3_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 3})

    assert q1_op["value"] == 4965000000.0 and q1_op["calculated"] is True
    assert q2_op["value"] == 2449000000.0 and q2_op["calculated"] is True
    assert q3_op["value"] == 991000000.0 and q3_op["calculated"] is True


def test_single_quarter_q2_deaccumulates_using_db_q1(qcom_setup):
    """Extracting Q2 cashflow alone when Q1 is in DB must compute Q2_3m = Q2_6m - Q1_3m."""
    service, values_col, op_cf_id, capex_id = qcom_setup
    cik = "0000804328"

    _seed_db_row(values_col, cik, op_cf_id, 2026, 1, "2025-12-28", 4965000000, calculated=True)
    _seed_db_row(values_col, cik, op_cf_id, 2026, 2, "2026-03-29", 7414000000, calculated=False)
    _seed_db_row(values_col, cik, capex_id, 2026, 1, "2025-12-28", -549000000, calculated=True)
    _seed_db_row(values_col, cik, capex_id, 2026, 2, "2026-03-29", -1082000000, calculated=False)

    stmts = [_make_cf_stmt(cik, 2026, 2, "2026-03-29", 7414000000, -1082000000)]
    service.process_company_from_statements(cik, stmts)

    q2_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 2})
    q2_cap = values_col.find_one({"concept_id": capex_id, "reporting_period.quarter": 2})
    assert q2_op["value"] == 2449000000.0 and q2_op["calculated"] is True
    assert q2_cap["value"] == -533000000.0 and q2_cap["calculated"] is True


def test_deaccumulate_company_cashflow_in_db_repairs_existing_company(qcom_setup):
    """deaccumulate_company_cashflow_in_db converts existing cumulative DB rows to 3M calculated=True."""
    service, values_col, op_cf_id, capex_id = qcom_setup
    cik = "0000804328"

    _seed_db_row(values_col, cik, op_cf_id, 2026, 1, "2025-12-28", 4965000000, calculated=False)
    _seed_db_row(values_col, cik, op_cf_id, 2026, 2, "2026-03-29", 7414000000, calculated=False)
    _seed_db_row(values_col, cik, op_cf_id, 2026, 3, "2026-06-28", 8405000000, calculated=False)

    years = service.deaccumulate_company_cashflow_in_db(cik)
    assert years == 1

    q1_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 1})
    q2_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 2})
    q3_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 3})

    assert q1_op["value"] == 4965000000.0 and q1_op["calculated"] is True
    assert q2_op["value"] == 2449000000.0 and q2_op["calculated"] is True
    assert q3_op["value"] == 991000000.0 and q3_op["calculated"] is True


def test_ensure_prior_quarters_for_cashflow_prepends_when_db_missing():
    """When --quarter Q3 --statement cash_flow is requested and Q1/Q2 are missing from DB,
    _ensure_prior_quarters_for_cashflow prepends Q1 and Q2 10-Q filings."""
    from sec_scraper_cli import SECDataScraperApp

    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.target_fiscal_year = 2026
    app.target_fiscal_quarter = "Q3"
    app.target_statement = "cashflow"
    app.db = MagicMock()
    app.db.companies.find_one.return_value = {
        "corporate_info": {"fiscal_year_end": "0928"},
        "fiscal_year_convention": "end",
    }
    app._fye_cache = {}
    app._fyc_cache = {}
    app._cv_quarterly_col = MagicMock()
    app._cv_quarterly_col.find_one.return_value = None  # Q1 and Q2 not in DB

    all_filings = [
        {"form": "10-Q", "accessionNumber": "acc-q3", "reportDate": "2026-06-28"},
        {"form": "10-Q", "accessionNumber": "acc-q2", "reportDate": "2026-03-29"},
        {"form": "10-Q", "accessionNumber": "acc-q1", "reportDate": "2025-12-28"},
    ]
    filtered = [{"form": "10-Q", "accessionNumber": "acc-q3", "reportDate": "2026-06-28"}]

    result = app._ensure_prior_quarters_for_cashflow(
        "0000804328", filtered, all_filings, {"cik": "0000804328"}
    )
    assert [f["accessionNumber"] for f in result] == ["acc-q1", "acc-q2", "acc-q3"]

    # When Q1 and Q2 are already in DB, nothing is prepended
    app._cv_quarterly_col.find_one.return_value = {"_id": "exists"}
    result_existing = app._ensure_prior_quarters_for_cashflow(
        "0000804328", filtered, all_filings, {"cik": "0000804328"}
    )
    assert [f["accessionNumber"] for f in result_existing] == ["acc-q3"]


def test_sort_filings_by_fy_desc_quarter_asc_orders_q1_q2_q3_then_annual():
    """EDGAR returns filings by filingDate descending (FY26 Q3, Q2, Q1, FY25 10-K, FY25 Q3, Q2, Q1).
    _sort_filings_by_fy_desc_quarter_asc must reorder them to:
    FY26 Q1 -> FY26 Q2 -> FY26 Q3 -> FY25 Q1 -> FY25 Q2 -> FY25 Q3 -> FY25 Annual."""
    from sec_scraper_cli import SECDataScraperApp

    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.db = MagicMock()
    app.db.companies.find_one.return_value = {
        "corporate_info": {"fiscal_year_end": "0928"},
        "fiscal_year_convention": "end",
    }
    app._fye_cache = {}
    app._fy_convention_cache = {}

    edgar_filings = [
        {"form": "10-Q", "accessionNumber": "26-q3", "reportDate": "2026-06-28", "filingDate": "2026-07-30"},
        {"form": "10-Q", "accessionNumber": "26-q2", "reportDate": "2026-03-29", "filingDate": "2026-04-30"},
        {"form": "10-Q", "accessionNumber": "26-q1", "reportDate": "2025-12-28", "filingDate": "2026-01-30"},
        {"form": "10-K", "accessionNumber": "25-10k", "reportDate": "2025-09-28", "filingDate": "2025-11-05"},
        {"form": "10-Q", "accessionNumber": "25-q3", "reportDate": "2025-06-29", "filingDate": "2025-07-30"},
        {"form": "10-Q", "accessionNumber": "25-q2", "reportDate": "2025-03-30", "filingDate": "2025-04-30"},
        {"form": "10-Q", "accessionNumber": "25-q1", "reportDate": "2024-12-29", "filingDate": "2025-02-05"},
    ]

    sorted_filings = app._sort_filings_by_fy_desc_quarter_asc(
        edgar_filings, {"cik": "0000804328"}, edgar_filings
    )
    assert [f["accessionNumber"] for f in sorted_filings] == [
        "26-q1",
        "26-q2",
        "26-q3",
        "25-q1",
        "25-q2",
        "25-q3",
        "25-10k",
    ]


def test_immediate_per_quarter_deaccumulation_during_q1_q2_q3_reload(qcom_setup):
    """When filings are processed in Q1 -> Q2 -> Q3 order, each quarter's cashflow must be
    deaccumulated immediately upon persistence (without waiting for all filings to finish)."""
    from sec_scraper_cli import SECDataScraperApp

    service, values_col, op_cf_id, capex_id = qcom_setup
    cik = "0000804328"

    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app._quarterly_service = service
    app._quarterly_accumulator = {}
    app._cashflow_fy_accumulator = {}

    # 1. Persist & accumulate Q1 -> immediately calculated=True (3M)
    _seed_db_row(values_col, cik, op_cf_id, 2026, 1, "2025-12-28", 3619000000, calculated=False)
    q1_stmt = _make_cf_stmt(cik, 2026, 1, "2025-12-28", 3619000000, -319000000)
    app._accumulate_for_quarterly(cik, q1_stmt, q1_stmt["_filing_doc"])

    q1_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 1})
    assert q1_op["value"] == 3619000000.0 and q1_op["calculated"] is True

    # 2. Persist & accumulate Q2 -> immediately deaccumulated (7,414M - 3,619M = 3,795M) BEFORE Q3 runs
    _seed_db_row(values_col, cik, op_cf_id, 2026, 2, "2026-03-29", 7414000000, calculated=False)
    q2_stmt = _make_cf_stmt(cik, 2026, 2, "2026-03-29", 7414000000, -540000000)
    app._accumulate_for_quarterly(cik, q2_stmt, q2_stmt["_filing_doc"])

    q2_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 2})
    assert q2_op["value"] == 3795000000.0 and q2_op["calculated"] is True

    # 3. Persist & accumulate Q3 -> immediately deaccumulated (11,227M - (3,619M + 3,795M) = 3,813M)
    _seed_db_row(values_col, cik, op_cf_id, 2026, 3, "2026-06-28", 11227000000, calculated=False)
    q3_stmt = _make_cf_stmt(cik, 2026, 3, "2026-06-28", 11227000000, -792000000)
    app._accumulate_for_quarterly(cik, q3_stmt, q3_stmt["_filing_doc"])

    q3_op = values_col.find_one({"concept_id": op_cf_id, "reporting_period.quarter": 3})
    assert q3_op["value"] == 3813000000.0 and q3_op["calculated"] is True
    # And cashflow statements are not left pending in _quarterly_accumulator for end-of-company stage
    assert app._quarterly_accumulator.get(cik, []) == []
