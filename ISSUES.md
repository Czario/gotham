# AAPL Pipeline Audit — Issue List

Read-only audit of the 10-K/10-Q pipeline using:

- the AAPL run log (`67` filings listed, `59 new, 8 failed`);
- `normalize_data` MongoDB for CIK `0000320193`
  (`normalized_concepts_{annual,quarterly}`, `concept_values_{annual,quarterly}`,
  `validation_reports`, `concept_aliases`);
- re-running the extractor against the failing filings to separate transient
  failures from permanent ones;
- code review of `filings_agent/` and
  `normalization/src/data_normalization_service/`.

No code was changed for this document.

---

## Severity summary

| # | Area | Issue | Severity |
|---|------|-------|----------|
| H1 | Hierarchy | Revenue and pre-tax income stored at path `555` (auxiliary bucket) → income statement has no revenue row | **Critical** |
| H2 | Hierarchy | Custom segmentation headers left orphaned at `001.001…` when their parent (`001` = revenue) disappeared | **Critical** |
| H3 | Hierarchy | 38 (annual) / 20 (quarterly) dimensional rows have no parent and collide with modern members on the same path | High |
| H4 | Hierarchy | Duplicate `(path, order_key)` triplets among dimensional rows (7–9 per statement) | High |
| H5 | Hierarchy | Resolver path/order changes for **existing** concepts are never persisted → tree cannot self-heal | **Critical** |
| H6 | Hierarchy | Agent may place *any* row (including parents) at `555`; no invariant prevents orphaning; tree changes on every filing | **Critical** |
| H7 | Hierarchy | Dimensional members are invisible to the hierarchy agent, so it cannot fix or reason about them | High |
| H8 | Hierarchy | Junk grouping headers (`custom:BusinessSegmentSegmentation`, `custom:SegmentreportinginformationbysegmentSegmentation`) | Medium |
| H9 | Data | Irrelevant dimensional members leak into the income statement (FX contracts, AOCI reclassification) | Medium |
| D1 | Data | 15 quarterly values reference an **annual-only** concept (`PaymentsToAcquireProductiveAssets`) → orphan values | High |
| D2 | Data | Concept aliasing leaves split series (capex, cash); same economic line across two concepts | High |
| D3 | Data | `duplicate_concept` finding on cashflow for **every** filing; unique index can silently drop a fact | High |
| D4 | Data | "Concepts with no number in any period" = the abstract grouping headers, plus rows stranded at `555` | Medium |
| F1 | Failures | 7 of 8 "failed" filings parse successfully on re-run → transient SEC/network failures are treated as permanent | High |
| F2 | Failures | 1 filing (`0000320193-20-000062`) is falsely classified as "no XBRL" and skipped forever | High |
| F3 | Failures | SEC `503 Service Unavailable` retries block ingestion for minutes | Low |
| V1 | Validation | `math_mismatch` on `GrossProfit` blocked FY2023 Q1 (write skipped) | Medium |
| V2 | Validation | `60` validation reports finalized but orphan warnings appear on most filings and are never acted on | Medium |

---

## H1 — Revenue and pre-tax income are stored at path `555`

**Evidence** (current DB):

```
normalized_concepts_annual / income
  555 b  us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax   (a=15 values)
  555 c  us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest (a=15)

normalized_concepts_quarterly / income
  555 b  us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax   (q=44 values)
  555 c  us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest (q=44)
```

Both rows have `hierarchy_repair_reason = "agent_hierarchy:agent"` and
`hide = False`.

**Impact**

- The income statement has **no revenue line** — the single most important row
  is missing from the tree.
- `555` is documented as the "non-relevant / supplementary" bucket
  (`filings_agent/agent/prompts.py`, `apply_hierarchy_updates`), so consumers
  that filter `path == 555` hide revenue entirely.
- Pre-tax income is likewise missing.

**Why it happens**

1. The hierarchy prompt instructs the agent to put "non-relevant" concepts at
   `555` (`filings_agent/agent/prompts.py:209`).
2. `plan_hierarchy` accepts `path: "555"` for any row and does not check that
   the row has children (`filings_agent/hierarchy/planner.py:164`).
3. `_apply_plan` queues a `stored_updates` entry moving the row to `555`
   (`filings_agent/nodes/hierarchy_review.py:225`).
4. `apply_hierarchy_updates` writes `path=555` and forces `hide=False`
   (`normalization/.../normalization_service.py:803`).

---

## H2 — Custom segmentation headers are orphaned

**Evidence:**

```
ANNUAL   income orphan paths: 001.001, 001.002, 001.003, 007.001
QUARTERLY income orphan paths: 001.001, 001.002, 001.003, 001.004, 001.005, 001.006, 007.001
```

