"""Canonical row identity for hierarchy resolution and persistence.

A row is **never** identified by its concept name alone.

The same dimension member hangs under different line items and means different
things in each: ``dhi:HomeBuildingOpsMember`` is a child of ``us-gaap:Revenues``
in one row and of ``us-gaap:CostOfRevenue`` in another (verified in production
data, CIK 0000882184). Identical member name, different economic fact. Merging,
de-duplicating, re-pathing or aliasing across that boundary corrupts the
statement, so every one of those operations is scoped by parent here.

Identity::

    (cik, form_type, statement_type, period, parent_id, dimension_signature)

* ``parent_id`` is the ``concept_id`` of the line item the row hangs under — the
  parent *row*, not a name, so a rename upstream cannot silently re-point it.
* ``dimension_signature`` is an order-independent canonicalisation of the
  dimensional slice (``axis=member`` pairs, sorted). It replaces the old
  ``f"{member}_{other_member}"`` concept-name hack, which lost the axis, varied
  with dict iteration order (435 order-reversed twins in production) and could
  not distinguish two rows that share a member but differ in line item.

This module is dependency-free so both the agent hierarchy code and the
normalization service can import it.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

# Axes that only flag structural membership (which statement/segment a row
# belongs to) rather than carrying a real business breakdown. They must not
# take part in the identity — two rows differing only by one of these are the
# same row. Tokens are in normalize_axis() form (namespace and ``Axis`` suffix
# already stripped), which is what the comparison sees.
WRAPPER_AXES: frozenset[str] = frozenset(
    {
        "consolidationitems",
        "consolidation",
        "segmentreporting",
        "legalentity",
        "statementgeographical",
        "explicitmember",  # XBRL-DI internal, not an axis at all
    }
)

SEPARATOR = "|"


def normalize_axis(axis: Any) -> str:
    """``us-gaap:StatementBusinessSegmentsAxis`` → ``statementbusinesssegments``.

    Namespace and the ``Axis`` suffix are dropped; case and separators are
    folded so the same axis always produces the same token. An axis whose local
    name *is* ``Axis`` (or is otherwise emptied by the suffix strip) keeps its
    local name rather than collapsing to nothing.
    """
    text = str(axis or "").strip()
    if not text:
        return ""
    local = text.rsplit(":", 1)[-1]
    stripped = local[:-4] if local.lower().endswith("axis") else local
    token = "".join(ch for ch in stripped.lower() if ch.isalnum())
    if not token:
        # Axis whose local name is bare "Axis": keep the whole qualified name so
        # two such axes stay distinguishable instead of both collapsing to one.
        token = "".join(ch for ch in text.lower() if ch.isalnum())
    return token


def normalize_member(member: Any) -> str:
    """Canonical token for a member, namespace preserved.

    The namespace matters — ``meta:FamilyOfAppsMember`` and a same-named member
    in another filer's namespace are different members — but case is folded.
    """
    text = str(member or "").strip()
    return text.lower()


def _iter_dimension_pairs(
    dimensions: Optional[Mapping[str, Any]],
    dimension_details: Optional[Mapping[str, Any]] = None,
) -> Iterable[tuple[str, str]]:
    """Yield ``(axis_token, member_token)`` pairs from either representation.

    ``dimensions`` is the flat ``{axis: member}`` map. ``dimension_details`` is
    the richer ``{axis: {member_qname, axis_local_name, ...}}`` map and is used
    to recover the qualified member when the flat map only carries a local name.
    """
    if isinstance(dimensions, Mapping):
        for axis, member in dimensions.items():
            axis_token = normalize_axis(axis)
            if not axis_token or axis_token in WRAPPER_AXES:
                continue
            if isinstance(member, Mapping):
                member = (
                    member.get("member_qname")
                    or member.get("member_local_name")
                    or member.get("member")
                )
            member_token = normalize_member(member)
            if member_token:
                yield axis_token, member_token

    if isinstance(dimension_details, Mapping):
        for axis, details in dimension_details.items():
            axis_token = normalize_axis(axis)
            if not axis_token or axis_token in WRAPPER_AXES:
                continue
            if not isinstance(details, Mapping):
                continue
            member_token = normalize_member(
                details.get("member_qname") or details.get("member_local_name")
            )
            if member_token:
                yield axis_token, member_token


def dimension_signature(
    dimensions: Optional[Mapping[str, Any]] = None,
    dimension_details: Optional[Mapping[str, Any]] = None,
) -> str:
    """Order-independent signature of a row's dimensional slice.

    ``""`` for a non-dimensional row. Two rows with the same signature slice the
    same members over the same axes, regardless of the order the axes were
    parsed or the order a dict happened to iterate in.

    When both representations describe the same axis, ``dimension_details``
    wins: it carries the namespace-qualified member, while the flat map often
    only has the unqualified local name. Emitting both would make one axis look
    like two and change the identity of every row that has both maps.
    """
    by_axis: dict[str, str] = {}
    for axis, member in _iter_dimension_pairs(dimensions, None):
        by_axis.setdefault(axis, member)
    for axis, member in _iter_dimension_pairs(None, dimension_details):
        by_axis[axis] = member  # qualified member overrides the local-only one
    if not by_axis:
        return ""
    return SEPARATOR.join(f"{axis}={member}" for axis, member in sorted(by_axis.items()))


def parent_anchor(row: Mapping[str, Any]) -> tuple[Optional[Any], Optional[str]]:
    """Return ``(parent_id, parent_concept)`` for a row.

    ``parent_id`` is the ``concept_id`` of the line item the row hangs under —
    the authoritative link. ``parent_concept`` is the parent's concept *name*
    (``concept_name`` on the row) and is used for display and for alias scoping.
    Both are ``None`` for a top-level row.
    """
    parent_id = row.get("parent_id") or row.get("concept_id")
    parent_concept = row.get("parent_concept") or row.get("concept_name")
    if parent_id is None and not parent_concept:
        return None, None
    return parent_id, (str(parent_concept) if parent_concept else None)


def is_dimensional(row: Mapping[str, Any]) -> bool:
    """True when the row is a dimensional slice rather than a line item."""
    if row.get("dimension_concept"):
        return True
    if dimension_signature(row.get("dimensions"), row.get("dimension_details")):
        return True
    # A dimensional row always has a parent anchor; a root line item does not.
    parent_id, _ = parent_anchor(row)
    return parent_id is not None and bool(row.get("concept_name"))


def _parent_component(row: Mapping[str, Any]) -> str:
    """The parent half of the identity.

    The parent *row id* is authoritative. When a caller has only the parent's
    concept name (unknown-parent filings, tests, pre-backfill rows), the name is
    used instead so two rows under different line items still separate.
    """
    parent_id, parent_concept = parent_anchor(row)
    if parent_id is not None:
        return str(parent_id)
    return f"name:{parent_concept}" if parent_concept else ""


def node_component(row: Mapping[str, Any]) -> str:
    """The half of the identity that names *which* node this row is.

    A dimensional row is identified by its slice signature, which already
    encodes every ``axis=member`` pair (so it distinguishes siblings and is
    unaffected by member renames). When the dimensional metadata is missing —
    legacy rows — the member name is used instead, because falling back to an
    empty string would collapse every such row under one parent onto a single
    identity.

    A non-dimensional row has no parent and no slice, so the concept name *is*
    its node: without it every line item in a statement+period would share one
    identity and a unique index on the key could never be created.
    """
    signature = dimension_signature(
        row.get("dimensions"), row.get("dimension_details")
    )
    if signature:
        return signature
    return str(row.get("concept") or "")


def row_key(
    row: Mapping[str, Any],
    *,
    cik: Optional[str] = None,
    form_type: Optional[str] = None,
    statement_type: Optional[str] = None,
    period: Optional[str] = None,
) -> tuple:
    """Canonical identity tuple for a row.

    Explicit keyword arguments win over values found on the row, so callers can
    compute a key for a row that does not yet carry its filing context.
    """
    return (
        str(cik if cik is not None else row.get("cik") or ""),
        str(form_type if form_type is not None else row.get("form_type") or ""),
        str(
            statement_type
            if statement_type is not None
            else row.get("statement_type") or ""
        ),
        str(period if period is not None else row.get("period") or ""),
        _parent_component(row),
        node_component(row),
    )


def row_key_str(row: Mapping[str, Any], **kwargs: Any) -> str:
    """Flattened, indexable form of :func:`row_key` (``SEPARATOR``-joined)."""
    return SEPARATOR.join(row_key(row, **kwargs))


def same_parent(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """True when two rows hang under the same parent row.

    The guard for every merge/alias/dedup decision: two rows that share a
    concept name but not a parent must never be combined.
    """
    left_id, left_name = parent_anchor(left)
    right_id, right_name = parent_anchor(right)
    if left_id is not None and right_id is not None:
        return str(left_id) == str(right_id)
    return bool(left_name) and left_name == right_name


def parent_scoped_concept(row: Mapping[str, Any]) -> tuple[Optional[str], str]:
    """``(parent_concept, concept)`` — the key an alias must be scoped by.

    ``us-gaap:Revenues`` + ``dhi:HomeBuildingOpsMember`` and
    ``us-gaap:CostOfRevenue`` + ``dhi:HomeBuildingOpsMember`` are distinct keys.
    """
    _, parent_concept = parent_anchor(row)
    return parent_concept, str(row.get("concept") or "")
