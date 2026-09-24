"""Row identity for the hierarchy layer — re-exported from the normalization core.

The implementation lives in
:mod:`data_normalization_service.core.row_identity` because the *writer*
(``persist_statement_bundle``) has to stamp the same key the hierarchy layer
reads. ``data_normalization_service`` is a one-way dependency of this package,
so the shared definition has to live there; this module keeps the hierarchy-side
import path stable::

    from filings_agent.hierarchy.identity import row_key_str, same_parent

See the implementation module for the rationale (parent-scoped identity, why a
concept name alone can never identify a row, and the production defects —
order-reversed composite concepts, Revenue/CostOfRevenue member collisions — that
motivated it).
"""
from __future__ import annotations

from data_normalization_service.core.row_identity import (  # noqa: F401
    SEPARATOR,
    WRAPPER_AXES,
    dimension_signature,
    is_dimensional,
    normalize_axis,
    normalize_member,
    node_component,
    parent_anchor,
    parent_scoped_concept,
    row_key,
    row_key_str,
    same_parent,
)

__all__ = [
    "SEPARATOR",
    "WRAPPER_AXES",
    "dimension_signature",
    "is_dimensional",
    "normalize_axis",
    "normalize_member",
    "node_component",
    "parent_anchor",
    "parent_scoped_concept",
    "row_key",
    "row_key_str",
    "same_parent",
]
