# Filings Agent — Master Plan

> Step-by-step plan for adding an AI agent to the SEC XBRL filings pipeline
> (`filings-extractor`), reusing the proven architecture of the existing 8-K
> agent in `/Users/aijaz/TrueGrids/all_projects/earning_agent`.
> Follow phases P1 → P7 in order; each phase ends with a verifiable
> acceptance criterion before moving on.

---

## 0. Goal

For every filing, processed **newest → oldest** (already how
`sec_client.get_company_submissions` sorts — `filingDate` desc):

1. **Validate** every filing after extraction + normalization, before DB write.
2. **Fill gaps** — fix wrong or missing numbers.
3. **Clean hierarchy** per filing, based on materialized `path` + `order_key`.
4. **Extract forward-looking MD&A guidance** for every filing and store it in
   `guidance_values` — the SAME collection the admin backend reads/writes
   (identical to the 8-K agent).
5. **First filing** → build the complete hierarchy fresh.
6. **Later filings** → reuse existing concepts; create only new concepts, placed correctly.

### Non-negotiables (agreed)

- Extraction and normalization are **pure compute — zero DB writes**.
- The **agent takes over after normalization**, does its work, and **the agent
  itself is the only writer** to the database.
- The agent reuses the **earning_agent architecture** (LangGraph +
  LangChain tools + `with_hooks` + STRICT_ACCURACY save gate + guidance +
  memory + LLM factory) — *not* a pi extension, *not* built from scratch.

---

## 1. Reference architecture — what we reuse from `earning_agent`

The existing 8-K agent (`/Users/aijaz/TrueGrids/all_projects/earning_agent`)
is the blueprint. Reuse its proven modules; adapt only what is XBRL-specific.

| earning_agent module | What it does | Reuse in filings agent |
|---|---|---|
| `hooks.py` | `with_hooks()` — node lifecycle logging, timing, exception → `status="failed"` short-circuit | ✅ copy as-is |
| `llm.py` | `build_llm()` / `build_chat_llm()` — ollama/groq/gemini/deepseek, JSON mode, rate limiting | ✅ copy as-is |
| `agent/loop.py` | `run_agent_loop()` — ReAct tool-calling loop + `finalize_*` terminal tool + JSON recovery | ✅ copy, change finalize tool name |
| `config.py` | env-driven settings, fail-fast for required vars (`MEMORY_ENABLED`), `STRICT_ACCURACY`, `GUIDANCE_ENABLED` | ✅ adapt env names |
| `state.py` | `TypedDict` state passed through nodes | ✅ new `FilingAgentState` |
| `graph.py` | LangGraph `StateGraph`, conditional edges, short-circuit on `failed`/`skipped` | ✅ new graph |
| `agent/tools.py` | `@_lc_tool` (LangChain tool) — pi-style navigation tools | 🔁 new XBRL tools (no doc navigation) |
| `nodes/save.py` | `mongodb_save_node` — STRICT_ACCURACY gate, currency gate, **atomic write-first replace** | ✅ adapt as `persist` node |
| `nodes/concepts.py` | load company concepts from `normalized_concepts_*` (path/order_key, recent-value window) | ✅ reuse for ask 5/6 |
| `integrations/normalize.py` | `get_statement_concepts()`, `upsert_concept_values()` (atomic), path-pollution filter | ✅ reference/reuse DB patterns |
| `agent/memory.py` | advisory `remember_*` tools → `agent_memory_company` | ⚪ optional later |
| `agent/guidance.py` + `integrations/guidance.py` + `nodes/save_guidance.py` | extract → normalize records → upsert + score (`guidance_values`) | ✅ reuse **verbatim** for ask 4 (same forward-looking guidance) |

The key difference: the 8-K agent uses the LLM to **extract numbers from
unstructured text**. For XBRL 10-K/10-Q, financial-statement extraction is
**deterministic** (Arelle), so the LLM runs for two things only:
(a) **judgment calls** (validation triage, repair authority, hierarchy
conflicts), and (b) **forward-looking MD&A guidance** — which IS unstructured
text and reuses the 8-K guidance stack verbatim.

---

## 2. Target architecture

```
PER FILING (newest → oldest)
 ┌────────────────────── PROGRAMMATIC (deterministic, no LLM, no DB writes) ──────────────────────┐
 │ 1. fetch_xbrl        XBRL → FinancialLineItem tree → flat line_items (existing core/)          │
 │ 2. normalize_bundle  line_items → StatementBundle  (PURE — no DB)                              │
 └─────────────────────────────────────────────────────────────────────────────────────────────────┘
 ┌────────────────────────── AGENT (LangGraph, earning_agent architecture) ────────────────────────┐
 │ 3. validate          deterministic checks → findings; LLM triage only when needs_decision       │
 │ 4. repair            fill missing / fix wrong (provenance kept, gate-controlled)                │
 │ 5. resolve_hierarchy seed (first filing) / reuse (later), re-path cleanly                       │
 │ 6. extract_guidance  HTML MD&A text → forward-looking guidance records (reuse 8-K stack)       │
 │ 7. persist           STRICT_ACCURACY gate → atomic write-first upsert (the ONLY writer)         │
 └─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

The pipeline is exposed to the LLM as **tools** (LangChain `@tool`), exactly
like the 8-K agent exposes document navigation tools. Clean filings cost zero
LLM decisions — the deterministic nodes do everything; the LLM node
(`agent_review`) runs only when `validate` produces findings needing judgment.

---

## 3. Pipeline contract — the normalization split (foundation)

Today `normalize_statement_in_memory` (called once, at
`sec_scraper_cli.py:1451`) both **computes and writes**. Split it in two.

### 3.1 Pure compute — `normalize_statement_to_bundle`

```python
# normalization/src/data_normalization_service/services/normalization_service.py
def normalize_statement_to_bundle(statement_doc, filing_doc, company_doc) -> StatementBundle:
    """Normalize a single statement to an in-memory bundle. NO DB writes."""
```

`StatementBundle` (new dataclass in `.../core/models.py`):

```python
@dataclass
class StatementBundle:
    company_cik: str
    statement_type: str              # income | balancesheet | cashflow
    form_type: str                   # 10-K | 10-Q
    accession_number: str | None
    filing_id: ObjectId
    reporting_period: dict           # cleaned {end_date, period_date, fiscal_year, quarter}
    created_at: datetime
    concepts: list[dict]             # {concept, canonical_concept, label, path, order_key, abstract, dimension}
    dimensional_concepts: list[dict] # {parent_concept, concept, segment_type, path, order_key}
    values: list[dict]               # {concept, period_key, value, fact_id, decimals, unit, is_calculated}
    dimensional_values: list[dict]
    hierarchy: list[dict]            # full tree incl. abstract nodes
    source_items: list[dict]         # original line items (audit/traceability)
```

Reuse existing pure helpers verbatim:
`_extract_all_dimensional_concepts`, `hierarchy_manager.build_hierarchy_data`,
`_is_structural_concept`, `_extract_time_period_values`,
`_extract_metadata_from_dimensional_facts`, `_determine_segment_info`.

### 3.2 Write-only — `persist_statement_bundle`

```python
def persist_statement_bundle(bundle, agent_decisions) -> dict:
    """Persist an agent-approved bundle. WRITES only; invoked by the persist node."""
