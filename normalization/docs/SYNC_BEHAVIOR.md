# Sync Behavior

This document describes the sync behavior of the financial data normalization service during reprocessing scenarios.

## Overview

The normalization service is designed to be safely re-run on already-processed data without causing duplication. It achieves this through distinct handling of **concepts** and **values**.

## Concepts

Concepts represent the structure (labels, hierarchy paths, segment types) of financial line items. When reprocessing:

- The service checks whether a concept already exists in the database before creating a new one.
- If a matching concept is found, it is **reused as-is** — its `_id` is returned directly.
- A new concept document is only inserted when no match is found.

This means concepts act as a stable registry: they are never duplicated on reprocessing.

## Values

Values represent the actual numeric data reported in a filing. When reprocessing:

- The service checks for an existing value record matching the `(concept_id, fiscal_year, period_date, accession_number)` combination.
- If a matching value exists, the insert is **skipped** — no duplicate is created.
- If no match is found, the value is **inserted** as a new record.

## Reprocessing Scenario

When all values are deleted but concepts remain (e.g., for a data refresh):

1. On reprocessing, the service encounters existing concept records.
2. It will **keep those concepts as-is** — no new concept documents are created.
3. Only the missing values are inserted anew.

This ensures idempotent operation across full and partial reprocessing runs.

## Dimensional Concepts

Dimensional concepts (segments, products, geographies, etc.) follow the same pattern:

- The service checks for an existing dimensional concept by `(parent_concept_id, segment_type, concept_name)`.
- Existing dimensional concepts are **reused**; new ones are only created when absent.
