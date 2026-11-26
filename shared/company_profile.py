"""Company Profile extraction, continuous update diff engine, and MongoDB persistence.

Maintains two collections in ``normalize_data``:
  • ``company_profiles``        — one canonical profile document per CIK with
                                  provenance citations, ``manual_overrides``
                                  protection, and ``pending_auto_updates``.
  • ``company_profile_history`` — append-only field-level change audit log.

Data flow:
  1. Deterministic (0 LLM cost): SEC Submissions JSON + cover/DEI share counts.
  2. Targeted LLM (1x per new 10-K / 20-F): Item 1 Business & Human Capital,
     Item 2 Properties/Facilities, Executive Officers, Auditor, Segments.
  3. Event delta (8-K Item 5.02 / 4.01): real-time leadership & auditor updates.
  4. Merge + Diff Engine: respects ``manual_overrides`` locks (queues newer SEC
     values into ``pending_auto_updates`` instead of overwriting admin edits)
     and writes changed fields to ``company_profile_history``.
"""
from __future__ import annotations

import copy
import gzip
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_SEC_HEADERS = {
    "User-Agent": os.getenv(
        "SEC_USER_AGENT", "TrueGridsGotham/1.0 admin@truegrids.com"
    ),
    "Accept-Encoding": "gzip, deflate",
}

_STATE_NAMES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Dot-paths that participate in diffing, manual override protection, and history
TRACKED_FIELDS: tuple[str, ...] = (
    "company_name",
    "ticker",
    "exchange",
    "website",
    "overview.summary",
    "overview.mission",
    "overview.founded_year",
    "workforce.total_employees",
    "workforce.breakdown",
    "workforce.as_of",
    "key_facts.sic",
    "key_facts.sic_description",
    "key_facts.headquarters",
    "key_facts.phone",
    "key_facts.incorporated_in",
    "key_facts.fiscal_year_end",
    "key_facts.auditor",
    "key_facts.ein",
    "shares.shares_outstanding",
    "shares.shares_as_of",
    "shares.public_float_usd",
    "shares.public_float_as_of",
    "leadership.executives",
    "leadership.as_of",
    "board_of_directors.directors",
    "board_of_directors.as_of",
    "key_people.people",
    "social_media.x",
    "social_media.linkedin",
    "social_media.youtube",
    "social_media.instagram",
    "social_media.facebook",
    "segments.items",
    "segments.as_of",
    "facilities.total_sq_ft",
    "facilities.owned_sq_ft",
    "facilities.leased_sq_ft",
    "facilities.highlights",
    "facilities.as_of",
)


def _pad_cik(cik: str | int) -> str:
    digits = "".join(ch for ch in str(cik or "") if ch.isdigit())
    return digits.zfill(10) if digits else str(cik or "").strip()


def _format_fy_end(mmdd: str | None) -> str:
    raw = str(mmdd or "").strip()
    if len(raw) == 4 and raw.isdigit():
        m, d = int(raw[:2]), int(raw[2:])
        if 1 <= m <= 12:
            return f"{_MONTH_NAMES[m]} {d}"
    return raw


def _format_ein(ein: str | None) -> str:
    digits = "".join(ch for ch in str(ein or "") if ch.isdigit())
    if len(digits) == 9:
        return f"{digits[:2]}-{digits[2:]}"
    return str(ein or "").strip()