```

Moves existing write logic unchanged (same repos, `DuplicatePreventionManager`,
idempotency/skip-if-exists): `_get_or_create_concept`, `_create_concept`,
`_create_value_record` (add return = written/existing doc id),
`_get_or_create_dimensional_concept`, `_create_dimensional_concept`,
`_create_dimensional_value_record`, `_process_dimensional_data_enhanced`,
`_ensure_company_in_target_from_dict`.

### 3.3 Backward compat

Keep `normalize_statement_in_memory()` as a thin wrapper
(`to_bundle` → `persist_statement_bundle`) so tests/legacy keep working.
The live pipeline stops using it.

---

## 4. State — `FilingAgentState`

New `TypedDict` in `filings_agent/state.py`, modeled on `EarningsAgentState`:

```python
class FilingAgentState(TypedDict):
    cik: str
    ticker: str
    company_name: str
    accession_number: Optional[str]
    form_type: str                          # 10-K | 10-Q
    status: str                             # pending → normalized → validated → repaired → saved | failed
    error: Optional[str]

    # ── programmatic stage outputs ────────────────────────────────
    bundles: Optional[list]                 # list[StatementBundle] (income/balancesheet/cashflow)
    reporting_period: Optional[dict]

    # ── validation / repair ────────────────────────────────────────
    findings: Optional[list]                # [{type, severity, message, evidence}] — same shape as 8-K agent
    validation_report: Optional[dict]       # full report (issues[], actions[], corrected_from/to, source)
    repair_decisions: Optional[list]        # agent-approved corrections

    # ── hierarchy (asks 3/5/6) ─────────────────────────────────────
    existing_concepts: Optional[list]       # from normalized_concepts_* (get_statement_concepts pattern)
    hierarchy_plan: Optional[dict]          # seed/reuse/re-path deltas
    hierarchy_seeded: Optional[bool]        # first-filing detection

    # ── guidance (ask 4) ───────────────────────────────────────────
    guidance_records: Optional[list]        # guidance_values-shaped docs (from normalize_guidance_records)
    guidance_save: Optional[dict]           # observability summary (never fails run)
    mda_text: Optional[str]                 # MD&A plain text (HTML → text) for the guidance loop

    # ── persistence ────────────────────────────────────────────────
    persist_receipt: Optional[dict]         # inserted/updated ids
    _pending_replace: Optional[dict]        # existing-period replace (atomic, like 8-K agent)
```

---

## 5. Graph (nodes) — LangGraph, earning_agent style

New `filings_agent/graph.py`:

```
fetch_xbrl → normalize_bundle → validate → [agent_review?] → repair
    → resolve_hierarchy → extract_guidance → persist → END
```

| # | Node | Ask | Job |
|---|------|-----|-----|
| 1 | `fetch_xbrl` | — | Reuse existing XBRL parse (deterministic). Sets `bundles`. |
| 2 | `normalize_bundle` | — | Call `normalize_statement_to_bundle` ×3 (pure). Sets `bundles`. |
| 3 | `validate` | 1 | Deterministic checks (structure/math/period/plausibility) → `findings` + `validation_report`. |
| 4 | `agent_review` | 2 | LLM node — runs `run_agent_loop` with XBRL tools **only when** `findings` has `needs_decision`. Produces `repair_decisions`. |
| 5 | `repair` | 2 | Apply `repair_decisions` to bundles (provenance kept). Gate-controlled. |
| 6 | `resolve_hierarchy` | 3,5,6 | Seed/reuse/re-path via `existing_concepts`; same-company-first precedence. |
| 7 | `extract_guidance` | 4 | Fetch HTML → MD&A text → guidance agent loop (reuse 8-K doc tools + contract) → `normalize_guidance_records` → `guidance_records`. |
| 8 | `persist` | — | STRICT_ACCURACY gate → `persist_statement_bundle` (the ONLY writer). |

All nodes wrapped with `with_hooks` (copied from earning_agent). Conditional
edges short-circuit to `END` on `status ∈ {failed, skipped}` — identical to
the 8-K graph.

---

## 6. Agent loop + tools (XBRL-specific)

Reuse `run_agent_loop` + `build_chat_llm` from `earning_agent` verbatim.
Only the **tools** and the **finalize contract** change.

XBRL tools (replacing `read_lines`/`search`/`get_document_info`):

| Tool | Purpose |
|---|---|
| `query_concepts(cik, statement_type)` | read existing `normalized_concepts_*` (path/order_key/label) — reuse decisions |
| `query_values(cik, concept_id, periods)` | read prior stored values — plausibility check |
| `query_hierarchy(cik, statement_type)` | full tree for conflict detection |
| `verify_math(expression)` | deterministic arithmetic (Assets = L+E, Rev − Exp = NI, cashflow sums) |
| `map_concept(label, candidates)` | map a filing label to an existing concept (same as 8-K `map_concept`) |
| `propose_repair(concept_id, from, to, reason)` | the agent's correction proposal — **write-gated** |
| `propose_hierarchy_placement(concept, parent_path, before/after)` | conflict-safe path proposal |
| `finalize_review(result_json)` | terminal tool — returns findings resolution + repair/hierarchy decisions |

The agent only calls these when `validate` flagged something; otherwise the
graph skips `agent_review` entirely (zero LLM cost for clean filings).

The guidance pass is a **second, separate agent loop** reusing earning_agent's
document-navigation tools (`build_pi_tools`: `get_document_info`, `read_lines`,
`search`, `find_sections`, `detect_currency`, `detect_scale`) plus the
`GUIDANCE_CONTRACT_BLOCK` / `GUIDANCE_PHASE_NOTICE` prompt contract and a
`finalize_guidance` terminal tool. It runs over the extracted MD&A text, not
the XBRL bundle.

---

## 7. Save gate (persist node) — the "agent is the only writer"

Modeled directly on `nodes/save.py` in earning_agent:

- **STRICT_ACCURACY gate**: unresolved high-severity findings → refuse persist,
  set `status="failed"`. Absence-only findings (`missing_concept`) never block.
- **Atomic write-first replace**: reuse the earning_agent `upsert_concept_values`
  pattern (write-first + stale sweep via `save_token`) — **no delete-before-write
  window**. Reload delete (`_delete_reload_data` at `sec_scraper_cli.py:1438`)
  stays before persist, as today.
- **Provenance**: every corrected value carries `corrected_from/to` + `source`;
  originals preserved in `validation_report`.

---

## 8. Guidance (ask 4) — forward-looking MD&A, reuse the 8-K stack verbatim

Guidance = **forward-looking MD&A statements** ("we expect revenue of $108B,
plus or minus 2%", capex/opex/margin/EPS outlook) from Item 7 (10-K) / Item 2
(10-Q) narrative text — NOT XBRL numeric facts.

Reuse the earning_agent guidance stack **verbatim**:

1. **Fetch + extract MD&A text** — deterministic: HTML filing (already
   downloadable via `--html-download-path`, or fetched from EDGAR) → strip
   HTML → isolate MD&A section (Item 7 / Item 2) → `state.mda_text`.
2. **`extract_guidance` node** — a dedicated `run_agent_loop` over `mda_text`
   using `build_pi_tools` (document navigation) + `GUIDANCE_CONTRACT_BLOCK` +
   `GUIDANCE_PHASE_NOTICE` + a `finalize_guidance` terminal tool. The LLM
   reports `__guidance__` (one JSON object per guidance number: metric,
   standard_label, basis, form, value/value_low/value_high, unit/scale/currency,
   as_printed, condition, period).
3. **`normalize_guidance_records`** (`agent/guidance.py`) — copied verbatim;
   takes the `__guidance__` payload + a `DetectedPeriod` + currency →
   `guidance_values`-shaped records.
4. **`save_guidance` node** — copied from the 8-K agent: `upsert_guidance_records`
   (key `(cik, accession_number, metric, basis, period)`, `is_current` demotion,
   `supersedes`, `source="manual"` protection) then `score_guidance_for_cik`
   (resolve to concept row → read stored actual → `result {outcome, delta_abs,
   delta_pct}`). **Never fails a run.**

**`DetectedPeriod` is built deterministically** from `bundle.reporting_period`
(`period_type`, `period_end`, `quarter`, `fiscal_year`, label) — no period agent
needed; XBRL already gives the period. Currency comes from the XBRL fact units
(USD assumed for US GAAP filings).

**Collection:** existing `guidance_values` in `normalize_data` — no new
collection. The filings agent writes the exact schema the admin backend already
reads/writes, with `source="llm"`.

---

## 9. DB additions (additive only)

```python
validation_reports   # { cik, accession_number, filing_id, statement_type, form_type,
                     #   status, issues[], actions[], corrected_from/to, source, created_at }