`custom:ProductSegmentation` (`001.001`), `custom:GeographicSegmentation`
(`001.002`), `custom:BusinessSegmentSegmentation` (`001.003`) all expect a
parent row `001` (revenue). Because revenue was moved to `555`, `001` no longer
exists → every header and its members are detached from the tree.

**Impact**

- Rendering walks parents; orphan paths either disappear or attach to the wrong
  node.
- This is the direct cause of the log warnings
  `hierarchy ✓ … ⚠ 1 / 2 / 3 / 18 / 22 / 23 orphans`.

---

## H3 — Legacy dimensional rows have no parent and collide on paths

**Evidence (annual income):** 38 dimensional rows have `parent_concept` empty.
Examples (all sharing one path with a modern member):

```
001.001.001 a  parent=us-gaap:RevenueFromContractWithCustomer... aapl:MacMember
001.001.001 aa parent=(none)                                     aapl:ServicesMember
001.001.001 ab parent=(none)                                     aapl:ItunesSoftwareAndServiceMember
001.002.002 b  parent=us-gaap:RevenueFromContractWithCustomer... country:US
001.002.002 ba parent=(none)                                     us-gaap:EuropeMember
```

`aapl:AmericasMember`, `aapl:EuropeMember`, `country:JP`, `us-gaap:EuropeMember`,
`aapl:ServicesMember`, `aapl:ItunesSoftwareAndServiceMember`, … are Apple's
pre-2018 member tags. Their `concept_id` still points at a parent row, but
`parent_concept` is empty, so the hierarchy layer cannot place them.

**Impact**

- Multiple distinct concepts share one materialised path (14–23 paths per
  income statement), which breaks "path identifies one row".
- They render as duplicate/blank rows.

---

## H4 — Duplicate `(path, order_key)` among dimensional rows

**Evidence:**

```
ANNUAL   income dupTriplets=7   (e.g. 002.002 b = aapl:JapanSegmentMember AND us-gaap:ProductMember)
QUARTERLY income dupTriplets=9  (e.g. 005.001 a = aapl:AmericasSegmentMember AND aapl:AmericasMember)
ANNUAL   cashflow dupTriplets=2 (e.g. 002.005.001 a = aapl:RetailMember AND aapl:RetailSegmentMember)
```

**Root cause:** the resolver's integrity check
(`resolver.py` `plan["integrity"]`) only counts non-dimensional rows; the agent
plan (`plan_hierarchy`) keys siblings by `parent_concept` name and the dim-sync
groups by parent, so two rows under the same parent can receive the same
`(path, order_key)` unnoticed.

**Impact:** non-deterministic ordering; the DB index
`idx_cik_stmt_path (cik, statement_type, path, order_key)` is **not unique**, so
both rows persist.

---

## H5 — Resolver path changes for existing concepts are never persisted

**Evidence (code):**

- `resolve_hierarchy_bundles` computes new `path`/`order_key` for every row
  (`filings_agent/hierarchy/resolver.py`) and puts them on the bundle items.
- `persist_statement_bundle._promote` copies those paths onto the concept item.
- `_get_or_create_concept` **reuses the existing row and ignores `path`** for
  existing concepts (`normalization/.../normalization_service.py:930`); only
  `_create_concept` (new rows) uses it.
- The resolver only queues `existing_updates` for *duplicate* stored
  `(path, order_key)` pairs, not for ordinary re-placements.

**Impact**

- The deterministic resolver can never repair an existing bad tree; only the
  LLM agent's `stored_updates` change a stored concept's path.
- Combined with H6 this makes the hierarchy permanently dependent on the LLM.

---

## H6 — No invariant: any row can be hidden/moved to `555`, orphaning children

- `plan_hierarchy` accepts `path="555"` for a row that is the parent of other
  rows (`planner.py:164`, `planner.py:355`).
- It only validates the parent of a row with an **explicit** path; a parent that
  moves to `555` while its **stored** children stay behind is never checked
  (`planner.py:264-269`).
- `_apply_plan` queues a `stored_updates` entry for the moved parent but not for
  its stored children (`hierarchy_review.py:225-235`).
- `_apply_plan`'s guard only protects `custom:` headers
  (`hierarchy_review.py:221`), not ordinary parents such as revenue.
- Result: `revenue → 555`, headers stay at `001.001` (H1/H2).

**Additionally:** because the agent decides "relevance" per filing, the tree
drifts run-to-run. Log shows very different placement counts between filings of
the same statement (`15`, `27`, `47`, `65`, `84` placed), and `reused` runs still
re-invoke the agent (`HIERARCHY_AGENT_ALWAYS=1`).

---

## H7 — Dimensional members are invisible to the hierarchy agent

- `_stored_rows()` queries `dimension_concept: False` only
  (`filings_agent/nodes/hierarchy_review.py:130`).
