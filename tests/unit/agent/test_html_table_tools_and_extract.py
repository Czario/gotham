"""Tests for shared SEC HTML table tools and the ADK HTML Extraction Agent."""
import json
from langchain_core.messages import AIMessage

from filings_agent.tools.html_table_tools import build_html_table_tools
from filings_agent.nodes.html_extract_agent import extract_statements_from_html_agent


SAMPLE_HTML = """
<html><body>
  <h2>Consolidated Statements of Earnings</h2>
  <p>(In thousands, except per share data)</p>
  <table>
    <tr><th></th><th>FY2025</th><th>FY2024</th></tr>
    <tr><td>Sales</td><td>$ 22,750,560</td><td>$ 21,129,220</td></tr>
    <tr><td>Cost of goods sold</td><td>(16,447,260)</td><td>(15,260,510)</td></tr>
    <tr><td>Net earnings per share - diluted</td><td>$ 6.32</td><td>$ 5.80</td></tr>
  </table>
  <h2>Consolidated Balance Sheets</h2>
  <table>
    <tr><th></th><th>Jan 31, 2026</th></tr>
    <tr><td>Cash and cash equivalents</td><td>$ 4,130,980</td></tr>
    <tr><td>Total assets</td><td>$ 15,554,572</td></tr>
  </table>
</body></html>
"""


def test_html_table_tools_list_read_search():
    tools = {t.name: t for t in build_html_table_tools(html_text=SAMPLE_HTML)}
    assert set(tools) == {"list_html_tables", "read_html_table", "search_html_text"}

    listed = tools["list_html_tables"].invoke({"statement_hint": "income"})
    assert "table_id=0" in listed
    assert "Consolidated Statements of Earnings" in listed

    read_out = tools["read_html_table"].invoke({"table_id": 0})
    assert "Sales | $ 22,750,560 | $ 21,129,220" in read_out
    assert "(16,447,260)" in read_out

    search_out = tools["search_html_text"].invoke({"query": "In thousands"})
    assert "In thousands" in search_out


class _ScriptedHTMLChat:
    """Minimal scripted chat model that exercises HTML table tools + submit_html_statement."""

    def __init__(self):
        self.step = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.step += 1
        if self.step == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": "c1", "name": "list_html_tables", "args": {"statement_hint": "income"}},
                    {"id": "c2", "name": "read_html_table", "args": {"table_id": 0}},
                ],
            )
        if self.step == 2:
            items = [
                {"concept": "us-gaap:Revenues", "label": "Sales", "value": 22750560, "abstract": False, "is_per_share": False},
                {"concept": "us-gaap:CostOfGoodsAndServicesSold", "label": "Cost of goods sold", "value": -16447260, "abstract": False, "is_per_share": False},
                {"concept": "us-gaap:EarningsPerShareDiluted", "label": "Net earnings per share - diluted", "value": 6.32, "abstract": False, "is_per_share": True},
            ]
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "c3",
                        "name": "submit_html_statement",
                        "args": {
                            "statement_type": "income",
                            "unit_multiplier": 1000.0,
                            "period_date": "2026-01-31",
                            "items_json": json.dumps(items),
                        },
                    }
                ],
            )
        return AIMessage(
            content="",
            tool_calls=[
                {"id": "c4", "name": "finalize_html_extraction", "args": {"status": "done"}},
            ],
        )


def test_extract_statements_from_html_agent():
    res = extract_statements_from_html_agent(
        html_text=SAMPLE_HTML,
        accession_number="0000745732-26-000006",
        ticker="ROST",
        chat_llm=_ScriptedHTMLChat(),
    )
    assert "income" in res
    items = {it["concept"]: it for it in res["income"]}
    assert items["us-gaap:Revenues"]["value"] == 22750560000.0
    assert items["us-gaap:CostOfGoodsAndServicesSold"]["value"] == -16447260000.0
    assert items["us-gaap:EarningsPerShareDiluted"]["value"] == 6.32
    assert items["us-gaap:EarningsPerShareDiluted"]["unit"] == "USD/shares"
