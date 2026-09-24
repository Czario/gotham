"""P3 — deterministic validation checks (ask 1).

Covers each validator (structure / math / periods / plausibility), the report
status derivation, and the validate node's report persistence.
"""
from datetime import datetime, timezone

import pytest
from bson import ObjectId

from data_normalization_service.core.models import StatementBundle
from filings_agent.nodes.validate import make_validate_node
from filings_agent.validation import validate_bundles
from filings_agent.validation.findings import HIGH, LOW, MEDIUM, blocking_findings
from filings_agent.validation.math import check_math
from filings_agent.validation.periods import check_periods
from filings_agent.validation.plausibility import check_plausibility
from filings_agent.validation.structure import check_structure


# ── factories ───────────────────────────────────────────────────────────────


def _item(concept, value, **overrides):
    base = {
        "concept": concept,
        "label": concept,
        "value": value,
        "period": "2024-01-01 00:00:00 to 2024-12-31 00:00:00",
        "level": 1,
        "order": 1,
        "abstract": False,
        "path": "001",
        "order_key": "a",
    }
    base.update(overrides)
    return base


def _bundle(statement_type="income", items=(), reporting_period=None, form_type="10-K"):
    return StatementBundle(
        company_cik="0000320193",
        statement_type=statement_type,
        form_type=form_type,
        reporting_period=(
            {"end_date": "2024-12-31", "fiscal_year": 2024}
            if reporting_period is None
            else reporting_period
        ),
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(),
        filing_id=ObjectId(),
        concepts=list(items),
    )


def _income_items(revenue=1_000_000, cor=400_000, gp=600_000):
    return [
        _item("us-gaap:Revenues", revenue, path="001", order_key="a"),
        _item("us-gaap:CostOfRevenue", cor, path="002", order_key="b"),
        _item("us-gaap:GrossProfit", gp, path="003", order_key="c"),
    ]


def _types(findings):
    return {f.type for f in findings}


# ── math ────────────────────────────────────────────────────────────────────


def test_math_identity_reconciles():
    assert check_math(_bundle(items=_income_items())) == []


def test_math_mismatch_is_high_and_blocking():
    findings = check_math(_bundle(items=_income_items(gp=999_999)))
    assert len(findings) == 1
    f = findings[0]
    assert f.type == "math_mismatch"
    assert f.severity == HIGH
    assert f.is_blocking
    assert f.evidence["identity"] == "gross_profit"
    assert f.evidence["difference"] == pytest.approx(399_999)


def test_math_tolerates_negative_cost_sign_convention():
    # Cost reported as a negative number must still reconcile.
    assert check_math(_bundle(items=_income_items(cor=-400_000))) == []


def test_math_skipped_when_required_inputs_absent():
    assert check_math(_bundle(items=[_item("us-gaap:GrossProfit", 600_000)])) == []


def test_balance_sheet_identity_reconciles():
    items = [
        _item("us-gaap:Assets", 5_000_000, path="001", order_key="a"),
        _item("us-gaap:Liabilities", 2_000_000, path="002", order_key="b"),
        _item("us-gaap:StockholdersEquity", 3_000_000, path="003", order_key="c"),
    ]
    assert check_math(_bundle("balancesheet", items)) == []


def test_balance_sheet_identity_mismatch_is_high():
    items = [
        _item("us-gaap:Assets", 5_000_000, path="001", order_key="a"),
        _item("us-gaap:Liabilities", 2_000_000, path="002", order_key="b"),
        _item("us-gaap:StockholdersEquity", 1_000_000, path="003", order_key="c"),
    ]
    findings = check_math(_bundle("balancesheet", items))
    assert [f.type for f in findings] == ["math_mismatch"]


def test_assets_equal_liabilities_and_equity():
    items = [
        _item("us-gaap:Assets", 5_000_000, path="001", order_key="a"),
        _item("us-gaap:LiabilitiesAndStockholdersEquity", 5_000_000, path="002", order_key="b"),
    ]
    assert check_math(_bundle("balancesheet", items)) == []


# ── structure ───────────────────────────────────────────────────────────────


def test_duplicate_path_order_is_high():
    items = [
        _item("us-gaap:Revenues", 1, path="001", order_key="a"),
        _item("us-gaap:CostOfRevenue", 2, path="001", order_key="a"),
    ]
    findings = check_structure(_bundle(items=items))
    dup = [f for f in findings if f.type == "duplicate_path_order"]
    assert dup and dup[0].severity == HIGH and dup[0].is_blocking


def test_duplicate_concept_is_medium():
    items = [
        _item("us-gaap:Revenues", 1, path="001", order_key="a"),
        _item("us-gaap:Revenues", 2, path="002", order_key="b"),
    ]
    findings = check_structure(_bundle(items=items))
    dup = [f for f in findings if f.type == "duplicate_concept"]
    assert dup and dup[0].severity == MEDIUM and not dup[0].is_blocking


def test_missing_hierarchy_path_is_medium():
    items = [_item("us-gaap:Revenues", 1, path=None, order_key=None)]
    findings = check_structure(_bundle(items=items))
    assert any(
        f.type == "missing_hierarchy_path" and f.severity == MEDIUM for f in findings
    )


def test_empty_statement_is_non_blocking():
    findings = check_structure(_bundle(items=[]))
    assert [f.type for f in findings] == ["empty_statement"]
    assert not findings[0].is_blocking


