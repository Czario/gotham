"""Tests for --fix-hierarchy / --fix-hi in-place ADK hierarchy repair."""
import json
from bson import ObjectId
from langchain_core.messages import AIMessage

from filings_agent.nodes.fix_hierarchy import fix_company_statement_hierarchy


class _FakeColl:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def _matches(self, doc, q):
        for k, v in q.items():
            cur = doc
            for part in k.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if isinstance(v, dict):
                if "$exists" in v and ((cur is not None) != v["$exists"]):
                    return False
                if "$ne" in v and cur == v["$ne"]:
                    return False
            elif cur != v:
                return False
        return True

    def find(self, q=None, sort=None, projection=None):
        return [d for d in self.docs if self._matches(d, q or {})]

    def find_one(self, q=None, sort=None, projection=None):
        for d in self.docs:
            if self._matches(d, q or {}):
                return d
        return None

    def update_one(self, q, upd):
        doc = self.find_one(q)
        if doc and "$set" in upd:
            doc.update(upd["$set"])

    def update_many(self, q, upd):
        for doc in self.find(q):
            if "$set" in upd:
                doc.update(upd["$set"])

    def delete_one(self, q):
        for i, d in enumerate(self.docs):
            if self._matches(d, q):
                self.docs.pop(i)
                return


class _ScriptedFixChat:
    def __init__(self):
        self.step = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.step += 1
        if self.step == 1:
            rows = [
                {"concept": "us-gaap:Revenues", "parent": None, "position": 1},
                {"concept": "us-gaap:CostsAndExpenses", "parent": None, "position": 2},
                {"concept": "us-gaap:OperatingIncomeLoss", "parent": None, "position": 3},
            ]
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": "t1", "name": "propose_hierarchy", "args": {"rows_json": json.dumps(rows), "dims_json": "[]"}},
                ],
            )
        return AIMessage(
            content="",
            tool_calls=[
                {"id": "t2", "name": "finalize_hierarchy", "args": {"status": "done"}},
            ],
        )


def test_fix_company_statement_hierarchy_updates_in_place():
    cik = "0000745732"
    id_rev, id_op, id_ce = ObjectId(), ObjectId(), ObjectId()
    ccol = _FakeColl([
        {"_id": id_rev, "cik": cik, "statement_type": "income", "concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
        {"_id": id_op, "cik": cik, "statement_type": "income", "concept": "us-gaap:OperatingIncomeLoss", "path": "002", "order_key": "b"},
        {"_id": id_ce, "cik": cik, "statement_type": "income", "concept": "us-gaap:CostsAndExpenses", "path": "014", "order_key": "gn"},
    ])
    vcol = _FakeColl([
        {"_id": ObjectId(), "cik": cik, "statement_type": "income", "concept_id": id_ce, "value": 12000.0, "accession_number": "0000745732-26-000006", "reporting_period": {"fiscal_year": 2025}},
    ])
    db = {
        "normalized_concepts_annual": ccol,
        "concept_values_annual": vcol,
    }

    res = fix_company_statement_hierarchy(
        db, cik, "ROST", "annual", "income", chat_llm=_ScriptedFixChat()
    )
    assert res["updated"] == 3
    by_c = {d["concept"]: d for d in ccol.docs}
    assert by_c["us-gaap:CostsAndExpenses"]["_id"] == id_ce
    assert by_c["us-gaap:CostsAndExpenses"]["order_key"] == "b"
    assert by_c["us-gaap:OperatingIncomeLoss"]["order_key"] == "c"
    assert vcol.docs[0]["concept_id"] == id_ce
