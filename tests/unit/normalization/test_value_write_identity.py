"""Regression tests for the value-write identity bugs.

Two coupled bugs caused a production ``DuplicateKeyError`` when reloading
AAPL's FY2026 Q3 10-Q:

1. ``_create_value_record`` built its own dedup query containing
   ``statement_type``, ``form_type``, ``calculated`` and
   ``reporting_period.period_date`` — all NARROWER than the DB unique index
   ``(cik, concept_id, reporting_period.fiscal_year, .quarter)``.  When any of
   those extra fields drifted (e.g. a different ``period_date`` on a row written
   by an earlier run), the lookup missed a row the index considered identical
   and the subsequent ``insert_one`` raised E11000.

2. ``--reload`` deleted by *accession number* while writes are unique by
   *period*, so a same-period row written under another accession survived — and
   the replacement write either skipped it (stale data) or collided with it.

These tests pin the fixes: the dedup must use the index-aligned repository
finder, and a reload must replace in place.
"""
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from bson import ObjectId

from data_normalization_service.core.config import AppConfig
from data_normalization_service.core.models import Filing, FinancialStatement
from data_normalization_service.services.normalization_service import (
    FinancialNormalizationService,
)


@pytest.fixture
def service(mocker):
    mocker.patch("data_normalization_service.services.normalization_service.DatabaseConnection")
    mocker.patch("data_normalization_service.services.normalization_service.DatabaseTracker")
    config = Mock(spec=AppConfig)
    config.database = Mock()
    return FinancialNormalizationService(config)


def _statement(reporting_period=None):
    return FinancialStatement(
        id=ObjectId(),
        company_cik="0000320193",
        filing_id=ObjectId(),
        statement_type="income",
        reporting_period=reporting_period
        or {"fiscal_year": 2026, "quarter": 3, "period_date": "2026-06-30"},
        created_at=datetime.now(tz=timezone.utc),
        financial_data=[],
    )


def _filing(form_type="10-Q"):
    return Filing(id=ObjectId(), form_type=form_type, accession_number="acc-1")


ITEM = {"concept": "us-gaap:Revenues", "label": "Revenue", "value": 999.0,
        "period": "2026-03-29 00:00:00 to 2026-06-27 00:00:00"}


def test_dedup_uses_the_unique_index_aligned_finder(service):
    """The old hand-built query (statement_type/form_type/calculated/period_date)
    must no longer be used — it was narrower than the unique index."""
    repo = Mock()
    existing_id = ObjectId()
    repo.find_existing_value.return_value = {
        "_id": existing_id, "value": 111111.0, "accession_number": "acc-0",
    }
    service._get_value_repo_by_form_type = Mock(return_value=repo)

    result = service._create_value_record(
        ObjectId(), _statement(), _filing(), dict(ITEM), 999.0
    )

    assert result == existing_id
    repo.find_existing_value.assert_called_once()
    args, kwargs = repo.find_existing_value.call_args
    assert kwargs["company_cik"] == "0000320193"
    assert kwargs["dimension_value"] is False
    assert args[1].get("fiscal_year") == 2026 and args[1].get("quarter") == 3
    # The narrow, crash-prone path is gone.
    repo.collection.find_one.assert_not_called()
    repo.collection.insert_one.assert_not_called()


def test_dedup_finder_ignores_drifting_metadata(service):
    """A row found for the period is reused regardless of the incoming
    period_date/statement_type — mirroring the unique index, which ignores them."""
    repo = Mock()
    existing_id = ObjectId()
    repo.find_existing_value.return_value = {"_id": existing_id}
    service._get_value_repo_by_form_type = Mock(return_value=repo)

    statement = _statement({"fiscal_year": 2026, "quarter": 3, "period_date": "2026-06-28"})
    service._create_value_record(ObjectId(), statement, _filing(), dict(ITEM), 999.0)

    # Even though the stored row's period_date differs, the finder (not a
    # period_date-scoped query) decided.
    repo.collection.find_one.assert_not_called()
    repo.collection.insert_one.assert_not_called()


def test_normal_rerun_is_insert_only(service):
    repo = Mock()
    # The row already carries an accession, so the only possible branch is the
    # plain "already exists -> skip" one.
    repo.find_existing_value.return_value = {
        "_id": ObjectId(), "value": 111111.0, "accession_number": "acc-0",
    }
    service._get_value_repo_by_form_type = Mock(return_value=repo)

    service._create_value_record(ObjectId(), _statement(), _filing(), dict(ITEM), 999.0)

    repo.collection.update_one.assert_not_called()  # ordinary reruns never overwrite