- `_filing_concepts()` iterates `bundle.concepts` + `bundle.abstract_concepts`
  only — never `bundle.dimensional_concepts`
  (`filings_agent/nodes/hierarchy_review.py:169`).
- `build_hierarchy_tools` therefore shows the agent no segment members; it
  cannot place or repair them.

**Impact:** the agent's "complete hierarchy" is structurally incomplete; dims
are placed afterwards by heuristic code (`_apply_plan`), which is where H3/H4
come from.

---

## H8 — Junk `custom:` grouping headers

**Evidence:**

```
custom:BusinessSegmentSegmentation
custom:SegmentreportinginformationbysegmentSegmentation
```

Produced by `_classify_segmentation` fallback when `segment_type` is an
unexpected value (`filings_agent/hierarchy/resolver.py:_classify_segmentation`).
`SegmentreportinginformationbysegmentSegmentation` is clearly not a
human-meaningful grouping label.

---

## H9 — Irrelevant dimensional members leak into the income statement

**Evidence:**

```
001.003.006 us-gaap:ForeignExchangeContractMember              (under revenue)
002.001     us-gaap:ForeignExchangeContractMember              (under cost of revenue)
006.001     us-gaap:InterestRateContractMember                 (under non-operating)
006.002     us-gaap:ReclassificationOutOfAccumulatedOtherComprehensiveIncomeMember
007.001     us-gaap:ReclassificationOutOfAccumulatedOtherComprehensiveIncomeMember
```

These are derivative / AOCI-reclassification disclosure members, not income
statement breakdowns. The context filters
(`core/extractors/dimensional_context_filters.py`) exclude forecast/scenario
contexts but not these axes.

---

## D1 — Cross-collection orphan values

**Evidence:**

```
concept_values_quarterly: 15 rows with concept_id = 6ab3d17b769aa3fb102bd7a8
   -> that _id exists only in normalized_concepts_ANNUAL
   -> concept us-gaap:PaymentsToAcquireProductiveAssets
```

**Impact:** in any quarterly view those values cannot be joined to a concept, and
the annual concept shows values only in the annual view. This is one concrete
instance of "a concept with no number in a period".

**Root cause:** values were written in an earlier run (before the alias
`PaymentsToAcquireProductiveAssets → PaymentsToAcquirePropertyPlantAndEquipment`
was recorded); later runs stop creating the quarterly concept row, so the values
are orphaned.

---

## D2 — Concept alias gaps leave split series

**Evidence:**

| Economic line | Old concept | New concept | Split |
|---|---|---|---|
| Capex | `us-gaap:PaymentsToAcquireProductiveAssets` (annual, a=4; quarterly values orphaned) | `us-gaap:PaymentsToAcquirePropertyPlantAndEquipment` (annual, a=11) | series split |
| Cash & equivalents | `us-gaap:CashAndCashEquivalentsAtCarryingValue` (2010–2018) | `us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents` (2019+) | series split |

`concept_aliases` **does** contain
`PaymentsToAcquireProductiveAssets → PaymentsToAcquirePropertyPlantAndEquipment`
(for 10-Q cashflow) and revenue aliases
(`us-gaap:Revenues`, `us-gaap:SalesRevenueNet →
RevenueFromContractWithCustomerExcludingAssessedTax`), but:

- aliases are scoped by `form_type` (10-K vs 10-Q) and do not retroactively
  move already-written values (`promote_concept` is only called from
  `persist` for decided promotions);
- the capex alias exists for `10-Q` only (annual values stay split).

---

## D3 — Cashflow `duplicate_concept` on every filing + value-loss risk

**Evidence:** `60/60` validation reports contain:

```
cashflow: concept 'us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
appears 2 times in one statement (later rows may not persist)
```

Beginning-of-period and end-of-period cash are two facts of the same concept in
the same statement. The unique index is:

```
concept_values_annual:    (cik, concept_id, reporting_period.fiscal_year)                 unique
concept_values_quarterly: (cik, concept_id, reporting_period.fiscal_year, .quarter)       unique
```

Neither includes `reporting_period.end_date` / `period_date`, so two instants
with the same fiscal year+quarter collide and the second is skipped
(`_create_value_record` dedups on this key). Today the values coincide, but any
statement where beginning and ending balances differ within the same
year+quarter silently loses a fact.

---

## D4 — "Concepts with no number in any period"

Strict query (0 values in both value collections) returns only the abstract
grouping headers:

```
normalized_concepts_annual:    custom:ProductSegmentation, custom:GeographicSegmentation, custom:BusinessSegmentSegmentation
normalized_concepts_quarterly: the three above + custom:SegmentreportinginformationbysegmentSegmentation
```

Abstract headers legitimately have no values. The user-visible "blank row"
cases are instead:

1. **Rows stranded at `555`** (revenue, pre-tax income) if the consumer filters
   or deprioritises `555`.