def _get_nested(doc: dict[str, Any], path: str) -> Any:
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_nested(doc: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def _is_meaningful(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, (list, dict)):
        return len(val) > 0
    return True


def _sec_archive_url(cik: str, accession: str, primary_doc: str) -> str:
    cik_int = str(int(_pad_cik(cik))) if _pad_cik(cik).isdigit() else cik
    acc_clean = accession.replace("-", "")
    if primary_doc:
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{primary_doc}"
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/"


# ── 1. Deterministic extraction (0 LLM cost) ─────────────────────────────────

def extract_deterministic_profile(
    cik: str,
    submissions: dict[str, Any],
    dei_facts: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build deterministic profile fields from SEC submissions JSON (+ optional DEI facts)."""
    cik_padded = _pad_cik(cik)
    tickers = submissions.get("tickers") or []
    exchanges = submissions.get("exchanges") or []
    ticker = str(tickers[0]).upper() if tickers else ""
    exchange = str(exchanges[0]) if exchanges else ""

    biz_addr = ((submissions.get("addresses") or {}).get("business") or {})
    state_code = str(biz_addr.get("stateOrCountry") or "").strip().upper()
    state_desc = str(biz_addr.get("stateOrCountryDescription") or "").strip()
    country = "US" if state_code in _STATE_NAMES else (state_desc or state_code)
    hq = {
        "street": " ".join(
            p for p in [biz_addr.get("street1"), biz_addr.get("street2")] if p
        ).strip(),
        "city": str(biz_addr.get("city") or "").strip(),
        "state": str(biz_addr.get("stateOrCountry") or "").strip(),
        "postal_code": str(biz_addr.get("zipCode") or "").strip(),
        "country": country,
    }
    hq = {k: v for k, v in hq.items() if v}

    inc_code = str(submissions.get("stateOfIncorporation") or "").strip().upper()
    incorporated_in = _STATE_NAMES.get(inc_code, inc_code)

    out: dict[str, Any] = {
        "cik": cik_padded,
        "company_name": str(submissions.get("name") or "").strip(),
        "ticker": ticker,
        "exchange": exchange,
        "website": str(submissions.get("website") or "").strip() or None,
        "key_facts": {
            "sic": str(submissions.get("sic") or "").strip(),
            "sic_description": str(submissions.get("sicDescription") or "").strip(),
            "headquarters": hq or None,
            "phone": str(submissions.get("phone") or "").strip(),
            "incorporated_in": incorporated_in,
            "fiscal_year_end": _format_fy_end(submissions.get("fiscalYearEnd")),
            "ein": _format_ein(submissions.get("ein")),
        },
        "sources": [
            {
                "label": f"SEC submissions record – {submissions.get('name') or ticker} (CIK {int(cik_padded) if cik_padded.isdigit() else cik_padded})",
                "url": f"https://data.sec.gov/submissions/CIK{cik_padded}.json",
            }
        ],
    }

    if dei_facts:
        shares: dict[str, Any] = {}
        if dei_facts.get("shares_outstanding") is not None:
            shares["shares_outstanding"] = int(dei_facts["shares_outstanding"])
        if dei_facts.get("shares_as_of"):
            shares["shares_as_of"] = str(dei_facts["shares_as_of"])
        if dei_facts.get("shares_citation"):
            shares["shares_citation"] = str(dei_facts["shares_citation"])
        if dei_facts.get("shares_accession"):
            shares["shares_accession"] = str(dei_facts["shares_accession"])
        if dei_facts.get("public_float_usd") is not None:
            shares["public_float_usd"] = float(dei_facts["public_float_usd"])
        if dei_facts.get("public_float_as_of"):
            shares["public_float_as_of"] = str(dei_facts["public_float_as_of"])
        if dei_facts.get("public_float_citation"):
            shares["public_float_citation"] = str(dei_facts["public_float_citation"])
        if shares:
            out["shares"] = shares

    return out


def extract_latest_dei_from_companyfacts(cik: str) -> dict[str, Any]:
    """Fetch latest ``EntityCommonStockSharesOutstanding`` & ``EntityPublicFloat`` from SEC companyfacts."""
    cik_padded = _pad_cik(cik)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
    out: dict[str, Any] = {}
    try:
        resp = requests.get(url, headers=_SEC_HEADERS, timeout=20)
        if resp.status_code != 200:
            return out
        dei = (resp.json().get("facts") or {}).get("dei") or {}

        # Latest shares outstanding
        sh_units = ((dei.get("EntityCommonStockSharesOutstanding") or {}).get("units") or {})
        sh_rows = sh_units.get("shares") or []
        if sh_rows:
            latest_sh = max(sh_rows, key=lambda r: (str(r.get("end") or ""), str(r.get("filed") or "")))
            if latest_sh.get("val") is not None:
                out["shares_outstanding"] = int(latest_sh["val"])
                out["shares_as_of"] = str(latest_sh.get("end") or "")
                out["shares_citation"] = f"{latest_sh.get('form') or 'SEC'} cover"
                out["shares_accession"] = str(latest_sh.get("accn") or "")

        # Latest public float
        pf_units = ((dei.get("EntityPublicFloat") or {}).get("units") or {})
        pf_rows = pf_units.get("USD") or []
        if pf_rows:
            latest_pf = max(pf_rows, key=lambda r: (str(r.get("end") or ""), str(r.get("filed") or "")))
            if latest_pf.get("val") is not None:
                out["public_float_usd"] = float(latest_pf["val"])
                out["public_float_as_of"] = str(latest_pf.get("end") or "")
                out["public_float_citation"] = f"{latest_pf.get('form') or '10-K'} cover"
    except Exception as exc:  # noqa: BLE001
        logger.debug("company_profile: DEI companyfacts lookup failed for %s: %s", cik_padded, exc)
    return out


# ── 2. Agentic document navigation (grep / read_lines) & profile extraction ──

def _html_to_clean_text(html: str) -> str:
    import warnings
    from bs4 import XMLParsedAsHTMLWarning

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "ix:header", "ix:hidden"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"[\u00a0\s]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def extract_sections_from_annual_html(html: str) -> dict[str, str]:
    """Legacy helper retained for compatibility; slices compact windows from annual HTML."""
    text = _html_to_clean_text(html)
    sections: dict[str, str] = {"cover": text[:8000]}

    def _find_window(pattern: str, length: int, start_after: int = 2000) -> str:
        matches = list(re.finditer(pattern, text[start_after:], flags=re.IGNORECASE))
        if not matches:
            return ""
        m = matches[1] if len(matches) >= 2 else matches[0]
        pos = start_after + m.start()
        return text[pos : pos + length]

    sections["item1_business"] = _find_window(
        r"(?:ITEM\s+1\.?\s+BUSINESS|ITEM\s+4\.?\s+INFORMATION\s+ON\s+THE\s+COMPANY)",
        22000,
    )
    sections["human_capital"] = _find_window(r"HUMAN\s+CAPITAL|EMPLOYEES", 6000)
    sections["item2_properties"] = _find_window(
        r"(?:ITEM\s+2\.?\s+PROPERTIES|PROPERTY,\s+PLANTS\s+AND\s+EQUIPMENT)", 7000
    )
    sections["executive_officers"] = _find_window(
        r"(?:INFORMATION\s+ABOUT\s+OUR\s+EXECUTIVE\s+OFFICERS|EXECUTIVE\s+OFFICERS\s+OF\s+THE\s+REGISTRANT|DIRECTORS,\s+SENIOR\s+MANAGEMENT)",
        10000,
    )
    sections["auditor"] = _find_window(
        r"REPORT\s+OF\s+INDEPENDENT\s+REGISTERED\s+PUBLIC\s+ACCOUNTING\s+FIRM", 4000
    )
    return {k: v for k, v in sections.items() if v}


def _strip_json_fences(raw: str) -> str:
    s = str(raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _norm_person_name(name: str) -> str:
    tokens = [t.strip(".").lower() for t in str(name or "").split() if t.strip(".")]
    if len(tokens) >= 3:
        return f"{tokens[0]} {tokens[-1]}"
    return " ".join(tokens)


def _ensure_person_social_fields(
    people: list[dict[str, Any]],
    existing_people: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Ensure every person dict has ``x`` and ``linkedin`` keys, preserving existing non-null links when incoming is null."""
    existing_by_name = {
        _norm_person_name(p["name"]): p
        for p in (existing_people or [])
        if isinstance(p, dict) and p.get("name")
    }
    out: list[dict[str, Any]] = []
    for p in people:
        if not isinstance(p, dict):
            continue
        item = dict(p)
        prev = existing_by_name.get(_norm_person_name(item.get("name") or ""))
        for sk in ("x", "linkedin"):
            val = item.get(sk)
            if isinstance(val, str):
                val = val.strip() or None
            if val is None and prev and prev.get(sk):
                val = prev[sk]
            item[sk] = val
        out.append(item)
    return out


_SOCIAL_URL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("x", re.compile(r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,30})/?$", re.IGNORECASE)),
    ("linkedin", re.compile(r"^https?://(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/(?:company|in)/([^/?#]+)", re.IGNORECASE)),
    ("youtube", re.compile(r"^https?://(?:www\.)?youtube\.com/(?:@|channel/|c/|user/)?([^/?#]+)", re.IGNORECASE)),
    ("instagram", re.compile(r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{1,40})/?$", re.IGNORECASE)),
    ("facebook", re.compile(r"^https?://(?:www\.)?facebook\.com/([A-Za-z0-9_.]{1,60})/?$", re.IGNORECASE)),
]
_SOCIAL_BLOCKLIST = {"share", "intent", "home", "search", "login", "signup", "sharer", "hashtag", "explore", "watch"}


def extract_company_social_media_from_website(website: Optional[str]) -> dict[str, str]:
    """Best-effort deterministic scrape of official company homepage for social media links (JSON-LD sameAs + anchor tags)."""
    raw = str(website or "").strip()
    if not raw:
        return {}
    url = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
    found: dict[str, str] = {}
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; TrueGridsBot/1.0; +https://truegrids.com)",
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=8,
            allow_redirects=True,
        )
        if resp.status_code != 200 or not resp.text:
            return {}
        import warnings
        from bs4 import XMLParsedAsHTMLWarning

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(resp.text[:500_000], "lxml")

        candidate_urls: list[str] = []
        # 1. JSON-LD sameAs (highest signal)
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                blob = json.loads(script.string or "")
                items = blob if isinstance(blob, list) else [blob]
                for item in items:
                    if isinstance(item, dict):
                        same_as = item.get("sameAs")
                        if isinstance(same_as, str):
                            candidate_urls.append(same_as)
                        elif isinstance(same_as, list):
                            candidate_urls.extend(str(u) for u in same_as if u)
            except Exception:
                continue

        # 2. Anchor hrefs
        for a in soup.find_all("a", href=True):
            href = str(a["href"]).strip()
            if any(dom in href.lower() for dom in ("x.com/", "twitter.com/", "linkedin.com/", "youtube.com/", "instagram.com/", "facebook.com/")):
                candidate_urls.append(href)

        for cand in candidate_urls:
            clean = cand.split("?")[0].split("#")[0].rstrip("/")
            for platform, rx in _SOCIAL_URL_PATTERNS:
                if platform in found:
                    continue
                m = rx.match(clean)
                if m and m.group(1).lower() not in _SOCIAL_BLOCKLIST:
                    found[platform] = clean
    except Exception as exc:  # noqa: BLE001
        logger.debug("company_profile: website social media lookup skipped for %s: %s", url, exc)
    return found


def _normalize_extracted_profile(parsed: Any) -> dict[str, Any]:
    """Map raw LLM/agent JSON into the canonical ``company_profiles`` structure."""
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, Any] = {}
    if parsed.get("website"):
        result["website"] = str(parsed["website"]).strip()
    if isinstance(parsed.get("social_media"), dict):
        sm = {
            k: (str(v).strip() or None) if v is not None else None
            for k, v in parsed["social_media"].items()
            if k in {"x", "linkedin", "youtube", "instagram", "facebook"}
        }
        if any(sm.values()):
            result["social_media"] = sm
    if isinstance(parsed.get("overview"), dict):
        result["overview"] = parsed["overview"]
    if isinstance(parsed.get("workforce"), dict):
        result["workforce"] = parsed["workforce"]
    if parsed.get("auditor"):
        result["key_facts"] = {"auditor": str(parsed["auditor"]).strip()}
    if isinstance(parsed.get("leadership"), dict):
        lead = dict(parsed["leadership"])
        if isinstance(lead.get("executives"), list):
            lead["executives"] = _ensure_person_social_fields(lead["executives"])
        result["leadership"] = lead
    if isinstance(parsed.get("segments"), dict):
        result["segments"] = parsed["segments"]
    if isinstance(parsed.get("facilities"), dict):
        result["facilities"] = parsed["facilities"]
    if isinstance(parsed.get("shares"), dict):
        sh = {k: v for k, v in parsed["shares"].items() if v is not None}
        if sh:
            result["shares"] = sh
    return result


