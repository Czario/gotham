"""Filings-agent graph nodes.

Each node is a pure ``(state) -> state`` function. The factory functions here
(``make_*_node``) bind a node to its service dependency; ``graph.py`` wraps the
result with ``with_hooks`` before registering it on the LangGraph state graph.
"""
from filings_agent.nodes.fetch_filing import make_fetch_filing_node
from filings_agent.nodes.normalize import make_normalize_node
from filings_agent.nodes.validate import make_validate_node
from filings_agent.nodes.xbrl_extract import make_xbrl_extract_node

__all__ = [
    "make_fetch_filing_node",
    "make_normalize_node",
    "make_validate_node",
    "make_xbrl_extract_node",
]
