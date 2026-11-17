"""Shared SEC HTML statement-table inspection tools for ADK agents.

Provides ``list_html_tables``, ``read_html_table``, and ``search_html_text``
so any agent node (extraction, hierarchy, review) can inspect the human-visible
SEC ``.htm`` filing tables directly instead of relying solely on brittle
deterministic parsers.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from collections import OrderedDict
from typing import Any, Optional

from langchain_core.tools import StructuredTool

_HTML_INDEX_LOCK = threading.Lock()
_HTML_INDEX_CACHE: OrderedDict[str, tuple[list[dict[str, Any]], list[str]]] = OrderedDict()
_HTML_INDEX_MAX = 16


def load_filing_html(
    *,
    html_text: Optional[str] = None,
    accession_number: Optional[str] = None,
    ticker: Optional[str] = None,
    html_dir: Optional[str] = None,
) -> Optional[str]:
    """Return raw HTML text from memory or the local ``SEC_HTML_DOWNLOAD_PATH`` cache."""
    if html_text:
        return html_text
    if not accession_number:
        return None
    try:
        from filings_agent.mda import find_local_html, read_html_file

        resolved_dir = html_dir or os.getenv("SEC_HTML_DOWNLOAD_PATH")
        path = find_local_html(
            str(accession_number),
            ticker=str(ticker or ""),
            html_dir=resolved_dir,
        )
        if path:
            return read_html_file(path)
    except Exception:
        return None
    return None


def _clean_cell(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _table_context_title(table_tag: Any) -> str:
    """Best-effort preceding heading/caption text for a ``<table>`` element."""
    parts: list[str] = []
    caption = table_tag.find("caption")
    if caption:
        txt = _clean_cell(caption.get_text(" ", strip=True))
        if txt:
            parts.append(txt[:160])

    prev = table_tag
    for _ in range(12):
        prev = prev.find_previous(["h1", "h2", "h3", "h4", "p", "div", "b", "strong", "font"])
        if prev is None:
            break
        txt = _clean_cell(prev.get_text(" ", strip=True))
        if not txt or len(txt) > 220:
            continue
        if any(
            kw in txt.lower()
            for kw in (
                "consolidated", "statement", "balance sheet", "operations",
                "income", "cash flow", "earnings", "financial position",
                "in thousands", "in millions", "unaudited",
            )
        ):
            parts.insert(0, txt[:160])
            if len(parts) >= 2:
                break
    return " | ".join(dict.fromkeys(parts))


def _index_html_tables(html_text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse ``<table>`` elements and clean text lines from HTML once."""
    import warnings
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(html_text, "lxml")
    tables: list[dict[str, Any]] = []
    for idx, tbl in enumerate(soup.find_all("table")):
        rows: list[list[str]] = []
        for tr in tbl.find_all("tr"):
            cells = [_clean_cell(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
            # Collapse empty spacer columns (common in SEC HTML tables where '$' sits in its own <td>)
            non_empty = [c for c in cells if c and c != "$"]
            if non_empty:
                rows.append(non_empty)
        if len(rows) < 3:
            continue
        title = _table_context_title(tbl)
        tables.append({
            "table_id": len(tables),
            "dom_index": idx,
            "title": title,
            "row_count": len(rows),
            "rows": rows,
        })

    full_text = soup.get_text("\n")
    lines = [_clean_cell(ln) for ln in full_text.splitlines()]
    lines = [ln for ln in lines if ln]
    return tables, lines


def build_html_table_tools(
    *,
    html_text: Optional[str] = None,
    accession_number: Optional[str] = None,
    ticker: Optional[str] = None,
    html_dir: Optional[str] = None,
) -> list[StructuredTool]:
    """Build ``list_html_tables``, ``read_html_table``, and ``search_html_text`` tools."""
    cache: dict[str, Any] = {"loaded": False, "tables": [], "lines": []}

    def _ensure_loaded() -> bool:
        if cache["loaded"]:
            return bool(cache["tables"] or cache["lines"])
        cache["loaded"] = True
        cache_key = (
            f"acc:{accession_number}"
            if accession_number
            else (f"sha:{hashlib.sha256(html_text.encode('utf-8', errors='ignore')).hexdigest()[:24]}" if html_text else None)
        )
        if cache_key:
            with _HTML_INDEX_LOCK:
                hit = _HTML_INDEX_CACHE.get(cache_key)
                if hit is not None:
                    _HTML_INDEX_CACHE.move_to_end(cache_key)
                    cache["tables"], cache["lines"] = hit
                    return True

        raw = load_filing_html(
            html_text=html_text,
            accession_number=accession_number,
            ticker=ticker,
            html_dir=html_dir,
        )
        if not raw:
            return False
        try:
            tables, lines = _index_html_tables(raw)
            cache["tables"] = tables
            cache["lines"] = lines
            if cache_key:
                with _HTML_INDEX_LOCK:
                    _HTML_INDEX_CACHE[cache_key] = (tables, lines)
                    _HTML_INDEX_CACHE.move_to_end(cache_key)
                    while len(_HTML_INDEX_CACHE) > _HTML_INDEX_MAX:
                        _HTML_INDEX_CACHE.popitem(last=False)
            return True
        except Exception:
            return False

    def list_html_tables(statement_hint: str = "") -> str:
        """List financial tables in the SEC HTML filing with table_id, title, and preview rows."""
        if not _ensure_loaded():
            return "HTML filing not available locally for this accession."
        tables = cache["tables"]
        if not tables:
            return "No multi-row tables found in HTML filing."

        hint = (statement_hint or "").strip().lower()
        scored: list[tuple[int, dict[str, Any]]] = []
        for t in tables:
            blob = (t["title"] + " " + " ".join(" ".join(r) for r in t["rows"][:4])).lower()
            score = 0
            if hint:
                if "income" in hint or "oper" in hint:
                    for kw in ("operations", "income", "earnings", "revenue", "sales", "gross profit"):
                        if kw in blob:
                            score += 3
                elif "balance" in hint:
                    for kw in ("balance sheet", "financial position", "assets", "liabilities", "stockholders"):
                        if kw in blob:
                            score += 3
                elif "cash" in hint:
                    for kw in ("cash flows", "operating activities", "investing activities", "financing activities"):
                        if kw in blob:
                            score += 3
            if any(kw in blob for kw in ("consolidated", "in millions", "in thousands")):
                score += 1
            scored.append((score, t))

        scored.sort(key=lambda x: (-x[0], x[1]["table_id"]))
        top = [t for _, t in scored[:20]]
        out = [f"Found {len(tables)} table(s) in HTML (showing top {len(top)}):"]
        for t in top:
            preview = " | ".join(t["rows"][0][:5]) if t["rows"] else ""
            out.append(
                f"  table_id={t['table_id']} | rows={t['row_count']} | "
                f"title={t['title'] or '(none)'} | header={preview[:120]}"
            )
        return "\n".join(out)

    def read_html_table(table_id: int, start_row: int = 0, max_rows: int = 80) -> str:
        """Read rows from a specific SEC HTML table_id (preserving parenthesized negative values)."""
        if not _ensure_loaded():
            return "HTML filing not available locally for this accession."
        tables = cache["tables"]
        if table_id < 0 or table_id >= len(tables):
            return f"Invalid table_id={table_id}. Valid range: 0..{len(tables) - 1}."
        tbl = tables[table_id]
        rows = tbl["rows"]
        s = max(0, int(start_row))
        e = min(len(rows), s + max(1, min(int(max_rows), 150)))
        lines = [
            f"table_id={table_id} | title={tbl['title'] or '(none)'} | rows {s}..{e - 1} of {len(rows)}"
        ]
        for r_idx in range(s, e):
            lines.append(f"[{r_idx}] " + " | ".join(rows[r_idx]))
        return "\n".join(lines)

    def search_html_text(query: str, context_lines: int = 3) -> str:
        """Search the SEC HTML filing text for a phrase (e.g. 'in thousands', 'Gross profit', 'Operating income')."""
        if not _ensure_loaded():
            return "HTML filing not available locally for this accession."
        q = (query or "").strip().lower()
        if not q:
            return "Query must be non-empty."
        lines = cache["lines"]
        ctx = max(0, min(int(context_lines), 8))
        hits: list[str] = []
        for idx, ln in enumerate(lines):
            if q in ln.lower():
                lo = max(0, idx - ctx)
                hi = min(len(lines), idx + ctx + 1)
                snippet = "\n".join(f"  L{j}: {lines[j]}" for j in range(lo, hi))
                hits.append(snippet)
                if len(hits) >= 8:
                    break
        if not hits:
            return f"No matches for {query!r} in HTML filing."
        return f"Matches for {query!r} ({len(hits)} shown):\n" + "\n---\n".join(hits)

    return [
        StructuredTool.from_function(
            func=list_html_tables,
            name="list_html_tables",
            description="List candidate financial statement tables in the SEC .htm filing with table_id, title, and header preview.",
        ),
        StructuredTool.from_function(
            func=read_html_table,
            name="read_html_table",
            description="Read rows from a specific SEC .htm table_id (shows exact labels, numbers, and parentheses for negatives).",
        ),
        StructuredTool.from_function(
            func=search_html_text,
            name="search_html_text",
            description="Search the SEC .htm filing text for unit notes ('in thousands'/'in millions'), statement headings, or line items.",
        ),
    ]
