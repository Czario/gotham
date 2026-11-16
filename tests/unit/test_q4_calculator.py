"""Tests for automatic Q4 calculation from Annual + Q1-Q3 data."""
from bson import ObjectId

from core.processors.q4_calculator import auto_calculate_q4_for_fiscal_year


class _FakeColl:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def _matches(self, doc, q):
        for k, v in q.items():
            cur = doc
            for part in k.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if isinstance(v, dict):
                if "$in" in v and cur not in v["$in"]:
                    return False
                if "$ne" in v and cur == v["$ne"]:
                    return False
            elif cur != v:
                return False
        return True

    def find(self, q=None):
        return [d for d in self.docs if self._matches(d, q or {})]

    def find_one(self, q=None):
        for d in self.docs:
            if self._matches(d, q or {}):
                return d
        return None

    def insert_one(self, doc):
        d = dict(doc)
        d.setdefault("_id", ObjectId())
        self.docs.append(d)

    def insert_many(self, docs):
        for d in docs:
            self.insert_one(d)

    def update_one(self, q, upd):
        doc = self.find_one(q)
        if doc and "$set" in upd:
            doc.update(upd["$set"])


def test_auto_calculate_q4_income_bs_cf_and_missing_quarter_guard():
    db = {
        "normalized_concepts_annual": _FakeColl(),
        "normalized_concepts_quarterly": _FakeColl(),
        "concept_values_annual": _FakeColl(),
        "concept_values_quarterly": _FakeColl(),
    }
    cik = "0000745732"

    rev_a_id, rev_q_id = ObjectId(), ObjectId()
    sh_a_id, sh_q_id = ObjectId(), ObjectId()
    assets_a_id, assets_q_id = ObjectId(), ObjectId()

    db["normalized_concepts_annual"].insert_many([
        {"_id": rev_a_id, "cik": cik, "statement_type": "income", "concept": "us-gaap:Revenues"},
        {"_id": sh_a_id, "cik": cik, "statement_type": "income", "concept": "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"},
        {"_id": assets_a_id, "cik": cik, "statement_type": "balancesheet", "concept": "us-gaap:Assets"},
    ])
    db["normalized_concepts_quarterly"].insert_many([
        {"_id": rev_q_id, "cik": cik, "statement_type": "income", "concept": "us-gaap:Revenues"},
        {"_id": sh_q_id, "cik": cik, "statement_type": "income", "concept": "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"},
        {"_id": assets_q_id, "cik": cik, "statement_type": "balancesheet", "concept": "us-gaap:Assets"},
    ])

    db["concept_values_annual"].insert_many([
        {"cik": cik, "statement_type": "income", "concept_id": rev_a_id, "value": 1000.0, "dimension_value": False,
         "reporting_period": {"fiscal_year": 2025, "period_date": "2026-01-31"}},
        {"cik": cik, "statement_type": "income", "concept_id": sh_a_id, "value": 320.0, "dimension_value": False,
         "reporting_period": {"fiscal_year": 2025, "period_date": "2026-01-31"}},
        {"cik": cik, "statement_type": "balancesheet", "concept_id": assets_a_id, "value": 5000.0, "dimension_value": False,
         "reporting_period": {"fiscal_year": 2025, "period_date": "2026-01-31"}},
    ])

    # Only Q1 & Q2 present for Revenues (Q3 missing -> should NOT compute Q4 for Revenues yet)
    db["concept_values_quarterly"].insert_many([
        {"cik": cik, "statement_type": "income", "concept_id": rev_q_id, "value": 200.0, "dimension_value": False,
         "reporting_period": {"fiscal_year": 2025, "quarter": 1}},
        {"cik": cik, "statement_type": "income", "concept_id": rev_q_id, "value": 250.0, "dimension_value": False,
         "reporting_period": {"fiscal_year": 2025, "quarter": 2}},
    ])

    written1 = auto_calculate_q4_for_fiscal_year(db, cik, 2025)
    assert "income" not in written1
    assert written1["balancesheet"] == 1
    bs_q4 = db["concept_values_quarterly"].find_one({"cik": cik, "concept_id": assets_q_id, "reporting_period.quarter": 4})
    assert bs_q4["value"] == 5000.0
    assert bs_q4["calculated"] is True

    # Add Q3 for Revenues + Q1, Q2, Q3 for shares -> now Q4 income computes
    db["concept_values_quarterly"].insert_many([
        {"cik": cik, "statement_type": "income", "concept_id": rev_q_id, "value": 260.0, "dimension_value": False,
         "reporting_period": {"fiscal_year": 2025, "quarter": 3}},
        {"cik": cik, "statement_type": "income", "concept_id": sh_q_id, "value": 322.0, "dimension_value": False,
         "reporting_period": {"fiscal_year": 2025, "quarter": 1}},
        {"cik": cik, "statement_type": "income", "concept_id": sh_q_id, "value": 321.0, "dimension_value": False,
         "reporting_period": {"fiscal_year": 2025, "quarter": 2}},
        {"cik": cik, "statement_type": "income", "concept_id": sh_q_id, "value": 320.0, "dimension_value": False,
         "reporting_period": {"fiscal_year": 2025, "quarter": 3}},
    ])

    written2 = auto_calculate_q4_for_fiscal_year(db, cik, 2025)
    assert written2["income"] == 2
    rev_q4 = db["concept_values_quarterly"].find_one({"cik": cik, "concept_id": rev_q_id, "reporting_period.quarter": 4})
    sh_q4 = db["concept_values_quarterly"].find_one({"cik": cik, "concept_id": sh_q_id, "reporting_period.quarter": 4})
    assert rev_q4["value"] == 290.0
    assert rev_q4["calculated"] is True
    assert sh_q4["value"] == 320.0