# companies gains:  hierarchy_seeded_at, hierarchy_seed_version
# existing normalized_concepts_annual/quarterly + concept_values_annual/quarterly unchanged
# guidance: REUSE existing guidance_values collection in normalize_data — no new collection
```

Config (`.env`, adapted from earning_agent `config.py`):

```
LLM_PROVIDER=deepseek                 # or ollama/groq/gemini
DEEPSEEK_API_KEY=...                  # already present
STRICT_ACCURACY=1                     # refuse persist on high-severity findings
GUIDANCE_ENABLED=1
AGENT_REVIEW_ENABLED=1                # LLM judgment node on/off
```

---

## 10. Integration points (exact)

| File | Change |
|---|---|
| `normalization/.../services/normalization_service.py` | add `normalize_statement_to_bundle` + `persist_statement_bundle`; keep `normalize_statement_in_memory` as wrapper |
| `sec_scraper_cli.py:1451` | replace `normalize_statement_in_memory` with `to_bundle` → hand bundles to graph |
| `sec_scraper_cli.py:1438` | `_delete_reload_data` stays (delete still precedes agent persist) |
| `sec_scraper_cli.py:772-774` | company stage (`_flush_quarterly_accumulator`, `_run_companyfacts_reconciliation`) — kept as backstop in v1; folded into the graph in P7 |
| `worker_10kq.py` | inherits via shared `SECDataScraperApp` — no change |
| new `filings_agent/` package | graph, state, nodes, tools, llm, hooks, config (copied/adapted from earning_agent) |

---

## 11. Phased build (follow in order)

### P1 — Normalization split (no agent yet) ✅ DONE
**Goal:** separate pure compute from DB writes.

- [x] Add `StatementBundle` to `normalization/.../core/models.py`
- [x] Implement `normalize_statement_to_bundle(...)` (pure)
- [x] Implement `persist_statement_bundle(...)` (write-only)
- [x] `_create_value_record` returns the written/existing doc id
- [x] `normalize_statement_in_memory` = thin wrapper (compat)
- [x] Wire `sec_scraper_cli.py` to the new split (`to_bundle` → `persist`)

**Delivered**
- `StatementBundle` dataclass in `normalization/.../core/models.py`
- `FinancialNormalizationService._build_bundle` (pure core),
  `normalize_statement_to_bundle` (dict → bundle),
  `persist_statement_bundle` (all writes), `_is_structural_concept` hoisted
  to a method; module constants `_ALLOWED_STATEMENT_TYPES`, `_NON_FIN_NS`,
  `_ALWAYS_SKIP_CONTAINS`
- Legacy source-DB path preserved via `_process_financial_statement_with_filing`
  (`enforce_allowed_types=False`)
- New test `tests/unit/normalization/test_statement_bundle_split.py` (8 tests):
  purity, bundle contents, write order, no-op, allowed-type filtering,
  wrapper equivalence, legacy path

**✅ Acceptance (verified):** full suite 7 failed / 98 passed — identical
7 pre-existing failures before and after the split (zero regressions);
write logic diffed line-for-line against `HEAD` (only variable renames +
relocation); `diagnose/accuracy_check.py` all green on live data.

---

### P2 — Port earning_agent primitives into `filings_agent/` ✅ DONE
**Goal:** reuse the proven agent runtime.

- [x] Copy `hooks.py`, `llm.py`, `agent/loop.py`, `config.py` (adapted)
- [x] New `state.py` (`FilingAgentState`), `graph.py` skeleton (2 nodes: normalize → persist)
- [x] `main_filing_agent.py` entry point (one filing end-to-end, no LLM yet)

**Delivered**
- `filings_agent/hooks.py` — copied from the earning_agent (import rewrites only):
  `with_hooks` node wrapper, `report_call` / node & detail callbacks
- `filings_agent/llm.py` — copied verbatim (provider imports stay lazy:
  ollama/groq/gemini/deepseek)
- `filings_agent/config.py` — adapted env config (MONGODB_URI, DATABASE_NAME,
  LLM_PROVIDER + provider blocks, STRICT_ACCURACY, AGENT_MODE,
  AGENT_REVIEW_ENABLED, GUIDANCE_ENABLED, MEMORY_ENABLED optional)
- `filings_agent/state.py` — `FilingAgentState` TypedDict + `new_state` /
  `state_summary` helpers
- `filings_agent/nodes/{normalize,persist}.py` — P1 split wrapped as graph
  nodes (`make_normalize_node` / `make_persist_node`)
- `filings_agent/graph.py` — `build_filing_graph(norm_service)`,
  `normalize_bundle → persist`, conditional short-circuit on failed/skipped
- `filings_agent/agent/loop.py` — generic ReAct tool-calling loop ported from
  the earning_agent with the 8-K-specific number parsing removed
  (`parse_json_object`, `run_agent_loop`, `AgentProviderError`)
- `filings_agent/agent/prompts.py` — `FINALIZE_DESCRIPTION`
- `main_filing_agent.py` + `filings-agent` console script
- Deps added: `langgraph>=0.2.0`, `langchain-core>=0.3.0`
- Tests: `tests/unit/agent/test_graph_skeleton.py` (9),
  `tests/unit/agent/test_agent_loop.py` (10)

**✅ Acceptance (verified):**
- Full suite 7 failed / **118 passed**; identical 7 pre-existing failures
  (zero regressions), +19 new tests
- Graph runs one filing end-to-end: `normalize_bundle → persist`
  (`status: saved`, receipt with 2/2 statements)
- **persist is the only writer** — a test asserts the normalize node never
  calls the persist API
- `with_hooks` logging + short-circuit verified (no-bundles / nothing-written /
  node exception → `failed` → END)
- Live E2E against a throwaway DB (`filings_agent_p2_smoke`, since dropped):
  1 company + 5 concepts + 5 values, abstracts filtered, paths assigned;
  production `normalize_data` confirmed untouched

**Deviation from the letter of the plan:** `agent/loop.py` was ported as a
*generic* loop rather than copied verbatim — the earning_agent version is
entangled with 8-K number parsing (scale multipliers, share counts,
`agent/derive.SCALE_MULTIPLIERS`).  The filings agent's terminal tool returns
decisions, not extracted numbers, so only the ReAct core was kept.

---

### P3 — Validate node (ask 1) ✅ DONE
**Goal:** deterministic validation before write.

- [x] `filings_agent/nodes/validate.py` — structure/math/period/plausibility
- [x] `ValidationReport` + `validation_reports` collection
- [x] Wire `findings` into the persist gate (STRICT_ACCURACY)

**Delivered**
- `filings_agent/validation/`:
  - `findings.py` — `Finding` + severities + `blocking_findings()` +
    `ABSENCE_ONLY_TYPES` (absence-only findings never block)
  - `report.py` — `ValidationReport` (`pass` / `pass_with_findings` / `fail`)
  - `common.py` — bundle accessors (numeric items, concept→value map, date/
    duration parsing)
  - `structure.py` — duplicate `(path, order_key)`, duplicate concept name,
    missing path/order, negative level, abstract leakage, empty statement
  - `math.py` — accounting identities (Assets = L+E; Assets =
    LiabilitiesAndStockholdersEquity; GrossProfit = Revenues − CostOfRevenue;
    OperatingIncome = GrossProfit − OperatingExpenses) with alias sets,
    sign-convention tolerance, and 0.5 %/abs-1 slack
  - `periods.py` — missing/unparseable period end, missing fiscal year,
    period-type ↔ form mismatch, unusual value duration
  - `plausibility.py` — implausible magnitude, unexpected negative,
    all-values-identical
  - `validator.py` — `validate_bundles()` orchestrator
- `filings_agent/reports.py` — `ValidationReportStore` (upsert per
  `(cik, accession_number, form_type)`) + `build_report_store()`
- `filings_agent/nodes/validate.py` — records the report, never blocks
- `filings_agent/nodes/persist.py` — STRICT_ACCURACY gate refuses on
  unresolved high-severity findings (read lazily so it is overridable)
- `filings_agent/graph.py` — `normalize_bundle → validate → persist`
- `main_filing_agent.py` — compute+validate mode now runs the validators and
  prints findings; `--persist` runs the full graph with the report store
- Fixture `tests/fixtures/filing_agent_broken.json` (gross-profit mismatch)
- Tests: `test_validation.py` (33), `test_persist_gate.py` (6)

**✅ Acceptance (verified):**
- Full suite 7 failed / **152 passed** — same 7 pre-existing failures (zero
  regressions), +53 agent tests total
- A filing with a broken math identity is **refused at persist**: live E2E on a
  throwaway DB → `status: failed`, error `refusing to persist … gross_profit
  does not reconcile`, **0 values written** for that accession
- Clean filings persist normally (5 concepts + 5 values written)
- `validation_reports` records BOTH outcomes (refused filing recorded as
  `status: fail`, `blocking_count: 1`) — a rejected filing stays traceable
- Gate is scoped: medium findings and absence-only types never block; disabling
  `STRICT_ACCURACY` still records the report but lets the write through
- Production `normalize_data` untouched; throwaway DB dropped

**P4 resolution:** the CLI routing deferral is now complete; the real scraper
path invokes the graph once per filing. In report mode, unresolved high findings
are refused by STRICT_ACCURACY; in repair mode, deterministic/agent-approved
corrections run before the final gate.

---

### P4 — Agent review + repair (ask 2) ✅ DONE
**Goal:** LLM judgment + gap-fill/correction, gate-controlled.

- [x] XBRL tools (`query_concepts`, `query_values`, `verify_math`,
      `propose_repair`; `finalize_review` supplied by the generic loop)
- [x] `agent_review` node — runs only when validation findings have
      `needs_decision`; deterministic identity fallback is used when the
      provider is unavailable or review is disabled
- [x] `repair` node — applies approved corrections in memory with
      `corrected_from`, `source`, `correction_reason`, `correction_source`
- [x] `CorrectionGate` — validates concept identity, stale old values,
      finite numeric values, trusted source, mode/approval; no DB access
- [x] Re-validation after repair before persist
- [x] Real scraper path now routes through the graph (normalize → validate →
      review/repair → validate → persist); rejected bundles are not added to
      the quarterly accumulator
- [x] Normalization value persistence carries repair provenance and updates an
      existing period value when an explicitly repaired item is reprocessed

**Delivered**
- `filings_agent/agent/tools.py`, `nodes/review.py`, `nodes/repair.py`,
  `review/corrections.py`
- `langchain-openai` dependency for the DeepSeek/Groq tool-calling providers
- `ValueDocument.to_dict()` now preserves `fact_id`, `decimals`, and `source`;
  repaired values additionally store `corrected_from`, `correction_reason`,
  `correction_source`, and `corrected_at`
- `sec_scraper_cli.py` invokes the graph once per filing; the graph is now the
  only per-filing writer and the quarterly accumulator runs only after a saved
  result

**✅ Acceptance (verified):** a broken gross-profit identity is corrected in
`AGENT_MODE=repair` and persisted as 600,000 with
`source="deterministic_identity"`, `corrected_from=999999`, and correction
reason/source; report is finalized as `pass`. In report mode the same filing
is not repaired and the STRICT_ACCURACY gate refuses it. Full suite remains
7 pre-existing failures / 168 passing tests.

---

### P5 — Hierarchy (asks 3, 5, 6) ✅ DONE
**Goal:** clean, reusable hierarchy.

- [x] `resolve_hierarchy` node — same-company seed first, reuse, re-path
- [x] Conflict-safe placement for occupied paths/order keys
- [x] Same-company-first precedence; cross-company reference only as fallback
- [x] `companies.hierarchy_seeded_at`, `hierarchy_seed_version`, and
      `hierarchy_seeded_statement_types`
- [x] Persist respects graph-approved paths/order keys instead of overriding
      them with the old cross-company promotion logic
- [x] Final validation runs after hierarchy resolution

**Delivered**
- `filings_agent/hierarchy/resolver.py` — fresh seed, existing concept reuse,
  filing-order placement, cross-company fallback, sibling conflict resolution,
  and repathing of duplicate historical `(path, order_key)` rows
- `filings_agent/nodes/hierarchy.py`
- Graph order now includes:
  `validate → review/repair → resolve_hierarchy → final_validate → persist`
- `FinancialNormalizationService.apply_hierarchy_updates()` for existing
  conflict repairs and `update_hierarchy_seed_metadata()` for seed state
- `persist_statement_bundle()` honors `_hierarchy_resolved` bundle annotations
- `sec_scraper_cli.py` continues quarterly accumulation only after graph save

**✅ Acceptance (verified):** two-filing throwaway integration produced stable
same-company paths, added a new concept at `001.004/d`, stored seed metadata,
and ended with zero duplicate `(path, order_key)` pairs. Seven hierarchy tests cover fresh
seed, existing reuse, conflict-safe siblings, cross-company fallback,
uniqueness, historical repathing, and node behavior.

---

### P6 — Guidance (ask 4) — forward-looking MD&A ✅ DONE
**Goal:** extract forward-looking guidance into `guidance_values` (reuse 8-K stack).

- [x] `fetch_mda_text` helper — HTML → MD&A plain text (Item 7 / Item 2)
- [x] `extract_guidance` node — `run_agent_loop` + MD&A tools + guidance contract + `finalize_guidance`
- [x] Port `agent/guidance.py` (`normalize_guidance_records`) + `integrations/guidance.py` (`upsert_guidance_records`, `score_guidance_for_cik`)
- [x] `DetectedPeriod` built from `bundle.reporting_period` (no period agent)
- [x] `save_guidance` node — upsert + score (never fails run)

**Delivered**
- `filings_agent/period.py` — `DetectedPeriod` + `build_detected_period()` (the
  period comes deterministically from XBRL; no LLM period agent)
- `filings_agent/agent/number_utils.py` — `coerce_number`, `is_usd_safe`,
  `SCALE_MULTIPLIERS` (the only helpers the guidance port needed from the 8-K
  number machinery)
- `filings_agent/agent/guidance.py` — `normalize_guidance_records` ported
  verbatim (imports repointed)
- `filings_agent/integrations/guidance.py` — `upsert_guidance_records` +
  `score_guidance_for_cik`; `_get_db()` now reads the filings-agent config
- `filings_agent/agent/prompts.py` — MD&A-scoped guidance contract
  (`GUIDANCE_SYSTEM_PROMPT`) + `GUIDANCE_FINALIZE_DESCRIPTION`
- `filings_agent/agent/mda_tools.py` — read-only navigation tools
  (`get_document_info`, `read_lines`, `search`)
- `filings_agent/mda.py` — HTML → text → MD&A isolation (Item 7 / Item 2),
  using the local `SEC_HTML_DOWNLOAD_PATH` archive (+ optional SEC fetch);
  best-effort, never fails a run
- `filings_agent/nodes/guidance.py` — `extract_guidance` + `save_guidance`
  nodes (both preserve `status`)
- Config: `GUIDANCE_ENABLED`, `GUIDANCE_LLM_ENABLED`, `GUIDANCE_MAX_CHARS`
- Graph: post-save `persist → (saved?) → extract_guidance → save_guidance → END`
- **Bug fix required for 10-K/10-Q:** `_resolve_concept` now prefers the
  concept collection matching the COVERED period.  A doc covering a full year
  previously resolved to the *quarterly* concept row, so the annual actual
  lookup queried `concept_values_annual` with a quarterly concept id and every
  annual actual was reported `unresolved`.  The 8-K stack never hit this (its
  guidance is always quarterly).
- Tests: `tests/unit/agent/test_guidance.py` (23)

**✅ Acceptance (verified):** three-filing throwaway-DB run — all docs stored
with `source="llm"`; the newer filing's FY2025 guidance demoted the earlier
FY2025 doc (`is_current: False`, `supersedes` set); and the FY2025 revenue
guidance scored against the stored FY2025 actual
(`outcome="beat"`, `actual_value=120e9`, `delta_pct=11.1%`).  Full suite
7 pre-existing failures / 191 passing.

---

### P7 — Company stage + batch + audit ✅ DONE
**Goal:** full-company runs, durable sessions, audit trail.

- [x] Fold `_flush_quarterly_accumulator` + `_run_companyfacts_reconciliation`
      into the graph (company agent stage)
- [x] JSONL audit of every node + tool/LLM call (reuses `with_hooks` events)
- [x] Headless batch loop (LLM only on `needs_decision`)

**Delivered**
- `filings_agent/audit.py` — `AuditLog` (thread-safe JSONL) + `install_audit()`;
  subscribes to the existing hook layer and **composes** with (never replaces)
  the CLI/worker progress callbacks; `agent_event()` accepts both the
  ``{"event": ...}`` dicts the agent loop emits and `(event, **fields)`
- `filings_agent/hooks.py` — added `get_node_callback()` for callback composition
- `filings_agent/session.py` — durable `RunSession` JSONL ledger with a
  `run_id` on every record, `is_saved()`/`saved_accessions()` for **resume**,
  and `build_session()` / opt-in `build_run_session()`
- `filings_agent/nodes/company.py` + `filings_agent/company_graph.py` —
  `quarterly_deaccumulation → companyfacts_reconciliation
  → finalize_validation_reports` over `CompanyAgentState`; every node is
  best-effort, records its own summary, and preserves a `failed` run status
- `filings_agent/reports.py` — `finalize_company(cik, summary)` stamps the
  company's validation reports (`phase: "company_final"`)
- `filings_agent/batch.py` — `run_batch()` (resume + audit + per-filing ledger)
  and `run_company_stage()`
- `filings_agent/graph.py` — `on_event` sink threaded into the agent loops
- `sec_scraper_cli.py` — both company-stage call sites now invoke
  `_run_company_agent_stage`; the superseded inline helpers were removed;
  per-filing outcomes are appended to the durable session ledger
- `main_filing_agent.py` — `--batch`, `--audit-log`, `--session-file`,
  `--no-resume`, `--company-stage`
- Config/state: `new_company_state(..., run_id, quarterly_statements,
  enable_reconciliation)` + `company_state_summary`; `.filings_agent/` ignored
- Fixture `tests/fixtures/filing_agent_batch.json` (2 filings, newest → oldest)
- Tests: `test_audit.py` (7), `test_session.py` (6), `test_company_stage.py` (11)

**✅ Acceptance (verified):** a two-filing batch ran newest → oldest through the
filing graph and then the company stage, writing 5 concepts + 10 annual values,
2 validation reports (finalized as `company_final` with the company-stage
summary) and the company hierarchy seed metadata.  A second run **resumed** —
both filings reported `skipped` with no graph invocation.  The audit JSONL held
44 records: `filing_start`/`filing_end` (status, findings, blocking, guidance),
34 node events covering both graphs (`normalize_bundle`, `validate`,
`resolve_hierarchy`, `extract_guidance`, `save_guidance`, `persist`,
`quarterly_deaccumulation`, `companyfacts_reconciliation`,
`finalize_validation_reports`), `company_start`/`company_end`,
`filing_skipped`, and `batch_end`.  Production DB untouched; throwaway DB
dropped; full suite 7 pre-existing failures / 215 passing.

---

### P8 — The agent decides what is written ✅ DONE
**Goal:** the final write must be an explicit, agent-owned decision — never an
implicit side effect of a flag inside `persist`.

**Delivered**
- `filings_agent/decision.py` — `WriteDecision` + two layers:
  - **Ceiling (validation, non-negotiable):** statement-level blocking findings
    block that statement; concept-scoped blocking findings exclude that concept;
    a filing-level finding blocks everything. Data validation proved wrong is
    never written.
  - **Decision (policy or agent):** `write` | `write_partial` | `skip`, plus
    additional concept exclusions.
- `filings_agent/nodes/decide.py` — the `decide` node, inserted between
  `validate_final` and `persist`. It always produces a `WriteDecision`, records
  it on the filing's `validation_reports` row and emits a `write_decision` audit
  event. The deterministic policy decides by default; when
  `AGENT_DECISION_ENABLED=1` an LLM judge is consulted **only for the ambiguous
  middle** (blocking findings exist *and* something is still writable) and may
  narrow the write or opt into a partial write — it can never widen the ceiling.
- `filings_agent/nodes/persist.py` — rewritten to **execute** the decision
  literally: `skip` writes nothing (`status="skipped"`, not a failure),
  `write`/`write_partial` write only the decided statements with the decided
  concepts excluded. With no decision on the state it derives the policy
  decision, so a write is never implicit.
- Graph: `… → validate_final → decide → persist → …`
- `ValidationReportStore.update_decision()` — the decision is stored next to the
  findings it was made from.
- Tests: `tests/unit/agent/test_decision.py` (19)

**✅ Acceptance (verified):** the same broken filing (gross-profit identity
fails) was run three ways against a throwaway DB — **0 values** written when the
policy decided `skip`; **2 values** (balance sheet only) when the agent judge
decided `write_partial ["balancesheet"]`; and the permitted write with
`us-gaap:GrossProfit` **excluded by the ceiling** when the judge decided `write`.
Every outcome recorded its decision (`action`, `statements`, `decided_by`) on
`validation_reports`. Full suite 7 pre-existing failures / 234 passing.

**Semantic change:** an unresolved blocking finding now yields
`status="skipped"` (a deliberate decision) instead of `status="failed"` (an
implicit refusal). Failures remain failures — e.g. a decision to write that
writes nothing is still `failed`.

**Open decision for the operator:** `AGENT_DECISION_ENABLED` defaults to `0`
(deterministic policy decides). Setting it to `1` makes the LLM the final
decider for flagged filings — one LLM call per filing that has blocking findings
*and* a writable remainder.

---

### P9 — Command-compatibility fixes (housekeeping) ✅ DONE
**Goal:** every documented command actually runs.

Verified rather than assumed: each entry point, every README flag, a whole-repo
import sweep, and the full test suite.

**Fixed**
1. **`.venv` stale shebangs (real breakage).** The project had been moved from
   `admin_panel/filings-extractor` → `all_projects/filings-extractor`, leaving
   30 absolute shebangs pointing at the old path. `uv run pytest` therefore fell
   back to anaconda python 3.14 (`No module named 'bson'`), and `arelleCmdLine`,
   `streamlit`, `tqdm`, `dotenv` were broken the same way. Fixed with
   `uv sync --all-groups --reinstall` plus repairing the 5 `activate*` scripts.
2. **`normalize-data` entry point.** `normalization/pyproject.toml` declared
   `normalize-data = "main:main"`, but `main.py` lives outside the wheel's
   `packages = ["src/data_normalization_service"]`, so it was never installed
   (`ModuleNotFoundError`). The runner now lives at
   `data_normalization_service/cli.py` and the script points there;
   `normalization/main.py` is kept as a working shim.
3. **Two modules with syntax errors.** `data_access/mongodb_repositories.py`
   and `database/repositories/repositories.py` each had a `try:` with no body
   (the intended `collection.create_index(index_spec, **options)` was missing).
   Both now compile and import. Nothing imports them today — the second is
   documented as the current CRUD layer and the first as legacy per
   `.github/copilot-instructions.md`; the live path uses
   `data_normalization_service/database/database.py`.
4. **README `--local` was never implemented.** No offline XBRL cache exists
   anywhere in the repo (there is not even a `download_xbrl` client method), so
   the stale section was replaced with an explicit "not implemented" note. The
   README now also documents the agent pipeline env vars and the
   `filings-agent` commands.

**✅ Acceptance (verified):** whole-repo import sweep → 114 modules, **0
failures** (was 2); README flags → **0 missing** from the CLI (was
`--local`); all five entry points (`main`, `sec-scraper-worker`,
`filings-agent`, `normalize-data`, `python normalization/main.py`) respond to
`--help`; `uv run pytest tests/ -q` → 236 passed / 7 pre-existing failures; 0
stale shebangs.

**Not implemented (offered):** a true offline mode would need an XBRL
instance/zip cache — Arelle requires the whole discovery taxonomy set, so it is
a feature in the extraction path rather than a flag.

**Related behavioural change worth knowing:** with `--reload` the period delete
is now **deferred** to just before the agent's write (a pre-write hook), so an
agent decision to skip a filing can no longer delete existing rows without
replacing them.

---

### P10 — Fiscal-period test failures fixed ✅ DONE
**Goal:** a fully green suite (7 failures had been red since before P1).

**Diagnosis — the tests were stale, not the code.** All seven encoded the *old*
contract: read the fiscal year end from the SEC submissions payload
(``company_data["fiscalYearEnd"]``) and ignore the database.  The implementation
deliberately changed to read ``companies.corporate_info.fiscal_year_end`` and
return ``None`` rather than guess (documented rationale: SEC ``fiscalYearEnd``
misreports non-calendar filers — Dell is listed as ``1231`` while its fiscal
year actually ends the Friday nearest 31 January).  Because the tests passed no
CIK and no DB, the lookup returned ``None``, filtering was skipped, and every
filing was selected.

**Fixed:** the seven tests now drive the real path — a fake ``db`` whose
``companies.find_one`` returns the fiscal-year-end document, plus a CIK — and
keep their original assertions (FY-only selects the 10-K; Q1/Q2/Q3 select the
matching 10-Q; Q4 selects the 10-K; no filter keeps all four; period labels;
the accession→period fallback in ``_find_existing_filing``).

**Added regression guards** so the deliberate policy cannot be "fixed" back:
``test_sec_fiscal_year_end_metadata_is_ignored`` and
``test_missing_db_company_skips_period_filtering_rather_than_guessing``.

**✅ Acceptance:** ``uv run pytest tests/ -q`` → **245 passed, 0 failed** (was
236 passed / 7 failed).

**⚠️ Open decision found while fixing this.** ``corporate_info.fiscal_year_end``
is written *verbatim* from ``raw_data["fiscalYearEnd"]``
(``core/transformers/data_transformers.py:39``), so the "authoritative" DB value
has the same origin as the metadata the code refuses to trust — the DB copy is
only more trustworthy when an operator has since corrected it.  Consequence: on
a **first run** for a company not yet in ``companies``, the lookup returns
``None``, ``--fiscal-year`` / ``--fiscal-quarter`` silently filter nothing, and
**every** filing is processed (and with ``--reload``, every filing is reloaded) —
the opposite of the requested scope.

Options (needs an operator call — filtering scope in production should not be
changed unilaterally):
* **(a) Bootstrap from the payload** when the company is absent from the DB —
  the same value that is about to be persisted anyway; log it explicitly.
* **(b) Seed/upsert the company document before filtering** so the authoritative
  lookup succeeds on the first run and one code path is kept. *(recommended)*
* **(c) Keep DB-only** and make the failure loud — refuse to run (or require an
  override flag) instead of silently processing every filing.

---

### P11 — Live terminal narration ✅ DONE
**Goal:** the agent shows every step it takes in the terminal, like the
earning_agent does.

**The gap:** the real pipeline (`uv run main`) installed no hook callbacks at
all — it showed tqdm bars and logger output only, so the whole agent flow
(normalize → validate → review → repair → hierarchy → decide → persist →
guidance) was invisible.  The nodes also emitted almost no `report_call` lines.

**Delivered**
- `filings_agent/presenter.py` — the terminal presenter:
  - `NODE_LABELS` + `format_step_line(node, state)` produce a human summary per
    node (verdict, findings, repair before/after, hierarchy seeding, the write
    decision with its ceiling, the persist receipt, guidance counts, company
    stage), and `▶ stage` lines as each node starts.
  - A per-filing progress trail (`✓norm ✓valid ✓hier ✓final ✓decide ✗skip`)
    and a filed header (`── AAPL · 10-K · 0000320193-24-000123 ───`).
  - Output goes through `tqdm.write` so step lines appear **above** the
    progress bars; the callbacks **compose** with the JSONL audit trail rather
    than replacing it.
  - A broken writer warns once instead of silently dropping the narrative.
- **Distinct node names.** `with_hooks` derives the reported name from
  `__name__`, and the graph used one factory (`make_validate_node`) for three
  stages — so all three reported as `validate_node`.  `_named()` now gives each
  node a unique name (`validate_node` / `validate_after_repair_node` /
  `validate_final_node`), which also sharpens the audit trail.
- **In-node narrative:** `report_call` lines for the persist write (per
  statement), the guidance MD&A pass, the review-agent consultation and the
  decision-judge consultation — so long steps narrate *while* they run, on top
  of the `[llm]`/`[tool]` lines the agent loop already emitted.
- **Wiring:** `sec_scraper_cli._ensure_audit()` now installs the presenter (with
  `tqdm.write`) before any graph runs; `main_filing_agent` uses the same
  presenter and is narrative-first (logger at WARNING, `-v` for INFO/DEBUG).
- Tests: `tests/unit/agent/test_presenter.py` (17).

**Bug found and fixed while wiring:** the CLI passed `tqdm.write` on the
*module* (`import tqdm`), which does not exist — the real method is
`tqdm.tqdm.write`.  The presenter's safety net swallowed the `AttributeError`,
so the narrative vanished silently on the real CLI path.  Fixed, and writer
failures now warn once.

**✅ Acceptance:** the real CLI graph path prints the full narrative —
```
  ── SMOKE  ·  10-K  ·  CLI-BROKEN-2 ────────────────────────────────────
  ▶ normalize
  SMOKE  ✓norm
  [normalize]       2 statement doc(s) → 2 bundle(s)  (income, balancesheet)   0.0s
  ▶ validate
  SMOKE  ✓norm ✓valid
  [validate]        fail  ·  1 finding(s) (1 blocking)  ·  checks {...}
  │    high  math_mismatch  (income/us-gaap:GrossProfit) — gross_profit does not…   0.0s
  ▶ review → ▶ repair → ▶ re-validate → ▶ hierarchy → ▶ final validate
  ▶ decide
  [decide]          SKIP by policy — writing nothing
  │  permitted=['income', 'balancesheet']  blocked=[]
  │  excluded {income: [us-gaap:GrossProfit]}
  │  reason: 1 unresolved high-severity finding(s); 2 of 2 statement(s) would be writable
  ▶ persist
  [persist]  skipping — 1 unresolved high-severity finding(s); …
  SMOKE  ✓norm ✓valid ✓review ✓repair ✓re-val ✓hier ✓final ✓decide ✗skip
  [persist]         ✗ skipped — 1 unresolved high-severity finding(s); …   0.0s
