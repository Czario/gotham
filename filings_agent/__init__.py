"""Filings agent — LangGraph pipeline for SEC XBRL statement bundles.

Reuses the proven architecture of the earning_agent (LangGraph state graph,
``with_hooks`` node wrapper, STRICT_ACCURACY save gate, LLM factory, ReAct
tool-calling loop), adapted for deterministic XBRL extraction: the pipeline
computes an in-memory bundle, the agent validates/repairs/re-hierarchies it,
and only then persists it.

This package intentionally has no import side effects — import the submodules
you need (``filings_agent.graph``, ``filings_agent.llm``, ...).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
