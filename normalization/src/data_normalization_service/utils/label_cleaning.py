"""Clean human-readable labels before they are persisted.

XBRL labels are copied verbatim from the taxonomy label linkbase, which appends
a bracketed element-type tag to every standard label, for example::

    "Rest Of World [Member]"
    "Rest of Asia Pacific Segment [Member]"
    "Revenue [Text Block]"

Those tags (and the redundant, generic ``Segment`` word that members often
carry in front of ``[Member]``) are presentation noise for an end user.  Every
label that reaches the target database must therefore pass through
:func:`clean_label` first.

Rules
-----
1. Collapse whitespace and trim.
2. Drop a trailing bracketed XBRL tag when its content is a known label
   role/element type (``Member``, ``Axis``, ``Domain``, ``Text Block`` ...).
   Unknown bracketed suffixes are left untouched so genuine content (e.g. a
   date) is never lost.
3. When a ``[Member]``/``[Domain]`` tag was removed, also drop a redundant
   trailing ``Segment``/``Segments`` word -- ``"Rest of Asia Pacific Segment
   [Member]"`` becomes ``"Rest of Asia Pacific"``.  A single generic descriptor
   is preserved so ``"Operating Segments [Member]"`` never collapses to
   ``"Operating"``.

The function is idempotent: ``clean_label(clean_label(x)) == clean_label(x)``.
"""

from __future__ import annotations

import re
from typing import Optional

# Bracketed suffixes emitted by the XBRL label linkbase.  Compared
# case-insensitively against the content of a trailing ``[...]`` group.
_XBRL_LABEL_TAGS = frozenset(
    {
        "member",
        "axis",
        "domain",
        "line items",
        "line item",
        "table",
        "abstract",
        "text block",
        "policy text block",
        "deprecated",
        "roll up",
        "roll down",
        "roll forward",
        "roll forward rules",
        "details",
        "hierarchy",
        "alternative label",
        "preferred label",
        "documentation",
        "calculation",
        "presentation",
        "definition",
    }
)

# Tags whose removal makes a trailing generic "Segment(s)" redundant.
_MEMBER_TAGS = frozenset({"member", "domain"})

# A single remainder word from this set is too generic to stand on its own, so
# the trailing "Segment(s)" is kept ("Operating Segments [Member]").
_GENERIC_SEGMENT_BASE = frozenset(
    {
        "operating",
        "reportable",
        "all",
        "other",
        "total",
        "combined",
        "business",
        "geographic",
        "geographical",
        "product",
        "products",
        "service",
        "services",
        "customer",
        "customers",
        "industry",
        "consolidated",
        "segment",
    }
)

_TRAILING_TAG_RE = re.compile(r"\s*\[([^\[\]]*)\]\s*$")
_SEGMENT_SUFFIX_RE = re.compile(r"[\s,]+segments?$", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def clean_label(label: Optional[str]) -> Optional[str]:
    """Return ``label`` with XBRL presentation noise removed.

    Args:
        label: Raw label, typically straight from an Arelle ``concept.label()``
            call (e.g. ``"Rest Of World [Member]"``).  ``None`` and non-string
            values are handled gracefully.

    Returns:
        The cleaned label.  ``None`` in, ``None`` out.  When cleaning would
        leave nothing behind (e.g. the label was only ``"[Member]"``) the
        whitespace-normalised original is returned so callers never persist an
        empty string.
    """
    if label is None:
        return None

    original = _WHITESPACE_RE.sub(" ", str(label)).strip()
    if not original:
        return original

    cleaned = original
    member_tag_removed = False

    match = _TRAILING_TAG_RE.search(cleaned)
    if match:
        tag = match.group(1).strip().lower()
        if tag in _XBRL_LABEL_TAGS:
            member_tag_removed = tag in _MEMBER_TAGS
            cleaned = cleaned[: match.start()].strip()

    if member_tag_removed and cleaned:
        segment_match = _SEGMENT_SUFFIX_RE.search(cleaned)
        if segment_match:
            base = cleaned[: segment_match.start()].strip()
            # Only drop "Segment(s)" when a meaningful name remains.
            if base and base.lower() not in _GENERIC_SEGMENT_BASE:
                cleaned = base

    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned or original


def clean_labels_in_item(item: dict) -> dict:
    """Clean the ``label`` on a line-item/dimensional-fact dict in place.

    Also cleans the nested ``fact_label`` and the ``member_label`` values held
    in ``dimension_details`` (the source of dimensional concept labels).
    """
    if not isinstance(item, dict):
        return item

    if item.get("label"):
        item["label"] = clean_label(item["label"])
    if item.get("fact_label"):
        item["fact_label"] = clean_label(item["fact_label"])

    details = item.get("dimension_details")
    if isinstance(details, dict):
        for detail in details.values():
            if isinstance(detail, dict) and detail.get("member_label"):
                detail["member_label"] = clean_label(detail["member_label"])

    return item