```
Full suite: **262 passed**, 0 failed.

---

### P12 — Production DuplicateKeyError on `--latest --reload` ✅ FIXED
**Reported:** `uv run main --file stocks_download.txt --latest --reload` on AAPL's
FY2026 Q3 10-Q (0000320193-26-000020) crashed:

```
[error]  persist_node failed: DuplicateKeyError: E11000 duplicate key error
collection: normalize_data.concept_values_quarterly index: idx_unique_value
dup key: { cik: "0000320193", concept_id: ObjectId('6a68…'), fiscal_year: 2026, quarter: 3 }
```

**Two coupled bugs.**

1. **The dedup query was NARROWER than the DB unique index.**
   `_create_value_record` built its own existence query containing
   `statement_type`, `form_type`, `calculated` **and
   `reporting_period.period_date`**, while the index is
   `(cik, concept_id, reporting_period.fiscal_year[, quarter])`.  When any of the
   extra fields drifted — e.g. a row written by an earlier run with a different
   `period_date` — the lookup missed a row the index considered identical and the
   following `insert_one` raised E11000.  `ValueRepository` already ships an
   index-aligned finder (`find_existing_value`, whose own docstring describes
   **this exact bug**), but it was not being used.

2. **`--reload` deleted by accession while writes are unique by period.**
   `_build_reload_filter` scoped a `--latest` reload to the filing's accession
   number (the narrative showed `[reload] no existing rows to delete`), yet
   uniqueness is per *period*.  A same-period row written under another accession
   therefore survived the delete — and the replacement write then either skipped
   it (stale data) or collided with it.

**Fixed**
- `_create_value_record` now dedups through `value_repo.find_existing_value(...)`
  — exactly the index key — so a drifting `period_date`/`statement_type` can no
  longer cause a duplicate insert.  Ordinary reruns stay insert/skip-only.
- New `replace_existing` flag, threaded
  `bundle → persist node → persist_statement_bundle → _create_value_record`:
  on `--reload` an existing period row is **overwritten in place** (the DB allows
  only one row per period), so a reload actually replaces.
  (Ordering matters: the replace branch is checked *before* the
  missing-accession backfill, which otherwise swallowed the replace.)
- `_build_reload_filter` now deletes by **period identity**
  (`reporting_period.fiscal_year` + `quarter` for 10-Q only, matching the annual
  index which stores no quarter), falling back to accession only when the fiscal
  year end cannot be derived.  Fully defensive — never raises when the app has
  no DB/company state.
- **Reporting:** a company with failed filings was printed as `❌` yet the run
  still ended "All companies processed successfully!".  Success now requires a
  clean run *and* zero failed filings, and the tail reports the failure count.

**Verified**
- The crash was **reproduced first** in a throwaway DB with the production
  `idx_unique_value` index recreated, then fixed and re-verified: a rerun is now
  a no-op (`value` unchanged, no crash) and a reload replaces in place
  (`111111 → 999`, `period_date` refreshed, still exactly 1 row).
- New tests: `tests/unit/normalization/test_value_write_identity.py` (6 — four
  unit + two against a real Mongo, skipped when unavailable) plus three
  reload-filter tests.  Full suite **271 passed, 0 failed**.

---

### P13 — Rich CLI: clear steps, no terminal noise ✅ DONE
**Reported:** the step narration worked but the terminal was noisy — tqdm bars
and step lines fought over the same lines, producing overwritten/wrapped
duplicates, and the run ended with two competing "PROCESSING COMPLETE" banners
plus a summary that contradicted itself.

**Delivered**
- **`ProgressManager` rewritten on Rich** (`rich.progress.Progress` +
  `Console`), keeping its entire public API — `label`, `start_companies`,
  `set_current_company`, `advance_company`, `finish_companies`, `filing_bar`
  (returns a `.update/.set_description_str/.close` adapter), `close_filing_bar`,
  `close_all`, `set_status`, `status`, `error`.  Every call site is unchanged,
  including the Redis worker's `WorkerProgressManager` subclass.
- Rich clears the live region, prints the step line, then redraws the bars — so
  narration always occupies **its own line** and scrolls above the progress
  region instead of being overwritten.
- **`markup=False` everywhere step text is printed.**  Our tags are literal
  (`[normalize]`, `[llm]`, `[tool]`, `[persist]`); Rich would otherwise swallow
  them as style markup (this bit the first attempt — `[validate]` disappeared).
  The live-region description column uses `Column(no_wrap=True,
  overflow="ellipsis")` so a long description can never wrap and corrupt the
  region.
- **One end-of-run panel** replaces the duplicated `====` banners: per-company
  outcome rows, overall totals, succeeded/failed counts and a single verdict
  line.  The download-only mode uses the same panel.
- Verbose mode disables the live bars entirely (no task bookkeeping, `filing_bar`
  returns a no-op) — the logger owns output and the narration still prints.
- `presenter._default_writer` now prefers Rich too, so the standalone
  `filings-agent` entry point renders identically.
- `Console` is injectable (`ProgressManager(console=...)`) so the display is
  capturable in tests.
- Removed the dead tqdm `_fmt_time` monkeypatch; `rich` added as a dependency
  (rebuild the Docker image: `docker compose up -d --build`).
- Tests: `tests/unit/sec_processing/test_cli_presentation.py` (8).

**✅ Acceptance:** full suite **279 passed, 0 failed**; all four entry points
respond; captured render confirms literal tags survive, step lines are emitted
one-per-line above the live region, and the summary renders as a single panel.

---

### P14 — Quiet-by-default narration (kill the CLI noise) ✅ DONE
**Reported:** the step narration was far too chatty — ~25 lines per filing.  Per
node it printed `▶ stage`, a repeated `✓trail`, internal dicts
(`checks {'structure': 3, …}`, `sources {'fresh_seed': 72}`), the *same* medium
finding verbatim twice (with a giant truncated concept name), `permitted=[] /
blocked=[] / reason: validation passed`, three persist lines for one write, and
`[guidance] no MD&A … skipped` + `no_records` for the common case.

**Root cause:** the presenter treated "show what is happening" as "print a line
per node", so every internal field became terminal output — and the medium
`duplicate_concept` finding (which is normal upstream data) was dumped in full,
twice.

**Delivered — narration is now two layers**
1. **Live status in the progress bar** (in place, no scrolling): the filing bar
   description is driven by the agent —
   `AAPL   10-Q  ✓norm ✓valid ▶hierarchy` — via a new `stage_writer` hook and
   `ProgressManager.set_filing_status()`.
2. **One outcome line per filing**, plus detail lines only for genuine anomalies:

```
  [filing] FY2026 Q3    10-Q 0000320193-26-000020 extracting
  ✓ 3 statements written  ·  1 finding  ·  0.3s
  [filing] FY2026 Q2    10-Q 0000320193-26-000013 extracting
  ✓ 3 statements written  ·  1 finding  ·  0.3s
  [filing] FY2025 Annual 10-K 0000320193-25-000079 extracting
  ✓ 3 statements written  ·  1 finding  ·  0.3s
      guidance  3 records extracted
  company stage  quarterly deaccumulated  ·  companyfacts +1  ·  4 reports finalized  ·  2.4s