def test_reload_replaces_the_existing_period_row(service):
    repo = Mock()
    existing_id = ObjectId()
    repo.find_existing_value.return_value = {
        "_id": existing_id, "value": 111111.0, "accession_number": "acc-0",
    }
    service._get_value_repo_by_form_type = Mock(return_value=repo)

    result = service._create_value_record(
        ObjectId(), _statement(), _filing(), dict(ITEM), 999.0, replace_existing=True
    )

    assert result == existing_id
    repo.collection.insert_one.assert_not_called()  # never a second row for the period
    update = repo.collection.update_one.call_args.args[1]["$set"]
    assert update["value"] == 999.0
    assert update["reporting_period"]["period_date"] == "2026-06-30"
    assert update["accession_number"] == "acc-1"
    assert update["calculated"] is False


def test_reload_flag_threads_through_persist_statement_bundle(service, mocker):
    """persist_statement_bundle(replace_existing=True) must reach the writer."""
    captured = {}

    def fake_create(concept_id, statement, filing, item, value, **kwargs):
        captured.update(kwargs)
        return ObjectId()

    service._ensure_company_in_target_from_dict = Mock()
    service._get_or_create_concept = Mock(return_value=ObjectId())
    service._create_value_record = Mock(side_effect=fake_create)
    service._process_dimensional_data_enhanced = Mock()
    service._get_concept_repo_by_form_type = Mock(
        return_value=Mock(find_concept_reference_for_hierarchy=Mock(return_value=None))
    )

    from data_normalization_service.core.models import StatementBundle

    bundle = StatementBundle(
        company_cik="0000320193",
        statement_type="income",
        form_type="10-Q",
        reporting_period={"fiscal_year": 2026, "quarter": 3},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(),
        filing_id=ObjectId(),
        concepts=[{"concept": "us-gaap:Revenues", "label": "Revenue", "value": 999.0,
                   "period": "2026-03-29 00:00:00 to 2026-06-27 00:00:00",
                   "path": "001", "order_key": "a", "abstract": False}],
        company_doc={"cik": "0000320193"},
    )

    service.persist_statement_bundle(bundle, replace_existing=True)
    assert captured.get("replace_existing") is True


# ── integration: the exact crash class against a real Mongo ─────────────────


def _mongo_db_or_skip(name):
    import os

    from dotenv import load_dotenv
    from pymongo import MongoClient

    load_dotenv(".env")
    uri = os.getenv("MONGODB_URI")
    if not uri:
        pytest.skip("MONGODB_URI not configured")
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=2000)
        client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MongoDB unavailable: {exc}")
    client.drop_database(name)
    return client, client[name]


def test_conflicting_period_row_no_longer_crashes(mocker):
    """Reproduce the production failure: a same-period row with a different
    period_date plus the unique index must NOT raise DuplicateKeyError."""
    from pymongo import ASCENDING

    from data_normalization_service.core.models import StatementBundle

    client, db = _mongo_db_or_skip("filings_agent_dedup_regression")
    try:
        db["concept_values_quarterly"].create_index(
            [("cik", ASCENDING), ("concept_id", ASCENDING),
             ("reporting_period.fiscal_year", ASCENDING),
             ("reporting_period.quarter", ASCENDING)],
            unique=True, name="idx_unique_value",
        )
        concept_id = ObjectId()
        db["normalized_concepts_quarterly"].insert_one({
            "_id": concept_id, "cik": "0000320193", "statement_type": "income",
            "concept": "us-gaap:Revenues", "dimension_concept": False,
            "path": "001", "order_key": "a",
        })
        db["concept_values_quarterly"].insert_one({
            "cik": "0000320193", "concept_id": concept_id, "dimension_value": False,
            "statement_type": "income", "value": 111111.0,
            "accession_number": "acc-old",
            "reporting_period": {"fiscal_year": 2026, "quarter": 3,
                                 "period_date": "2026-06-28"},
        })

        # A REAL connection to the throwaway database — mocking the connection
        # here would send the writes to Mock objects and prove nothing.
        from data_normalization_service.core.config import AppConfig, DatabaseConfig

        import os

        from dotenv import load_dotenv

        load_dotenv(".env")
        config = AppConfig(
            database=DatabaseConfig(
                mongodb_uri=os.environ["MONGODB_URI"],
                database_name="filings_agent_dedup_regression",
            )
        )
        service = FinancialNormalizationService(config)

        def bundle():
            return StatementBundle(
                company_cik="0000320193", statement_type="income", form_type="10-Q",
                reporting_period={"fiscal_year": 2026, "quarter": 3,
                                  "period_date": "2026-06-30"},
                created_at=datetime.now(tz=timezone.utc),
                statement_id=ObjectId(), filing_id=ObjectId(),
                concepts=[{"concept": "us-gaap:Revenues", "label": "Revenue",
                           "value": 999.0,
                           "period": "2026-03-29 00:00:00 to 2026-06-27 00:00:00",
                           "level": 1, "order": 1, "abstract": False,
                           "path": "001", "order_key": "a"}],
                company_doc={"cik": "0000320193", "name": "Apple Inc."},
            )

        # A) ordinary rerun: no crash, existing row untouched
        assert service.persist_statement_bundle(bundle()) is True
        assert db["concept_values_quarterly"].count_documents({}) == 1
        assert db["concept_values_quarterly"].find_one({})["value"] == 111111.0

        # B) reload: no crash, the row is REPLACED in place
        assert service.persist_statement_bundle(bundle(), replace_existing=True) is True
        assert db["concept_values_quarterly"].count_documents({}) == 1
        row = db["concept_values_quarterly"].find_one({})
        assert row["value"] == 999.0
        assert row["reporting_period"]["period_date"] == "2026-06-30"
    finally:
        client.drop_database("filings_agent_dedup_regression")


