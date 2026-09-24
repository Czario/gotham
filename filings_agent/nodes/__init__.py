"""Filings-agent graph nodes.

Each node is a pure ``(state) -> state`` function.  The factory functions here
(``make_*_node``) bind a node to its service dependency; ``graph.py`` wraps the
result with ``with_hooks`` before registering it on the LangGraph state graph.
"""