# ── periods ─────────────────────────────────────────────────────────────────


def test_missing_period_end_is_medium():
    findings = check_periods(_bundle(items=_income_items(), reporting_period={}))
    assert any(f.type == "missing_period_end" and f.severity == MEDIUM for f in findings)


def test_period_type_form_mismatch_is_medium():
    findings = check_periods(
        _bundle(
            items=_income_items(),
            reporting_period={"end_date": "2024-12-31", "fiscal_year": 2024, "period_type": "quarterly"},
        )
    )
    assert any(f.type == "period_type_form_mismatch" for f in findings)


def test_unusual_duration_is_low():
    items = [_item("us-gaap:Revenues", 1, period="2024-12-30 00:00:00 to 2024-12-31 00:00:00")]
    findings = check_periods(_bundle(items=items))
    assert any(f.type == "unusual_period_duration" and f.severity == LOW for f in findings)


def test_instant_period_has_no_duration_finding():
    items = [_item("us-gaap:Assets", 1, period="2024-12-31")]
    assert not any(f.type == "unusual_period_duration" for f in check_periods(_bundle("balancesheet", items)))


# ── plausibility ────────────────────────────────────────────────────────────


def test_implausible_magnitude_is_medium():
    findings = check_plausibility(_bundle(items=[_item("us-gaap:Assets", 1e16)]))
    assert any(f.type == "implausible_magnitude" and f.severity == MEDIUM for f in findings)


def test_unexpected_negative_value_is_medium():
    findings = check_plausibility(_bundle(items=[_item("us-gaap:Assets", -5)]))
    assert any(f.type == "unexpected_negative_value" for f in findings)


def test_all_values_identical_is_low():
    items = [
        _item("us-gaap:A", 1, path="001", order_key="a"),
        _item("us-gaap:B", 1, path="002", order_key="b"),
        _item("us-gaap:C", 1, path="003", order_key="c"),
    ]
    findings = check_plausibility(_bundle(items=items))
    assert any(f.type == "all_values_identical" and f.severity == LOW for f in findings)


def test_negative_equity_is_tolerated():
    items = [_item("us-gaap:StockholdersEquity", -1_000)]
    assert check_plausibility(_bundle("balancesheet", items)) == []


# ── report ──────────────────────────────────────────────────────────────────


def test_report_status_pass():
    report = validate_bundles([_bundle(items=_income_items())], cik="1", form_type="10-K")
    assert report.status == "pass"
    assert report.blocking == []


def test_report_status_fail_on_high_finding():
    report = validate_bundles(
        [_bundle(items=_income_items(gp=999_999))], cik="1", form_type="10-K"
    )
    assert report.status == "fail"
    assert len(report.blocking) == 1
    doc = report.to_dict()
    assert doc["status"] == "fail"
    assert doc["blocking_count"] == 1
    assert doc["severity_counts"]["high"] == 1


def test_report_status_pass_with_findings():
    items = _income_items()
    items[0]["path"] = None
    items[0]["order_key"] = None
    report = validate_bundles([_bundle(items=items)], cik="1", form_type="10-K")
    assert report.status == "pass_with_findings"
    assert report.blocking == []


def test_checks_run_per_bundle():
    report = validate_bundles(
        [
            _bundle(items=_income_items()),
            _bundle("balancesheet", [_item("us-gaap:Assets", 1)]),
        ],
        cik="1",
    )
    assert report.checks_run == {
        "structure": 2,
        "math": 2,
        "periods": 2,
        "plausibility": 2,
    }


# ── validate node ───────────────────────────────────────────────────────────


class _FakeStore:
    def __init__(self):
        self.saved = []

    def save(self, report):
        self.saved.append(report)


def _state(bundles):
    return {
        "cik": "0000320193",
        "ticker": "AAPL",
        "form_type": "10-K",
        "accession_number": "acc-1",
        "bundles": bundles,
    }


def test_validate_node_records_report_and_never_fails():
    node = make_validate_node(_FakeStore())
    out = node(_state([_bundle(items=_income_items(gp=999_999))]))

    assert out["status"] == "validated"  # never blocks here
    assert out["validation_report"]["status"] == "fail"
    assert out["validation_report"]["blocking_count"] == 1
    assert len(out["findings"]) == 1


def test_validate_node_persists_report():
    store = _FakeStore()
    node = make_validate_node(store)
    node(_state([_bundle(items=_income_items())]))
    assert len(store.saved) == 1
    assert store.saved[0].status == "pass"


def test_validate_node_survives_store_failure():
    class _Boom:
        def save(self, report):
            raise RuntimeError("mongo down")

    out = make_validate_node(_Boom())(_state([_bundle(items=_income_items())]))
    assert out["status"] == "validated"


def test_validate_node_without_store():
    out = make_validate_node(None)(_state([_bundle(items=_income_items())]))
    assert out["status"] == "validated"
    assert out["validation_report"]["status"] == "pass"


# ── blocking helper ─────────────────────────────────────────────────────────


def test_blocking_findings_exempts_absence_only_types():
    assert blocking_findings(
        [{"type": "missing_concept", "severity": HIGH, "message": "x"}]
    ) == []
    assert blocking_findings(
        [{"type": "math_mismatch", "severity": HIGH, "message": "x"}]
    )
    assert blocking_findings(
        [{"type": "duplicate_concept", "severity": MEDIUM, "message": "x"}]
    ) == []