def build_profile_document_tools(document_text: str, form_type: str = "10-K") -> list[Any]:
    """Build coding-agent-style read-only navigation tools (``get_document_info``, ``grep``, ``read_lines``).

    Mirrors the pi-style navigation tools used in ``earnings_agents`` (8-K / 6-K / PDF)
    and ``filings_agent`` (MD&A / targeted text extraction) so the agent locates exact
    sections in any filing layout without hardcoded regex offsets or prompt truncation.
    """
    from langchain_core.tools import tool as _lc_tool

    lines = (document_text or "").split("\n")
    total_lines = len(lines)
    total_chars = len(document_text or "")

    @_lc_tool
    def get_document_info() -> str:
        """Get document size (lines, chars) and opening lines (cover / Table of Contents).

        Call this first if needed, or jump straight to ``grep`` calls.
        """
        preview: list[str] = []
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if len(stripped) > 2:
                preview.append(f"  {idx + 1:5d}: {stripped[:160]}")
            if len(preview) >= 35:
                break
        return (
            f"SEC {form_type} plain-text document: {total_chars:,} characters, {total_lines:,} lines\n"
            f"Opening lines:\n" + "\n".join(preview)
        )

    @_lc_tool
    def grep(pattern: str, context_lines: int = 3, max_matches: int = 20) -> str:
        """Search the full filing text using a case-insensitive regex or literal phrase.

        Returns matching line numbers with surrounding context so you can call
        ``read_lines(start, end)`` on the exact section you need.
        """
        pat = str(pattern or "").strip()
        if not pat:
            return "Empty pattern — provide a regex or search phrase."
        try:
            rx = re.compile(pat, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pat), re.IGNORECASE)

        matched_idxs = [i for i, ln in enumerate(lines) if rx.search(ln)]
        if not matched_idxs:
            return f"No lines found matching '{pat}'."

        ctx = max(0, min(int(context_lines), 10))
        cap = max(1, min(int(max_matches), 35))
        blocks: list[list[int]] = [[matched_idxs[0]]]
        for m in matched_idxs[1:]:
            if m - blocks[-1][-1] <= ctx * 2:
                blocks[-1].append(m)
            else:
                blocks.append([m])

        match_set = set(matched_idxs)
        out: list[str] = [f"Found {len(matched_idxs)} matching line(s) in {len(blocks)} block(s) for '{pat}':"]
        char_count = len(out[0])
        for block in blocks[:cap]:
            lo = max(0, block[0] - ctx)
            hi = min(total_lines, block[-1] + ctx + 1)
            chunk_lines = [f"── lines {lo + 1}-{hi} ──"]
            for i in range(lo, hi):
                marker = ">>>" if i in match_set else "   "
                chunk_lines.append(f"{marker} {i + 1:5d}: {lines[i][:240]}")
            chunk = "\n".join(chunk_lines)
            if char_count + len(chunk) > 14_000:
                out.append("... (output truncated; narrow your grep pattern or call read_lines)")
                break
            out.append(chunk)
            char_count += len(chunk)
        if len(blocks) > cap:
            out.append(f"... ({len(blocks) - cap} more blocks omitted)")
        return "\n".join(out)

    @_lc_tool
    def read_lines(start: int, end: int) -> str:
        """Read a 1-based inclusive range of lines from the filing (max 250 lines per call)."""
        if total_lines == 0:
            return "Document is empty."
        s = max(1, int(start))
        e = min(total_lines, int(end))
        if s > total_lines or s > e:
            return (
                f"Invalid range ({start}-{end}). Document has {total_lines:,} lines. "
                f"Use read_lines(1, {min(250, total_lines)})."
            )
        clamped = False
        if e - s + 1 > 250:
            e = s + 249
            clamped = True
        out = [f"{i + 1:5d}: {lines[i][:450]}" for i in range(s - 1, e)]
        header = f"Lines {s}-{e} of {total_lines:,}"
        if clamped:
            header += " (clamped to 250 lines; call read_lines again for more)"
        return header + ":\n" + "\n".join(out)

    return [get_document_info, grep, read_lines]