```
   **10 lines for 4 filings** (was ~100).

Rules now enforced:
- medium/low findings are **counted, never dumped** (kills the repeated
  `duplicate_concept` wall); only `high` findings print detail;
- repairs print `concept  old → new (source)`;
- a refusal is one line (`✗ not written · reason`), a failure one line;
- guidance is **silent** unless records were actually extracted;
- the company stage prints one line and stays silent when nothing happened;
- no per-filing `── header ──` (the CLI already prints the filing line), no
  `[xbrl] … normalized` duplicate, no internal dicts, no `permitted/blocked`.

**Debug escape hatch:** `-v` / `--verbose` or `AGENT_STEPS=1` restores the full
per-node trace (`format_step_line` is retained for that mode and still tested).

**✅ Acceptance:** full suite **286 passed, 0 failed**; all four entry points
respond; `AGENT_STEPS` toggles the mode; the simulation above renders 10 lines
for 4 filings.

---

### P15 — Per-filing step list (start → end, minus the noise) ✅ DONE
**Reported:** P14 over-corrected — one outcome line per filing hid the actual
work.  The requirement is to see the **main tasks for every filing from start to
end** (fetch, extract, normalize, validate, hierarchy, decide, persist,
guidance) without the internal noise that was removed.

**Delivered — the narration is now an explicit step list**
```
    fetch         ✓  14 filing(s) listed on EDGAR (2010+)   0.4s
  ── AAPL  ·  FY2026 Q3  ·  10-Q  ·  0000320193-26-000020 ────────────────
    extract       ✓  fetched + parsed XBRL → 3 statement tables   3.2s
    normalize     ✓  3 bundles (income, balancesheet, cashflow)
    validate      ✓  pass_with_findings  ·  1 finding
    hierarchy     ✓  72 concepts (reused)
    decide        ✓  write all permitted
    persist       ✓  3/3 statements written   0.6s
    guidance      –  no MD&A text available
  ✓ written
