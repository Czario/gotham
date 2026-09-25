"""Navigation tools for the guidance pass.

A focused subset of the earning_agent's pi-style document tools, scoped to the
MD&A text: ``get_document_info``, ``read_lines`` and ``search``. The terminal
``finalize_guidance`` tool is supplied by the generic agent loop, so these tools
only read — they never write anything.
"""
from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import tool


def build_mda_tools(document_text: str) -> list[Any]:
    """Build the read-only MD&A navigation toolset."""
    lines = (document_text or "").split("\n")
    total_lines = len(lines)
    total_chars = len(document_text or "")

    word_index: dict[str, list[int]] = {}
    for i, line in enumerate(lines):
        for word in re.findall(r"[a-zA-Z]{4,}", line.lower()):
            word_index.setdefault(word, []).append(i)

    @tool
    def get_document_info() -> str:
        """Get an overview of the MD&A document (size + opening lines).

        Call this FIRST to orient yourself, then use search()/read_lines().
        """
        preview = []
        for line in lines:
            stripped = line.strip()
            if len(stripped) > 3:
                preview.append(stripped[:140])
            if len(preview) >= 20:
                break
        toc = "\n".join(f"  {i + 1:5d}: {p}" for i, p in enumerate(preview))
        return (
            f"MD&A document: {total_chars:,} characters, {total_lines:,} lines\n"
            f"Opening lines:\n{toc}"
        )

    @tool
    def read_lines(start: int, end: int) -> str:
        """Read a range of lines from the MD&A (1-based, inclusive)."""
        if start < 1 or end > total_lines or start > end:
            return (
                f"Invalid range. Document has {total_lines:,} lines. "
                f"Use read_lines(1, min(400, {total_lines}))."
            )
        out = []
        for i in range(start - 1, min(end, total_lines)):
            out.append(f"{i + 1:5d}: {lines[i][:400]}")
        return f"Lines {start}-{min(end, total_lines)} of {total_lines:,}:\n" + "\n".join(out)

    @tool
    def search(query: str, context_lines: int = 3) -> str:
        """Search the MD&A for a term, returning matches with context.

        Use it to locate forward-looking language quickly: "guidance",
        "outlook", "expect", "forecast", "anticipate", "we plan to".
        """
        if not query.strip():
            return "Empty query — provide a search term."
        query_lower = query.lower()
        words = re.findall(r"[a-zA-Z]{4,}", query_lower)

        candidates: set[int] = set()
        if words:
            candidates = set(word_index.get(words[0], []))
            for w in words[1:]:
                candidates &= set(word_index.get(w, []))
            if not candidates:
                for w in words:
                    candidates |= set(word_index.get(w, []))

        direct = {i for i, line in enumerate(lines) if query_lower in line.lower()}
        matches = candidates | direct
        if not matches:
            return f"No lines found matching '{query}'."

        ctx = max(0, int(context_lines))
        ordered = sorted(matches)
        blocks: list[list[int]] = [[ordered[0]]]
        for m in ordered[1:]:
            if m - blocks[-1][-1] <= ctx * 2:
                blocks[-1].append(m)
            else:
                blocks.append([m])

        out: list[str] = []
        for block in blocks[:15]:
            lo = max(0, block[0] - ctx)
            hi = min(total_lines, block[-1] + ctx + 1)
            out.append(f"── lines {lo + 1}-{hi} ──")
            for i in range(lo, hi):
                marker = ">>>" if i in matches else "   "
                out.append(f"{marker} {i + 1:5d}: {lines[i][:220]}")
        if len(blocks) > 15:
            out.append(f"... ({len(blocks) - 15} more blocks)")
        return "\n".join(out)

    return [get_document_info, read_lines, search]