_PROFILE_SCHEMA_JSON = """{
  "website": "www.example.com (main corporate domain, not /investor unless only one given) or null",
  "social_media": {
    "x": "https://x.com/handle or null (only if explicitly disclosed in filing)",
    "linkedin": "https://www.linkedin.com/company/... or null",
    "youtube": null,
    "instagram": null,
    "facebook": null
  },
  "overview": {
    "summary": "2-3 concise sentences describing what the company builds, sells, and who its customers are",
    "mission": "exact mission statement quote if explicitly stated, else null",
    "founded_year": 1975
  },
  "workforce": {
    "total_employees": 223000,
    "breakdown": {"us": 121000, "international": 102000},
    "as_of": "YYYY-MM-DD or null"
  },
  "auditor": "Independent registered public accounting firm name (e.g. Deloitte & Touche LLP) or null",
  "leadership": {
    "as_of": "YYYY-MM-DD or null",
    "executives": [
      {"name": "Full Name", "age": 58, "title": "Exact Title", "is_ceo": true, "is_cfo": false, "x": null, "linkedin": null}
    ]
  },
  "segments": {
    "as_of": "YYYY-MM-DD or null",
    "items": [
      {"name": "Reportable Segment Name", "description": "1 concise sentence summarizing the segment's products/services"}
    ]
  },
  "facilities": {
    "as_of": "YYYY-MM-DD or null",
    "total_sq_ft": 112000000,
    "owned_sq_ft": 58000000,
    "leased_sq_ft": 54000000,
    "highlights": ["Redmond campus: ~14M sq ft (~12M owned) on ~520 acres"]
  },
  "shares": {
    "shares_outstanding": null,
    "shares_as_of": "YYYY-MM-DD or null"
  }
}"""


def extract_profile_with_agent(
    document_text: str,
    *,
    ticker: str,
    company_name: str,
    form_type: str,
    accession: str,
    max_steps: int = 20,
    chat_llm: Any = None,
) -> dict[str, Any]:
    """Extract structured company profile using a coding-agent-style tool loop (``grep`` / ``read_lines``).

    Works uniformly across 10-K, 20-F, 8-K, 6-K, and PDF plain-text documents for
    US and Foreign issuers across all industries (tech, banks, retail, pharma, industrials).
    """
    from filings_agent.agent.loop import run_agent_loop

    tools = build_profile_document_tools(document_text, form_type=form_type)
    system_prompt = (
        f"You are an institutional equity research extraction agent analyzing an SEC {form_type} "
        f"for {company_name} ({ticker}, accession {accession}).\n\n"
        "You have coding-agent-style document navigation tools: `get_document_info`, `grep`, and `read_lines`.\n"
        "NEVER guess or fabricate any number, date, name, or segment — verify every fact directly in the document.\n\n"
        "Strategy (be fast and thorough — issue multiple `grep` calls in parallel):\n"
        "1. **Overview, Website & Cover Shares**: `grep` for `Item 1\\b|Item 4\\b|Information on the Company|mission|founded|www\\.` "
        "and read the cover + Business overview (Item 1 for 10-K, Item 4 for 20-F) for `summary`, `mission`, `founded_year`, "
        "main corporate `website` (prefer root domain like `www.apple.com`, not `/investor`), and cover-page `shares.shares_outstanding` + `shares_as_of` (especially important for 20-F).\n"
        "2. **Reportable Segments**: `grep` for `reportable segments|operating segments|business segments|Note \\d+.*Segment` "
        "and read BOTH Item 1/Item 4 AND the Segment Footnote if needed so EVERY reportable segment has a concise 1-sentence description.\n"
        "3. **Workforce / Human Capital**: Companies use many terms (`employees`, `full-time equivalent`, `associates`, `colleagues`, `team members`, `personnel`, `headcount`). "
        "`grep` for `Human Capital|full-time|equivalent employees|associates|colleagues|Item 6\\.?D|headcount` "
        "and read the full workforce paragraph(s) for `total_employees` (integer), `breakdown.us`, `breakdown.international`, and `as_of`.\n"
        "4. **Executive Officers / Leadership**: `grep` for `Information about our Executive Officers|Executive Officers|Directors and Senior Management|Item 6\\.?A|SIGNATURES` "
        "and read the Executive Officers / Senior Management table (with ages & titles). If incorporated by reference to a Proxy Statement, read the `SIGNATURES` block at the end of the filing to capture CEO, CFO, PAO, and Chair/Directors.\n"
        "5. **Independent Auditor**: `grep` for `Report of Independent Registered Public Accounting Firm|Deloitte|Ernst & Young|PricewaterhouseCoopers|KPMG|BDO|Grant Thornton|served as the Company` "
        "and read the signature lines at the bottom of the audit report to get the exact firm name.\n"
        "6. **Properties / Facilities**: `grep` for `Item 2\\.?\\s*Properties|Item 4\\.?D|Property, Plant|square feet|stores|branches|manufacturing facilities|headquarters` "
        "and read Item 2 (10-K) or Item 4.D (20-F) for `total_sq_ft`, `owned_sq_ft`, `leased_sq_ft`, and 2–4 concrete `highlights` (even when sq ft is not disclosed).\n"
        "7. **Social Media (Reg FD / Available Information)**: `grep` for `x\\.com|twitter\\.com|linkedin\\.com|Available Information|social media` "
        "and extract any officially disclosed company or executive `x` / `linkedin` URLs (leave `null` if not explicitly stated — NEVER guess handles).\n\n"
        "When you have verified all available sections, call `finalize_profile` with ONLY valid JSON matching:\n"
        f"{_PROFILE_SCHEMA_JSON}"
    )
    initial_message = (
        f"Extract the complete institutional company profile for {company_name} ({ticker}) "
        f"from this SEC {form_type} using `grep` and `read_lines`, then call `finalize_profile`."
    )
    finalize_desc = (
        "Call this once you have verified all profile sections in the document. "
        "Pass `result_json` as a valid JSON string matching the required company profile schema."
    )

    nudge_state = {"count": 0}

    def _validate(parsed: dict[str, Any]) -> str | None:
        if not isinstance(parsed, dict):
            return "Result must be a JSON object."
        if not parsed.get("overview") and not parsed.get("segments"):
            return "Missing both `overview` and `segments` — please read Item 1 / Item 4 Business before finalizing."

        # One-shot self-check if major sections that almost every 10-K/20-F has were left empty
        if nudge_state["count"] < 1:
            missing_hints: list[str] = []
            wf = parsed.get("workforce") or {}
            if not wf.get("total_employees"):
                missing_hints.append(
                    "`workforce.total_employees` (try `grep` for `full-time|employees|associates|colleagues|headcount|Item 6`)"
                )
            if not parsed.get("auditor"):
                missing_hints.append(
                    "`auditor` (try `grep` for `Deloitte|Ernst & Young|PricewaterhouseCoopers|KPMG|served as the Company`)"
                )
            execs = (parsed.get("leadership") or {}).get("executives") or []
            if not execs:
                missing_hints.append(
                    "`leadership.executives` (try `grep` for `Executive Officers|Senior Management|SIGNATURES`)"
                )
            segs = (parsed.get("segments") or {}).get("items") or []
            if not segs:
                missing_hints.append(
                    "`segments.items` (try `grep` for `reportable segment|operating segment|Segment`)"
                )
            fac = parsed.get("facilities") or {}
            if not fac.get("total_sq_ft") and not (fac.get("highlights") or []):
                missing_hints.append(
                    "`facilities` (try `grep` for `Item 2|Properties|Item 4\\.?D|headquarters|square feet`)"
                )
            if missing_hints:
                nudge_state["count"] += 1
                return (
                    "Self-check: before finalizing, these usually-present fields are still empty: "
                    + "; ".join(missing_hints)
                    + ". Run targeted `grep` + `read_lines` to verify whether they exist in the filing, then call `finalize_profile` again."
                )
        return None

    parsed = run_agent_loop(
        system_prompt=system_prompt,
        initial_message=initial_message,
        tools=tools,
        ticker=ticker,
        max_steps=max_steps,
        finalize_name="finalize_profile",
        finalize_description=finalize_desc,
        finalize_validator=_validate,
        chat_llm=chat_llm,
    )
    return _normalize_extracted_profile(parsed)