# ── grouping headers keep the hierarchy nesting correct ─────────────────────


def test_grouping_headers_are_kept_as_abstract_rows(mocker):
    """Abstract mid-statement groupings must be persisted as parents.

    Without them each nested row attaches to the nearest preceding line item —
    the production symptom was R&D nested under Gross Profit instead of under
    the "Operating expenses:" group.
    """
    import os

    from dotenv import load_dotenv
    from pymongo import MongoClient

    from data_normalization_service.core.config import AppConfig, DatabaseConfig
    from filings_agent.hierarchy.resolver import resolve_hierarchy_bundles

    load_dotenv(".env")
    uri = os.getenv("MONGODB_URI")
    if not uri:
        pytest.skip("MONGODB_URI not configured")
    client = MongoClient(uri, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MongoDB unavailable: {exc}")
    db_name = "filings_agent_headers_regression"
    client.drop_database(db_name)
    try:
        service = FinancialNormalizationService(
            AppConfig(database=DatabaseConfig(mongodb_uri=uri, database_name=db_name))
        )

        def row(concept, value, level, order, abstract=False):
            return {"concept": concept, "label": concept.split(":")[-1], "value": value,
                    "period": "2026-03-29 00:00:00 to 2026-06-27 00:00:00",
                    "level": level, "order": order, "abstract": abstract,
                    "path": None, "order_key": None}

        doc = {
            "cik": "0000320193", "statement_type": "income",
            "reporting_period": {"end_date": "2026-06-27", "fiscal_year": 2026,
                                 "quarter": 3},
            "data": [
                row("us-gaap:IncomeStatementAbstract", None, 0, 0, abstract=True),
                row("us-gaap:Revenues", 1000.0, 0, 1),
                row("us-gaap:GrossProfit", 600.0, 0, 2),
                row("us-gaap:OperatingExpenses", None, 0, 3, abstract=True),
                row("us-gaap:ResearchAndDevelopmentExpense", 80.0, 1, 4),
                row("us-gaap:SellingGeneralAndAdministrativeExpense", 70.0, 1, 5),
                row("us-gaap:OperatingIncomeLoss", 450.0, 0, 6),
            ],
        }
        bundle = service.normalize_statement_to_bundle(
            doc, {"form_type": "10-Q"}, {"cik": "0000320193", "name": "Apple Inc."}
        )
        headers = [i["concept"] for i in bundle.abstract_concepts]
        # The mid-statement grouping is kept; the statement-level root is not.
        assert "us-gaap:OperatingExpenses" in headers
        assert "us-gaap:IncomeStatementAbstract" not in headers
        # A header must never also appear as a concrete row.
        assert "us-gaap:OperatingExpenses" not in [i["concept"] for i in bundle.concepts]

        plan = resolve_hierarchy_bundles([bundle], service, cik="0000320193")
        assert plan["integrity"] == {"duplicate_paths": 0, "orphans": 0}
        service.persist_statement_bundle(bundle)

        rows = list(client[db_name]["normalized_concepts_quarterly"].find(
            {"cik": "0000320193"}, {"_id": 0, "concept": 1, "path": 1, "abstract": 1}))
        by_concept = {r["concept"]: r for r in rows}

        header = by_concept["us-gaap:OperatingExpenses"]
        # In the DB, abstract is False for all items per business requirement
        assert header.get("abstract") is False
        # Both children hang off the grouping header, not off GrossProfit.
        for child in ("us-gaap:ResearchAndDevelopmentExpense",
                      "us-gaap:SellingGeneralAndAdministrativeExpense"):
            assert by_concept[child]["path"].startswith(header["path"] + ".")
        assert not by_concept["us-gaap:ResearchAndDevelopmentExpense"]["path"].startswith(
            by_concept["us-gaap:GrossProfit"]["path"] + "."
        )
    finally:
        client.drop_database(db_name)