2. **Orphan values (D1)** — values exist but the matching concept row is missing
   in that collection.
3. **Legacy dimension rows (H3)** that belong to a different-era member name.

---

## F1 — 7 of 8 "failed" filings parse successfully on re-run

Re-ran `FlexibleXBRLExtractor` for each failed accession:

| Accession | Period | Re-parse result |
|---|---|---|
| `0000320193-20-000062` | FY2020 Q3 | `no_xbrl_available=True` (still fails — see F2) |
| `0000320193-20-000052` | FY2020 Q2 | statements extracted ✅ |
| `0000320193-20-000010` | FY2020 Q1 | statements extracted ✅ |
| `0000320193-19-000076` | FY2019 Q3 | statements extracted ✅ |
| `0000320193-19-000066` | FY2019 Q2 | statements extracted ✅ |
| `0001193125-16-439878` | FY2016 Q1 | statements extracted ✅ |
| `0001193125-12-444068` | FY2012 Annual | statements extracted ✅ |

**Conclusion:** these were transient SEC/network failures during the run
(the log shows `Service Unavailable retrieving …`). The company-mode pipeline
does not retry a filing after a failed extraction; it is reported as `failed`
and skipped for the rest of the run. The Redis worker has retry logic, the CLI
company run does not.

---

## F2 — False "no XBRL" classification

`0000320193-20-000062` resolves to
`…/000032019320000062/aapl-20200627_htm.xml`, but
`_resolve_optimal_xbrl_url` sets `_no_xbrl_available=True`
(`core/extractors/xbrl_parser.py:242`), so
`statement_processor` returns `None` with the message
"this filing predates the XBRL era or has no XBRL exhibits".

The filing is a normal inline-XBRL 10-Q and should be processed.

**Impact:** the filing is permanently skipped and produces a data gap
(e.g. cash/revenue for FY2020 Q3).

---

## F3 — SEC 503 retries are slow

The log repeatedly shows `Service Unavailable retrieving …/aapl-*.xml`, and some
filings took 30–70 s (`extract 57.5s`, `1m 8s`). A transient 503 should be
retried with backoff and counted; currently it makes the run very slow and, if
it exhausts retries, becomes F1.

---

## V1 — `math_mismatch` blocked FY2023 Q1

```
validate  fail · 1 blocking finding
  high math_mismatch (income/us-gaap:GrossProfit)
  — income: gross_profit does not reconcile — us-gaap:GrossProfit=5…
review  ✓ 1 proposal
repair  ✓ applied 0/1
decide  ✗ skip — nothing written
```

The write was correctly blocked, but the repair applied `0/1` and the filing
was dropped entirely rather than persisted without the unreconciled row or
repaired. Worth checking whether:
- the gross-profit formula expects a different set of children for that period,
  and/or
- `repair` should fall back to `write_partial` instead of `skip`.

---

## V2 — Orphan warnings and duplicate findings are ignored

- `hierarchy ✓ … ⚠ N orphans` appears on most filings (`2`, `3`, `18`, `22`, `23`)
  yet the agent's plan is still accepted and the filing persisted
  (`decide → write all permitted`).
- `duplicate_concept` is emitted `60/60` times but never changes the decision.

These warnings are effectively noise. Either they should block/repair, or the
checks should be fixed so they stop firing on known-benign patterns.

---

## Suggested order of work (not implemented)

1. **H1/H2/H5/H6** — hierarchy invariants:
   - forbid `555` (and hide) for any row that is a parent of another row;
   - forbid `555` for primary statement lines (revenue, gross profit, net
     income, totals, cash-flow activities);
   - when a parent moves, move its children with it, or reject the move;
   - persist the resolver's `path`/`order_key` for existing concepts so the
     tree can self-heal on every run;
   - repair existing rows: pull any `555` parent back to its missing parent path
     (identifiable via `concept_id` on dimensional rows and orphan path
     prefixes).
2. **H3/H4/H7** — make dimensional members first-class in the hierarchy review
   (include them in `_filing_concepts` and `_stored_rows`) and key sibling
   placement by `(parent, member)`.
3. **D1/D2/D3** — value identity and aliasing:
   - include `period_date`/`instant` or a full fiscal period key in the value
     unique index (or add an `is_instant`/period-kind discriminant);
   - run alias reconciliation across both form types and migrate orphan values;
   - merge split series (capex, cash) into one surviving concept.
4. **F1/F2** — extraction robustness:
   - retry extraction per filing with backoff and re-queue transient failures;
   - fix the inline-XBRL detection so `…_htm.xml` filings are not declared
     "no XBRL".
5. **H8/H9** — filter junk grouping headers and disclosure-only members.
6. **V1/V2** — turn orphan/duplicate findings into real actions or fix the
   checks.