def extract_profile_with_llm(
    sections: dict[str, str],
    *,
    ticker: str,
    company_name: str,
    form_type: str,
    accession: str,
) -> dict[str, Any]:
    """Legacy single-shot wrapper retained for compatibility; delegates to ``extract_profile_with_agent``."""
    blob_parts = [f"=== {k.upper()} ===\n{v}" for k, v in sections.items() if v]
    combined = "\n\n".join(blob_parts)
    return extract_profile_with_agent(
        combined,
        ticker=ticker,
        company_name=company_name,
        form_type=form_type,
        accession=accession,
    )


def extract_8k_profile_delta(
    html: str,
    *,
    items: str,
    ticker: str,
    accession: str,
    chat_llm: Any = None,
) -> dict[str, Any]:
    """Extract targeted corporate-event updates from 8-K Item 5.02 (officers) or Item 4.01 (auditor).

    Uses the same ``grep`` / ``read_lines`` agent loop so even multi-exhibit 8-K filings
    are navigated cleanly without truncation.
    """
    item_tokens = {t.strip() for t in str(items or "").split(",") if t.strip()}
    if not (item_tokens & {"5.02", "4.01"}):
        return {}

    from filings_agent.agent.loop import run_agent_loop

    text = _html_to_clean_text(html)
    tools = build_profile_document_tools(text, form_type="8-K")
    system_prompt = (
        f"You are extracting corporate profile changes from an SEC Form 8-K (Items: {items}) "
        f"for {ticker} (accession {accession}).\n"
        "Use `grep` and `read_lines` to inspect Item 5.02 (executive officer appointments/departures) "
        "and/or Item 4.01 (changes in independent registered public accounting firm).\n"
        "Never guess — only report changes explicitly stated in the filing.\n"
        "Call `finalize_8k_delta` with ONLY valid JSON:\n"
        '{\n'
        '  "auditor": "new independent auditor name if Item 4.01 changed it, else null",\n'
        '  "leadership_changes": [\n'
        '    {"action": "appointed|departed", "name": "Full Name", "title": "Title", '
        '"is_ceo": false, "is_cfo": false, "effective_date": "YYYY-MM-DD or null"}\n'
        '  ]\n'
        '}'
    )
    parsed = run_agent_loop(
        system_prompt=system_prompt,
        initial_message=f"Inspect this 8-K (Items: {items}) for {ticker} and call `finalize_8k_delta`.",
        tools=tools,
        ticker=ticker,
        max_steps=8,
        finalize_name="finalize_8k_delta",
        finalize_description="Call with JSON string containing `auditor` and `leadership_changes`.",
        chat_llm=chat_llm,
    )
    return parsed if isinstance(parsed, dict) else {}


def extract_proxy_profile_with_agent(
    document_text: str,
    *,
    ticker: str,
    company_name: str,
    form_type: str = "DEF 14A",
    accession: str = "",
    max_steps: int = 14,
    chat_llm: Any = None,
) -> dict[str, Any]:
    """Extract Executive Officers (with ages/titles), Board of Directors, and Auditor from SEC DEF 14A Proxy."""
    from filings_agent.agent.loop import run_agent_loop

    tools = build_profile_document_tools(document_text, form_type=form_type)
    schema = """{
  "leadership": {
    "as_of": "YYYY-MM-DD or null",
    "executives": [
      {"name": "Full Name", "age": 58, "title": "Exact Title", "is_ceo": true, "is_cfo": false, "x": null, "linkedin": null}
    ]
  },
  "board_of_directors": {
    "as_of": "YYYY-MM-DD or null",
    "directors": [
      {"name": "Full Name", "age": 65, "role": "Chair / Lead Independent Director / Director", "independent": true, "since_year": 2015, "x": null, "linkedin": null}
    ]
  },
  "auditor": "Independent registered public accounting firm name or null"
}"""
    system_prompt = (
        f"You are an institutional governance extraction agent analyzing an SEC {form_type} Proxy Statement "
        f"for {company_name} ({ticker}, accession {accession}).\n\n"
        "Use `get_document_info`, `grep`, and `read_lines` to extract verified governance data — NEVER guess.\n"
        "Issue multiple `grep` calls in parallel:\n"
        "1. **Executive Officers**: `grep` for `Executive Officers|Named Executive Officers|Summary Compensation Table` "
        "and read the Executive Officers biographies/table to get EVERY executive officer's `name`, `age` (integer), `title`, `is_ceo`, `is_cfo`, and `as_of`.\n"
        "2. **Board of Directors**: `grep` for `Director Nominees|Election of Directors|Board of Directors|Independent|Director Since` "
        "and read the Director Nominees summary table to get EVERY director's `name`, `age`, `role`, `independent` (bool), `since_year` (integer), and `as_of`.\n"
        "3. **Independent Auditor**: `grep` for `Ratification of.*Independent|Deloitte|Ernst & Young|PricewaterhouseCoopers|KPMG` "
        "to confirm the independent registered public accounting firm.\n\n"
        "Call `finalize_proxy_profile` with ONLY valid JSON matching:\n"
        f"{schema}"
    )
    parsed = run_agent_loop(
        system_prompt=system_prompt,
        initial_message=(
            f"Extract Executive Officers (with ages), Board of Directors, and Auditor for {company_name} ({ticker}) "
            f"from this SEC {form_type} using `grep` and `read_lines`, then call `finalize_proxy_profile`."
        ),
        tools=tools,
        ticker=ticker,
        max_steps=max_steps,
        finalize_name="finalize_proxy_profile",
        finalize_description="Call with JSON string containing `leadership`, `board_of_directors`, and `auditor`.",
        chat_llm=chat_llm,
    )
    return parsed if isinstance(parsed, dict) else {}