```
- One line per executed pipeline step, indented under the filing header, in
  order.  `✓` done, `✗` failed/refused, `–` skipped/not applicable.
- The XBRL **fetch + parse** is a real step the CLI now reports (it happens
  before the graph), and the company-level EDGAR **fetch** is a step too.
- **Detail stays honest but terse**: verdict + finding *counts* (never the
  finding text for medium/low), concept counts, write counts, record counts.
  Suppressed entirely: `checks {…}`, `sources {…}`, `permitted/blocked`,
  internal paths, repeated identical verdicts (`verify` disappears when it
  matches `validate`), `0 records`, and micro-durations (<50 ms).
- Blocking findings print as one indented sub-line under the validation step
  that produced them (not repeated by later steps); repairs print
  `concept old → new (source)` under the repair step.
- The **live** current step remains in the progress bar
  (`AAPL  10-Q  ✓norm ✓valid ▶hierarchy`), so a slow step is visible while it
  runs, not just after it finishes.
- `-v` / `AGENT_STEPS=1` still restores the old verbose per-node internals.

**✅ Acceptance:** full suite **289 passed, 0 failed**; the render above is the
captured output for two filings plus the company stage.

---

### P16 — Agent-owned hierarchy ✅ DONE
**Requirement:** with thousands of companies, hierarchy must be the agent's
decision (not deterministic rules alone), verified against the real DB.

**Evidence gathered (MSFT/GOOGL/TSLA/AMZN) — why rules were not enough**
- Paths were **shared by several rows** (a path must identify exactly one row):
  TSLA quarterly 51, TSLA annual 34, GOOGL quarterly 32, MSFT annual 17.
- **Segment / legacy-era concepts were stored as ordinary line items** — e.g.
  MSFT income has 25 of 56 rows as ``custom:Segmentation``,
  ``custom:Segmentationold1rev``, ``custom:ServerAndToolsSegold`` (6 rows sharing
  one path), ``custom:GeographicAreas``, ``custom:IntelligentCloud``; GOOGL has
  ``GoogleCloudOi``/``OtherBetsOi`` sharing a path.
- **Grouping headers were never stored** (abstract rows: GOOGL/TSLA/AMZN = 0), so
  R&D/S&M/G&A hung off *Gross profit* and EPS/share counts off *Net income*.
- Genuine duplicate ``(path, order_key)`` pairs (e.g. MSFT cashflow).

Deciding that ``custom:ServerAndToolsSegold`` is a defunct 2011 segment member
rather than a line item is **semantic** — exactly what an LLM should judge.

**Delivered**
- ``filings_agent/hierarchy/planner.py`` — the deterministic half:
  ``propose_hierarchy`` takes ``{concept, parent, position, abstract, hide}`` and
  the planner computes materialised paths and lexicographic order keys, then
  validates (parents exist, no cycles, no duplicate siblings, no duplicate
  concepts, unique paths).  **The model never emits a path.**
- ``filings_agent/agent/tools.py`` — agent tools: ``query_hierarchy`` (stored
  tree + defect list + the rows this filing reports), ``propose_hierarchy``,
  ``validate_hierarchy``.
- ``filings_agent/nodes/hierarchy_review.py`` — the ``hierarchy_review`` node,
  inserted between ``resolve_hierarchy`` and ``validate_final``.  It runs **only
  when it matters**: a seed (first filing for a company+statement) or a detected
  anomaly.  Clean reuses cost **zero** LLM calls, so a thousands-company
  backfill stays affordable (~3 calls per company, not per filing).
- **Guardrail:** an invalid proposal, a provider failure, or a disabled agent all
  leave the deterministic resolver's plan untouched.  Nothing malformed can
  reach MongoDB.
- ``hide: true`` support end-to-end (``ConceptDocument.hide``,
  ``apply_hierarchy_updates``), so legacy rows are retired **without deleting
  their values** — and consumers already filter ``hide``/``abstract``.
- New grouping headers are created as ``abstract: true`` rows; stored rows whose
  placement/visibility changes are updated **by ``_id``** (dropping the id
  silently skipped them — caught by the demo).
- ``HIERARCHY_AGENT_ENABLED`` (default on; off in the test suite so tests never
  call a provider).
- Step line now reports the decider: ``hierarchy ✓ 56 concepts placed · agent ·
  25 rows hidden``.

**Verified**
- Synthetic MSFT-shaped statement: the agent re-parented R&D/S&M/G&A under
  ``Operating expenses`` (``004.001-003``), EPS under ``Earnings per share``
  (``007.001``), created both headers, and retired 4 legacy segment rows via
  ``hide``.
- Two bugs found and fixed by that demo (missing ``_id`` on stored updates;
  ``or []`` losing new headers) — both now have regression tests.
- Full suite: **305 passed, 0 failed**.

**Decisions applied (per operator):** junk rows are **hidden, not deleted**; and
**no old-data cleanup** — the agent handles new filings on existing companies and
all new companies.  Broken legacy trees therefore get corrected company-by-company
as new filings arrive, rather than in a one-off migration.

---

## 12. Risks & open decisions

1. **Corrections to financial data** — `report` (flag only) vs `repair` (auto
   with provenance) vs `strict` (human approval). **Recommend: start `report`,
   then `repair` per-company.**
2. **On FAIL** — write valid subset + quarantine rejected items
   (**recommended**), or write nothing?
3. **Company stage** — fold quarterly deaccumulation + companyfacts fully into
   the graph (P7), or keep as separate writers the agent only validates?
4. **Agent model** — DeepSeek (key already present) or another provider from
   `build_chat_llm`?
5. **Re-pathing historical data** — acceptable to rewrite `path`/`order_key` on
   reload (ask 3's intent), or backfill once via a migration script?
6. **"Guidance" scope** — ✅ RESOLVED: forward-looking MD&A guidance, reusing
   the 8-K agent's `guidance_values` stack verbatim.

---

## 13. Status

- [x] P1 — Normalization split
- [x] P2 — Port earning_agent primitives
- [x] P3 — Validate node
- [x] P4 — Agent review + repair
- [x] P5 — Hierarchy
- [x] P6 — Guidance (forward-looking MD&A)
- [x] P7 — Company stage + batch + audit
- [x] P8 — Agent-owned final write decision
- [x] P9 — Command-compatibility fixes
- [x] P10 — Fiscal-period test failures fixed
- [x] P11 — Live terminal narration
- [x] P12 — Production DuplicateKeyError on reload fixed
- [x] P13 — Rich CLI: clear steps, no terminal noise
- [x] P14 — Quiet-by-default narration (kill the CLI noise)
- [x] P15 — Per-filing step list (start → end)
- [x] P16 — Agent-owned hierarchy