def _merge_proxy_into_incoming(
    incoming: dict[str, Any],
    existing: Optional[dict[str, Any]],
    proxy_data: dict[str, Any],
    *,
    proxy_filed_date: str,
    annual_filed_date: str,
) -> None:
    """Merge DEF 14A leadership, board_of_directors, and auditor into *incoming* chronologically."""
    if not isinstance(proxy_data, dict):
        return

    # 1. Board of Directors (authoritative from DEF 14A)
    bod = proxy_data.get("board_of_directors")
    if isinstance(bod, dict) and bod.get("directors"):
        existing_dirs = ((existing or {}).get("board_of_directors") or {}).get("directors") or []
        bod_copy = dict(bod)
        bod_copy["directors"] = _ensure_person_social_fields(bod["directors"], existing_dirs)
        incoming["board_of_directors"] = bod_copy

    # 2. Auditor fallback if not already set from 10-K
    if proxy_data.get("auditor") and not _get_nested(incoming, "key_facts.auditor"):
        incoming.setdefault("key_facts", {})["auditor"] = str(proxy_data["auditor"]).strip()

    # 3. Leadership merge respecting filing chronology
    proxy_lead = proxy_data.get("leadership") or {}
    proxy_execs = [e for e in (proxy_lead.get("executives") or []) if isinstance(e, dict) and e.get("name")]
    if not proxy_execs:
        return

    base_lead = incoming.get("leadership") or (existing or {}).get("leadership") or {}
    base_execs = [e for e in (base_lead.get("executives") or []) if isinstance(e, dict) and e.get("name")]

    proxy_is_newer = bool(proxy_filed_date and (not annual_filed_date or proxy_filed_date >= annual_filed_date))

    def _norm_name(n: str) -> str:
        # Normalize middle initials so "Timothy D. Cook" matches "Tim Cook" / "Timothy Cook"
        tokens = [t.strip(".").lower() for t in str(n or "").split() if t.strip(".")]
        if len(tokens) >= 3:
            return f"{tokens[0]} {tokens[-1]}"
        return " ".join(tokens)

    if not base_execs:
        incoming["leadership"] = {
            "as_of": proxy_lead.get("as_of") or proxy_filed_date or None,
            "executives": proxy_execs,
        }
        return

    if proxy_is_newer:
        # Proxy is newer than 10-K: proxy list is primary; fill missing ages from 10-K if any
        base_by_norm = {_norm_name(e["name"]): e for e in base_execs}
        merged: list[dict[str, Any]] = []
        for pe in proxy_execs:
            entry = dict(pe)
            if entry.get("age") is None:
                match = base_by_norm.get(_norm_name(entry["name"]))
                if match and match.get("age") is not None:
                    entry["age"] = match["age"]
            merged.append(entry)
        incoming["leadership"] = {
            "as_of": proxy_lead.get("as_of") or proxy_filed_date or base_lead.get("as_of"),
            "executives": merged,
        }
    else:
        # 10-K is newer than Proxy: keep 10-K officers & titles authoritative, enrich ages from Proxy,
        # and append non-conflicting executive officers from Proxy (never resurrect an older CEO/CFO).
        proxy_by_norm = {_norm_name(e["name"]): e for e in proxy_execs}
        has_ceo = any(bool(e.get("is_ceo")) for e in base_execs)
        has_cfo = any(bool(e.get("is_cfo")) for e in base_execs)
        seen_norms: set[str] = set()
        merged = []
        for be in base_execs:
            entry = dict(be)
            nk = _norm_name(entry["name"])
            seen_norms.add(nk)
            if entry.get("age") is None:
                pm = proxy_by_norm.get(nk)
                if pm and pm.get("age") is not None:
                    entry["age"] = pm["age"]
            merged.append(entry)
        for pe in proxy_execs:
            nk = _norm_name(pe["name"])
            if nk in seen_norms:
                continue
            if (has_ceo and pe.get("is_ceo")) or (has_cfo and pe.get("is_cfo")):
                continue
            seen_norms.add(nk)
            merged.append(dict(pe))
        incoming["leadership"] = {
            "as_of": base_lead.get("as_of") or annual_filed_date or proxy_lead.get("as_of"),
            "executives": merged,
        }


# ── 3. Diff engine + MongoDB persistence ────────────────────────────────────

def upsert_company_profile(
    db: Any,
    cik: str,
    incoming: dict[str, Any],
    *,
    actor: str = "gotham:auto",
    trigger_form: str = "",
    accession: str = "",
    force_replace: bool = False,
) -> dict[str, Any]:
    """Merge *incoming* into ``company_profiles`` and log every change to ``company_profile_history``.

    No fields are locked — Gotham can always update any changed field regardless of
    whether it was previously written manually or by Gotham.
    """
    cik_padded = _pad_cik(cik)
    col = db["company_profiles"]
    hist_col = db["company_profile_history"]
    now = datetime.now(timezone.utc)

    existing = col.find_one({"cik": cik_padded})
    doc: dict[str, Any] = copy.deepcopy(existing) if existing else {
        "cik": cik_padded,
        "filings_used": [],
        "sources": [],
        "profile_version": 0,
        "created_at": now,
    }
    doc.pop("_id", None)
    doc.pop("manual_overrides", None)
    doc.pop("pending_auto_updates", None)

    changes: list[dict[str, Any]] = []

    for field in TRACKED_FIELDS:
        new_val = _get_nested(incoming, field)
        if not _is_meaningful(new_val):
            continue
        old_val = _get_nested(doc, field)
        if old_val == new_val:
            continue

        _set_nested(doc, field, new_val)
        changes.append({"field": field, "old_value": old_val, "new_value": new_val})

    # Merge citations metadata on shares/filings_used/sources
    for meta_path in (
        "shares.shares_citation",
        "shares.shares_accession",
        "shares.public_float_citation",
    ):
        mv = _get_nested(incoming, meta_path)
        if _is_meaningful(mv):
            _set_nested(doc, meta_path, mv)

    # Merge filings_used deduplicated by accession
    existing_filings = {
        str(f.get("accession") or f.get("url")): f
        for f in (doc.get("filings_used") or [])
        if isinstance(f, dict)
    }
    for f in incoming.get("filings_used") or []:
        if isinstance(f, dict):
            k = str(f.get("accession") or f.get("url") or "")
            if k:
                existing_filings[k] = f
    doc["filings_used"] = sorted(
        existing_filings.values(),
        key=lambda x: str(x.get("filed_date") or ""),
        reverse=True,
    )[:10]

    # Merge sources deduplicated by url
    existing_sources = {
        str(s.get("url")): s
        for s in (doc.get("sources") or [])
        if isinstance(s, dict) and s.get("url")
    }
    for s in incoming.get("sources") or []:
        if isinstance(s, dict) and s.get("url"):
            existing_sources[str(s["url"])] = s
    doc["sources"] = list(existing_sources.values())[:15]

    doc["updated_at"] = now
    if actor.startswith("gotham"):
        doc["last_auto_updated_at"] = now
    if changes or existing is None:
        doc["profile_version"] = int(doc.get("profile_version") or 0) + 1

    col.update_one(
        {"cik": cik_padded},
        {"$set": doc, "$unset": {"manual_overrides": "", "pending_auto_updates": ""}},
        upsert=True,
    )

    if changes:
        hist_col.insert_one({
            "cik": cik_padded,
            "ticker": doc.get("ticker") or "",
            "changed_at": now,
            "actor": actor,
            "trigger_form": trigger_form,
            "accession": accession,
            "profile_version": doc["profile_version"],
            "changes": changes,
        })

    return {
        "cik": cik_padded,
        "ticker": doc.get("ticker") or "",
        "profile_version": doc["profile_version"],
        "changes_count": len(changes),
        "changes": changes,
    }


# ── 4. End-to-end refresh orchestrator ───────────────────────────────────────

def _find_latest_filing_in_submissions(
    submissions: dict[str, Any], forms: set[str]
) -> Optional[dict[str, str]]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    form_list = recent.get("form") or []
    acc_list = recent.get("accessionNumber") or []
    filed_list = recent.get("filingDate") or []
    report_list = recent.get("reportDate") or []
    doc_list = recent.get("primaryDocument") or []
    for idx, form in enumerate(form_list):
        if str(form).upper() in forms:
            return {
                "form": str(form),
                "accession": str(acc_list[idx]) if idx < len(acc_list) else "",
                "filed_date": str(filed_list[idx]) if idx < len(filed_list) else "",
                "report_date": str(report_list[idx]) if idx < len(report_list) else "",
                "primary_document": str(doc_list[idx]) if idx < len(doc_list) else "",
            }
    return None


def _load_filing_html(
    cik: str,
    accession: str,
    primary_doc: str,
    *,
    html_dir: Optional[str] = None,
) -> Optional[str]:
    """Read cached HTML from ``SEC_HTML_DOWNLOAD_PATH`` if present, else fetch from SEC."""
    base_dir = html_dir or os.getenv("SEC_HTML_DOWNLOAD_PATH")
    if base_dir and accession:
        root = Path(base_dir)
        for pattern in (f"*{accession}*.htm", f"*{accession}*.html", f"*{accession}*.html.gz"):
            for match in root.rglob(pattern):
                try:
                    if match.name.endswith(".gz"):
                        return gzip.decompress(match.read_bytes()).decode("utf-8", errors="ignore")
                    return match.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    pass
    url = _sec_archive_url(cik, accession, primary_doc)
    try:
        resp = requests.get(url, headers=_SEC_HEADERS, timeout=30)
        if resp.status_code == 200:
            return resp.text
    except Exception as exc:  # noqa: BLE001
        logger.warning("company_profile: failed to fetch %s: %s", url, exc)
    return None


def _find_recent_event_8ks_after(
    submissions: dict[str, Any],
    after_date: str,
    *,
    target_items: set[str] = frozenset({"5.02", "4.01"}),
    limit: int = 3,
) -> list[dict[str, str]]:
    """Return recent 8-K / 8-K/A filings containing Item 5.02 or 4.01 filed after *after_date*."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    form_list = recent.get("form") or []
    acc_list = recent.get("accessionNumber") or []
    filed_list = recent.get("filingDate") or []
    doc_list = recent.get("primaryDocument") or []
    items_list = recent.get("items") or []
    out: list[dict[str, str]] = []
    for idx, form in enumerate(form_list):
        if str(form).upper() not in {"8-K", "8-K/A"}:
            continue
        filed = str(filed_list[idx]) if idx < len(filed_list) else ""
        if after_date and filed and filed <= after_date:
            continue
        raw_items = items_list[idx] if idx < len(items_list) else []
        if isinstance(raw_items, list):
            item_set = {str(x).strip() for x in raw_items}
            items_str = ",".join(sorted(item_set))
        else:
            items_str = str(raw_items or "")
            item_set = {x.strip() for x in items_str.split(",") if x.strip()}
        if not (item_set & target_items):
            continue
        out.append({
            "form": str(form),
            "accession": str(acc_list[idx]) if idx < len(acc_list) else "",
            "filed_date": filed,
            "primary_document": str(doc_list[idx]) if idx < len(doc_list) else "",
            "items": items_str,
        })
        if len(out) >= limit:
            break
    return out


def _apply_8k_delta_to_incoming(
    incoming: dict[str, Any],
    existing: Optional[dict[str, Any]],
    delta: dict[str, Any],
) -> None:
    """Apply extracted 8-K Item 5.02/4.01 changes onto *incoming* before atomic upsert."""
    if not isinstance(delta, dict):
        return
    if delta.get("auditor"):
        incoming.setdefault("key_facts", {})["auditor"] = str(delta["auditor"]).strip()

    l_changes = delta.get("leadership_changes") or []
    if not l_changes:
        return
    base_execs = list(
        ((incoming.get("leadership") or {}).get("executives"))
        or (((existing or {}).get("leadership") or {}).get("executives"))
        or []
    )
    by_name = {str(e.get("name") or "").strip().lower(): dict(e) for e in base_execs if e.get("name")}
    latest_date = (incoming.get("leadership") or {}).get("as_of")
    for ch in l_changes:
        if not isinstance(ch, dict) or not ch.get("name"):
            continue
        name = str(ch["name"]).strip()
        key = name.lower()
        action = str(ch.get("action") or "").strip().lower()
        if ch.get("effective_date"):
            latest_date = str(ch["effective_date"])
        if action == "departed":
            by_name.pop(key, None)
        elif action == "appointed":
            by_name[key] = {
                "name": name,
                "age": ch.get("age"),
                "title": str(ch.get("title") or "").strip(),
                "is_ceo": bool(ch.get("is_ceo")),
                "is_cfo": bool(ch.get("is_cfo")),
            }
    incoming["leadership"] = {
        "as_of": latest_date,
        "executives": list(by_name.values()),
    }


def refresh_company_profile(
    cik: str,
    *,
    db: Any = None,
    mode: str = "upsert",
    include_llm: bool = True,
    html_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Atomically build/update ``company_profiles`` from ALL applicable SEC sources.

    Auto-detects US issuers (``10-K`` + ``10-Q`` + ``8-K`` Item 5.02/4.01) vs.
    Foreign Private Issuers (``20-F`` + ``6-K``) from SEC submissions, combines
    deterministic metadata + DEI shares + annual 10-K/20-F sections + newer 8-K
    corporate-event deltas, and writes atomically in a single upsert.
    """
    from shared.run_report import get_normalize_db

    cik_padded = _pad_cik(cik)
    target_db = db if db is not None else get_normalize_db()

    sub_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    resp = requests.get(sub_url, headers=_SEC_HEADERS, timeout=20)
    resp.raise_for_status()
    submissions = resp.json()

    dei_facts = extract_latest_dei_from_companyfacts(cik_padded)
    incoming = extract_deterministic_profile(cik_padded, submissions, dei_facts=dei_facts)

    latest_10k = _find_latest_filing_in_submissions(submissions, {"10-K", "10-K/A"})
    latest_20f = _find_latest_filing_in_submissions(submissions, {"20-F", "20-F/A"})
    is_foreign = bool(latest_20f and not latest_10k)
    incoming["issuer_type"] = "FOREIGN" if is_foreign else "US"

    latest_annual = latest_20f if is_foreign else latest_10k
    latest_proxy = (
        None
        if is_foreign
        else _find_latest_filing_in_submissions(submissions, {"DEF 14A", "DEF 14A/A"})
    )
    latest_interim = (
        _find_latest_filing_in_submissions(submissions, {"6-K", "6-K/A"})
        if is_foreign
        else _find_latest_filing_in_submissions(submissions, {"10-Q", "10-Q/A"})
    )
    event_8ks = (
        []
        if is_foreign
        else _find_recent_event_8ks_after(
            submissions, (latest_annual or {}).get("filed_date") or ""
        )
    )

    filings_used: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = list(incoming.get("sources") or [])
    for entry in [latest_annual, latest_proxy, latest_interim, *event_8ks]:
        if entry and entry.get("accession"):
            f_url = _sec_archive_url(cik_padded, entry["accession"], entry.get("primary_document", ""))
            filings_used.append({
                "form": entry["form"],
                "period": entry.get("report_date") or "",
                "filed_date": entry.get("filed_date") or "",
                "accession": entry["accession"],
                "url": f_url,
            })
            sources.append({
                "label": f"{incoming.get('company_name') or incoming.get('ticker')} {entry['form']} ({entry.get('report_date') or entry.get('filed_date')})",
                "url": f_url,
            })
    incoming["filings_used"] = filings_used
    incoming["sources"] = sources

    existing = target_db["company_profiles"].find_one({"cik": cik_padded})
    existing_annual_acc = (existing or {}).get("_last_llm_annual_accession")
    existing_proxy_acc = (existing or {}).get("_last_llm_proxy_accession")
    seen_event_accs = set((existing or {}).get("_processed_event_accessions") or [])
    trigger_form = (latest_annual or {}).get("form") or "SUBMISSIONS"
    trigger_acc = (latest_annual or {}).get("accession") or ""
    proxy_acc = (latest_proxy or {}).get("accession") or ""

    should_run_llm = (
        include_llm
        and latest_annual is not None
        and (
            mode in ("upsert", "replace")
            or existing is None
            or existing_annual_acc != trigger_acc
        )
    )
    if should_run_llm and latest_annual:
        html = _load_filing_html(
            cik_padded,
            latest_annual["accession"],
            latest_annual.get("primary_document", ""),
            html_dir=html_dir,
        )
        if html:
            clean_text = _html_to_clean_text(html)
            if clean_text:
                llm_data = extract_profile_with_agent(
                    clean_text,
                    ticker=incoming.get("ticker") or cik_padded,
                    company_name=incoming.get("company_name") or "",
                    form_type=latest_annual["form"],
                    accession=latest_annual["accession"],
                )
                for k, v in llm_data.items():
                    if k == "key_facts" and isinstance(v, dict):
                        incoming.setdefault("key_facts", {}).update(v)
                    elif k == "shares" and isinstance(v, dict):
                        # Only fill shares from filing text when SEC DEI companyfacts didn't provide it
                        sh = incoming.setdefault("shares", {})
                        if sh.get("shares_outstanding") is None and v.get("shares_outstanding") is not None:
                            sh["shares_outstanding"] = int(v["shares_outstanding"])
                            if v.get("shares_as_of"):
                                sh["shares_as_of"] = str(v["shares_as_of"])
                            sh["shares_citation"] = f"{latest_annual['form']} cover"
                            sh["shares_accession"] = latest_annual["accession"]
                    else:
                        incoming[k] = v

    # Always extract DEF 14A (Proxy Statement) for US issuers — Executive Officers (with ages) + Board of Directors
    should_run_proxy = (
        include_llm
        and latest_proxy is not None
        and bool(proxy_acc)
        and (
            mode in ("upsert", "replace")
            or existing is None
            or existing_proxy_acc != proxy_acc
        )
    )
    if should_run_proxy and latest_proxy:
        proxy_html = _load_filing_html(
            cik_padded,
            latest_proxy["accession"],
            latest_proxy.get("primary_document", ""),
            html_dir=html_dir,
        )
        if proxy_html:
            proxy_text = _html_to_clean_text(proxy_html)
            if proxy_text:
                try:
                    proxy_data = extract_proxy_profile_with_agent(
                        proxy_text,
                        ticker=incoming.get("ticker") or cik_padded,
                        company_name=incoming.get("company_name") or "",
                        form_type=latest_proxy["form"],
                        accession=latest_proxy["accession"],
                    )
                    if proxy_data:
                        _merge_proxy_into_incoming(
                            incoming,
                            existing,
                            proxy_data,
                            proxy_filed_date=latest_proxy.get("filed_date") or "",
                            annual_filed_date=(latest_annual or {}).get("filed_date") or "",
                        )
                        trigger_form = f"{trigger_form}+DEF 14A" if trigger_form else "DEF 14A"
                except Exception as exc:  # noqa: BLE001
                    logger.debug("company_profile: DEF 14A extraction failed for %s (%s): %s", cik_padded, proxy_acc, exc)

    # Apply any newer 8-K Item 5.02 / 4.01 corporate-event deltas chronologically
    newly_processed_events: list[str] = []
    if include_llm and event_8ks:
        for ev in reversed(event_8ks):
            ev_acc = ev.get("accession") or ""
            if not ev_acc:
                continue
            if mode == "default" and ev_acc in seen_event_accs:
                continue
            ev_html = _load_filing_html(
                cik_padded, ev_acc, ev.get("primary_document", ""), html_dir=html_dir
            )
            if ev_html:
                try:
                    delta = extract_8k_profile_delta(
                        ev_html,
                        items=ev.get("items") or "",
                        ticker=incoming.get("ticker") or cik_padded,
                        accession=ev_acc,
                    )
                    if delta:
                        _apply_8k_delta_to_incoming(incoming, existing, delta)
                        trigger_form = f"{trigger_form}+8-K" if trigger_form else "8-K"
                    newly_processed_events.append(ev_acc)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("company_profile: 8-K delta failed for %s (%s): %s", cik_padded, ev_acc, exc)

    # Enrich company-level social_media deterministically from official corporate website
    effective_website = incoming.get("website") or (existing or {}).get("website")
    web_socials = extract_company_social_media_from_website(effective_website)
    if web_socials:
        sm = dict(incoming.get("social_media") or {})
        for k, v in web_socials.items():
            if not sm.get(k):
                sm[k] = v
        incoming["social_media"] = sm

    # Preserve any existing non-null social links on executives when incoming has null
    if isinstance(incoming.get("leadership"), dict) and isinstance(incoming["leadership"].get("executives"), list):
        existing_execs = ((existing or {}).get("leadership") or {}).get("executives") or []
        incoming["leadership"]["executives"] = _ensure_person_social_fields(
            incoming["leadership"]["executives"], existing_execs
        )

    # Single atomic write to company_profiles + company_profile_history
    summary = upsert_company_profile(
        target_db,
        cik_padded,
        incoming,
        actor="gotham:auto",
        trigger_form=trigger_form,
        accession=trigger_acc,
        force_replace=(mode == "replace"),
    )
    meta_updates: dict[str, Any] = {"issuer_type": incoming["issuer_type"]}
    if should_run_llm and trigger_acc:
        meta_updates["_last_llm_annual_accession"] = trigger_acc
    if should_run_proxy and proxy_acc:
        meta_updates["_last_llm_proxy_accession"] = proxy_acc
    if newly_processed_events:
        meta_updates["_processed_event_accessions"] = sorted(seen_event_accs | set(newly_processed_events))[-20:]
    if meta_updates:
        target_db["company_profiles"].update_one(
            {"cik": cik_padded},
            {"$set": meta_updates},
        )
    summary["issuer_type"] = incoming["issuer_type"]
    return summary
