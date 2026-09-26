"""Live terminal narrative for the filings agent.

Mirrors the earning_agent's operator UX: as the graph runs, every node prints a
``▶ stage`` line when it starts and a human-readable summary when it finishes,
and every ``report_call`` line emitted inside a node (LLM calls, tool calls,
validation findings, repairs, decisions, writes) is streamed to the terminal.

Output goes through :func:`_default_writer`, which uses ``tqdm.write`` when
available so step lines appear *above* the progress bars instead of corrupting
them.  Callbacks are **composed** with whatever is already registered (e.g. the
JSONL audit trail from :mod:`filings_agent.audit`), never replacing them.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ── node → short human label ────────────────────────────────────────────────
NODE_LABELS: dict[str, str] = {
    "normalize_bundle_node": "normalize",
    "sign_fix_node": "signs",
    "validate_node": "validate",
    "agent_review_node": "review",
    "repair_node": "repair",
    "hierarchy_agent_node": "hierarchy",
    "resolve_hierarchy_node": "hierarchy",
    "hierarchy_review_node": "hierarchy review",
    "concept_resolve_node": "concepts",
    "validate_after_repair_node": "re-validate",
    "validate_final_node": "final validate",
    "decide_node": "decide",
    "persist_node": "persist",
    "extract_guidance_node": "guidance",
    "save_guidance_node": "guidance save",
    "quarterly_deaccumulation_node": "quarterly",
    "companyfacts_reconciliation_node": "companyfacts",
    "finalize_validation_reports_node": "reports",
}

# Step names printed in the per-filing step list (start → end).
STEP_LABELS: dict[str, str] = {
    "normalize_bundle_node": "normalize",
    "sign_fix_node": "signs",
    "validate_node": "validate",
    "agent_review_node": "review",
    "repair_node": "repair",
    "hierarchy_agent_node": "hierarchy",
    "resolve_hierarchy_node": "hierarchy",
    "hierarchy_review_node": "hierarchy review",
    "concept_resolve_node": "concepts",
    "validate_after_repair_node": "re-validate",
    "validate_final_node": "verify",
    "decide_node": "decide",
    "persist_node": "persist",
    "extract_guidance_node": "guidance",
    "save_guidance_node": "guidance save",
    "quarterly_deaccumulation_node": "quarterly",
    "companyfacts_reconciliation_node": "companyfacts",
    "finalize_validation_reports_node": "reports",
}

# Filing-scoped stages shown in the "✓a → ✓b → ▶c" progress line.
_FILING_STAGES = {
    "normalize_bundle_node", "sign_fix_node", "validate_node", "agent_review_node", "repair_node",
    "hierarchy_agent_node",
    "validate_after_repair_node", "validate_final_node",
    "decide_node", "persist_node", "extract_guidance_node", "save_guidance_node",
}
_SHORT_STAGE = {
    "normalize": "norm", "signs": "signs", "validate": "valid", "re-validate": "re-val",
    "final validate": "final", "review": "review", "repair": "repair",
    "hierarchy": "hier", "decide": "decide", "persist": "write",
    "guidance": "guid", "guidance save": "guid-save",
    "quarterly": "qtr", "companyfacts": "cfacts", "reports": "reports",
}


# Width of "  [label]" so every step line's text starts in the same column.
_PREFIX_WIDTH = 20


def _clip(text: str, limit: int) -> str:
    """Single-line, ellipsized text for the narrative."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _prefix(label: str) -> str:
    """Left-justified ``  [label]`` prefix of a fixed width."""
    return f"  [{label}]".ljust(_PREFIX_WIDTH)


def _plural(count: int, noun: str) -> str:
    """'1 statement' / '3 statements'."""
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _fmt_dur(ms: Optional[float]) -> str:
    """450 → '0.5s', 5300 → '5.3s', 196200 → '3m 16s'."""
    if ms is None:
        return ""
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    return f"{minutes}m {seconds - minutes * 60:.0f}s"


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_concepts(mapping: Any, limit: int = 4) -> str:
    if not isinstance(mapping, dict) or not mapping:
        return "{}"
    parts = []
    for statement_type, concepts in list(mapping.items())[:limit]:
        names = ", ".join(str(c) for c in (concepts or [])[:limit])
        parts.append(f"{statement_type}: [{names}]")
    return "{" + "; ".join(parts) + "}"


def format_step_line(node_name: str, state: dict) -> Optional[str]:
    """Return the summary line(s) for a finished node, or ``None``.

    Multi-line strings are allowed (each line is printed separately).
    """
    status = str(state.get("status") or "?")

    if node_name == "normalize_bundle_node":
        if status in ("failed", "skipped"):
            return f"{_prefix('normalize')}✗ {(state.get('error') or 'no bundles')[:70]}"
        bundles = state.get("bundles") or []
        types = ", ".join(str(getattr(b, "statement_type", "?")) for b in bundles)
        return (
            f"{_prefix('normalize')}{len(state.get('statement_docs') or [])} statement doc(s) "
            f"→ {len(bundles)} bundle(s)" + (f"  ({types})" if types else "")
        )

    if node_name in ("validate_node", "validate_after_repair_node", "validate_final_node"):
        report = state.get("validation_report") or {}
        findings = state.get("findings") or []
        blocking = report.get("blocking_count", 0)
        checks = report.get("checks_run") or {}
        label = {
            "validate_node": "validate",
            "validate_after_repair_node": "re-validate",
            "validate_final_node": "final validate",
        }[node_name]
        verdict = report.get("status") or status
        head = (
            _prefix(label)
            + f"{verdict}  ·  {len(findings)} finding(s) ({blocking} blocking)"
            + (f"  ·  checks {checks}" if checks else "")
        )
        lines = [head]
        for finding in findings[:5]:
            where = finding.get("statement_type") or "filing"
            concept = finding.get("concept")
            lines.append(
                f"  │  {finding.get('severity'):>6}  {finding.get('type')}"
                f"  ({where}{'/' + str(concept) if concept else ''})"
                f"  — {_clip(str(finding.get('message')), 110)}"
            )
        if len(findings) > 5:
            lines.append(f"  │  … {len(findings) - 5} more finding(s)")
        return "\n".join(lines)

    if node_name == "agent_review_node":
        decisions = state.get("repair_decisions") or []
        if not decisions:
            return f"{_prefix('review')}no review needed"
        return f"{_prefix('review')}{len(decisions)} proposal(s) from the review agent"

    if node_name == "repair_node":
        actions = state.get("repair_actions") or {}
        proposed = actions.get("proposed", 0)
        applied = actions.get("applied", 0)
        rejected = actions.get("rejected") or []
        lines = [f"{_prefix('repair')}applied {applied}/{proposed} proposal(s)"]
        for item in (actions.get("applied_details") or [])[:5]:
            lines.append(
                f"  │  {item.get('concept')}  {_money(item.get('old_value'))} → "
                f"{_money(item.get('new_value'))}  ({item.get('source')})"
            )
        for item in rejected[:3]:
            lines.append(f"  │  rejected: {str(item.get('reason'))[:70]}")
        if not proposed:
            lines[0] = f"{_prefix('repair')}nothing to repair"
        return "\n".join(lines)

    if node_name == "hierarchy_agent_node":
        plan = state.get("hierarchy_plan") or {}
        seeded = plan.get("seeded_statement_types") or []
        updates = plan.get("existing_updates") or []
        line = (
            f"{_prefix('hierarchy')}{plan.get('resolved_concepts', 0)} concept(s) decided"
            f"  ·  by {plan.get('decided_by') or 'agent'}"
        )
        extra = []
        if seeded:
            extra.append(f"  │  seeded hierarchy for: {', '.join(seeded)}")
        if updates:
            extra.append(f"  │  re-pathed {len(updates)} existing row(s)")
        return line + ("\n" + "\n".join(extra) if extra else "")
    if node_name == "resolve_hierarchy_node":
        plan = state.get("hierarchy_plan") or {}
        seeded = plan.get("seeded_statement_types") or []
        sources = plan.get("sources") or {}
        conflicts = plan.get("conflicts") or []
        updates = plan.get("existing_updates") or []
        line = (
            f"{_prefix('hierarchy')}{plan.get('resolved_concepts', 0)} concept(s) resolved"
            f"  ·  sources {sources or '{}'}"
        )
        extra = []
        if seeded:
            extra.append(f"  │  seeded hierarchy for: {', '.join(seeded)}")
        if updates:
            extra.append(f"  │  re-pathed {len(updates)} existing row(s)")
        if conflicts:
            extra.append(f"  │  {len(conflicts)} path conflict(s) resolved")
        return "\n".join([line] + extra)

    if node_name == "sign_fix_node":
        fixes = state.get("sign_fixes") or []
        unresolved = state.get("sign_fix_unresolved") or []
        if not fixes and not unresolved:
            return f"{_prefix('signs')}sign conventions already satisfied"
        lines = [f"{_prefix('signs')}{len(fixes)} sign(s) fixed"]
        for fix in fixes[:5]:
            lines.append(
                f"  │  {fix.get('statement_type')} {fix.get('concept')}  "
                f"{_money(fix.get('old_value'))} → {_money(fix.get('new_value'))}  "
                f"({fix.get('required_sign')})"
            )
        if unresolved:
            lines.append(f"  │  ⚠ {len(unresolved)} unresolved sign violation(s)")
        return "\n".join(lines)

    if node_name == "decide_node":
        decision = state.get("decision") or {}
        action = decision.get("action") or "?"
        by = decision.get("decided_by") or "?"
        lines = [
            f"{_prefix('decide')}{action.upper()} by {by} — "
            f"writing {decision.get('statements') or 'nothing'}"
        ]
        lines.append(
            f"  │  permitted={decision.get('permitted') or '[]'}"
            f"  blocked={decision.get('blocked') or '[]'}"
        )
        if decision.get("drop_concepts"):
            lines.append(f"  │  excluded {_fmt_concepts(decision.get('drop_concepts'))}")
        if decision.get("reason"):
            lines.append(f"  │  reason: {str(decision['reason'])[:100]}")
        return "\n".join(lines)

    if node_name == "persist_node":
        receipt = state.get("persist_receipt") or {}
        marker = "✓" if status == "saved" else "✗"
        if status in ("failed", "skipped"):
            why = receipt.get("decision_reason") or state.get("error") or status
            return f"{_prefix('persist')}{marker} {status} — {str(why)[:90]}"
        line = (
            f"{_prefix('persist')}{marker} wrote {receipt.get('statements_written')}"
            f"/{receipt.get('statements_total')} statement(s)"
            f"  ({receipt.get('action')} by {receipt.get('decided_by')})"
        )
        extra = []
        skipped = receipt.get("statements_skipped") or []
        if skipped:
            extra.append(f"  │  not written: {skipped}")
        if receipt.get("dropped_concepts"):
            extra.append(f"  │  excluded {_fmt_concepts(receipt.get('dropped_concepts'))}")
        return "\n".join([line] + extra)

    if node_name == "extract_guidance_node":
        info = state.get("guidance_extract") or {}
        if info.get("status") == "no_mda_text":
            return f"{_prefix('guidance')}no MD&A text available — skipped"
        if info.get("status") == "disabled":
            return f"{_prefix('guidance')}disabled"
        return (
            f"{_prefix('guidance')}{info.get('normalized', 0)} record(s) extracted"
            f"  ({info.get('dropped', 0)} dropped, currency {info.get('currency')})"
        )

    if node_name == "save_guidance_node":
        info = state.get("guidance_save") or {}
        if info.get("status") != "saved":
            return f"{_prefix('guidance')}{info.get('status', '?')}"
        score = info.get("score") or {}
        return (
            f"{_prefix('guidance')}upserted {info.get('upserted', 0)}"
            f"  ·  demoted {info.get('demoted', 0)}"
            f"  ·  scored {score.get('scored', 0)}"
        )

    if node_name == "quarterly_deaccumulation_node":
        info = state.get("deaccumulation") or {}
        return (
            f"{_prefix('quarterly')}{info.get('status', '?')}"
            f"  ({info.get('statements', 0)} statement(s))"
        )

    if node_name == "companyfacts_reconciliation_node":
        info = state.get("reconciliation") or {}
        return (
            f"{_prefix('companyfacts')}{info.get('status', '?')}"
            f"  ·  filled {info.get('filled', 0)} gap value(s)"
        )

    if node_name == "finalize_validation_reports_node":
        info = state.get("report_finalization") or {}
        return (
            f"{_prefix('reports')}{info.get('status', '?')}"
            f"  ·  finalized {info.get('finalized', 0)} report(s)"
        )

    return None


_console = None


def _default_writer(message: str) -> None:
    """Print a step line, preferring Rich (markup disabled: our tags are literal)."""
    global _console
    try:
        if _console is None:
            from rich.console import Console

            _console = Console(highlight=False, soft_wrap=True)
        _console.print(message, markup=False)
        return
    except Exception:  # noqa: BLE001 — Rich is optional at runtime
        pass
    try:
        from tqdm import tqdm as _tqdm

        _tqdm.write(message)
    except Exception:  # noqa: BLE001
        print(message, file=sys.stdout, flush=True)


class StepPresenter:
    """Streams filing progress to the terminal.

    **Quiet mode (default).**  Nothing is printed while a filing is processed:
    the *live* status lives in the progress bar's description (``AAPL  10-Q
    ▶validate  ✓norm``), so the operator sees exactly what is happening without
    a line per node.  Exactly one line is printed per filing — its outcome —
    plus detail lines only for genuine anomalies (blocking findings, repairs,
    a refusal, guidance extracted, company-stage work).  Medium/low findings are
    counted, never dumped.

    **Detailed mode** (``detailed=True``, used by ``-v``) restores the full
    per-node trace for debugging.
    """

    def __init__(
        self,
        *,
        writer: Optional[Callable[[str], None]] = None,
        stage_writer: Optional[Callable[[str], None]] = None,
        detailed: bool = False,
        show_progress_line: bool = False,
    ):
        self._writer = writer or _default_writer
        self._stage_writer = stage_writer
        self.detailed = detailed
        self._show_progress_line = show_progress_line
        self._completed: list[tuple[str, bool]] = []
        self._current: str = ""
        self._filing: dict = {}
        self._filing_key: Optional[tuple] = None
        self._steps: list[str] = []
        self._filed_at: Optional[float] = None
        self._last_verdict: Optional[str] = None
        self._lock = threading.Lock()
        self._write_failures = 0
        self._attached = False

    # ── output ─────────────────────────────────────────────────────────────
    def write(self, message: str) -> None:
        try:
            self._writer(message)
        except Exception as exc:  # noqa: BLE001 — presentation must never break a run
            self._write_failures += 1
            if self._write_failures == 1:
                logger.warning("presenter writer failed: %s", exc)
            else:
                logger.debug("presenter write failed", exc_info=True)

    # ── live status (progress bar) ─────────────────────────────────────────
    @staticmethod
    def _stage_token(label: str, ok: bool) -> str:
        if label == "persist" and not ok:
            return "skip"
        return _SHORT_STAGE.get(label, label[:5])

    def _stage_text(self, ticker: str) -> str:
        """One-line live status: ``AAPL  10-Q  ✓norm ✓valid ▶hierarchy``."""
        filing = self._filing or {}
        head = (ticker or filing.get("ticker") or filing.get("cik") or "?")
        parts = [f"{head:<6}"]
        form = filing.get("form_type")
        if form:
            parts.append(f"{str(form):<5}")
        with self._lock:
            trail = [
                f"{'✓' if ok else '✗'}{self._stage_token(label, ok)}"
                for label, ok in self._completed[-4:]
            ]
            current = self._current
        if trail:
            parts.append(" ".join(trail))
        if current:
            parts.append(f"▶{current}")
        return " ".join(parts)

    def _update_stage(self, ticker: str) -> None:
        if self._stage_writer is None:
            return
        try:
            self._stage_writer(self._stage_text(ticker))
        except Exception:  # noqa: BLE001
            logger.debug("stage writer failed", exc_info=True)

    def _progress_line(self, ticker: str) -> str:
        done = " ".join(
            f"{'✓' if ok else '✗'}{self._stage_token(label, ok)}"
            for label, ok in self._completed
        )
        return f"  {ticker:<6} {done}" if done else f"  {ticker:<6}"

    # ── hooks ──────────────────────────────────────────────────────────────
    def node_callback(
        self,
        node_name: str,
        event: str,
        ticker: str = "",
        node_state: Any = None,
        elapsed_ms: Optional[float] = None,
    ) -> None:
        label = NODE_LABELS.get(node_name, node_name.replace("_node", "").replace("_", " "))

        if event == "start":
            with self._lock:
                if node_name == "normalize_bundle_node":
                    self._completed = []
                    if isinstance(node_state, dict):
                        self._filing = node_state
                self._current = label
            if self.detailed:
                if node_name == "normalize_bundle_node":
                    self._print_filing_header(node_state, ticker)
                self.write(f"  ▶ {label}")
            self._update_stage(ticker)
            return

        if event != "end" or node_state is None:
            return

        ok = str(node_state.get("status") or "") not in ("failed", "skipped")
        with self._lock:
            if all(existing != label for existing, _ in self._completed):
                self._completed.append((label, ok))
            self._current = ""
        self._update_stage(ticker)

        if self.detailed:
            if node_name in _FILING_STAGES and self._show_progress_line:
                self.write(self._progress_line(ticker or "?"))
            summary = format_step_line(node_name, node_state)
            if summary:
                duration = _fmt_dur(elapsed_ms)
                lines = summary.split("\n")
                for index, line in enumerate(lines):
                    suffix = f"   {duration}" if (duration and index == len(lines) - 1) else ""
                    self.write(f"{line}{suffix}")
            return

        # Step list: one line per main task, closed by a result line.
        self._emit_step(node_name, node_state, elapsed_ms)
        if node_name == "persist_node" and str(node_state.get("status")) != "saved":
            self._emit_result(node_state)
        elif node_name == "save_guidance_node":
            self._emit_result(node_state)

    # ── step list (one line per main task) ─────────────────────────────────
    def step(
        self,
        name: str,
        detail: str = "",
        *,
        ok: Any = True,
        duration_ms: Optional[float] = None,
        indent: int = 4,
    ) -> None:
        """Print one pipeline step line, e.g. ``  extract  ✓  3 statements  3.2s``.

        ``ok`` may be ``True`` (✓), ``False`` (✗) or ``None`` (–, skipped).
        """
        mark = "✓" if ok is True else ("✗" if ok is False else "–")
        line = f"{' ' * indent}{name:<13} {mark}"
        if detail:
            line += f"  {detail}"
        # Durations under 50 ms are noise — only meaningful time is shown.
        if duration_ms is not None and duration_ms >= 50:
            line += f"   {_fmt_dur(duration_ms)}"
        self.write(line)

    def filing_header(self, state: Any, period_label: str = "") -> None:
        """Print the per-filing header that the step list hangs under."""
        if not isinstance(state, dict):
            return
        bits = [
            str(state.get("ticker") or state.get("cik") or "?"),
            str(period_label or "").strip() or str(state.get("form_type") or ""),
        ]
        form = str(state.get("form_type") or "")
        if period_label and form:
            bits.append(form)
        accession = state.get("accession_number")
        if accession:
            bits.append(str(accession))
        title = "  ── " + "  ·  ".join(b for b in bits if b) + " "
        self.write("")
        self.write(title + "─" * max(4, 78 - len(title)))
        with self._lock:
            self._filing_key = (state.get("cik"), accession)
            self._steps = []
            self._filed_at = time.perf_counter()
            self._last_verdict = None

    def _step_detail(self, node_name: str, state: dict) -> tuple[Optional[str], Optional[bool]]:
        """Return ``(detail, ok)`` for a finished node, or ``(None, ok)`` to skip."""
        status = str(state.get("status") or "")
        ok = status not in ("failed", "skipped")

        if node_name == "normalize_bundle_node":
            bundles = state.get("bundles") or []
            types = ", ".join(
                str(getattr(b, "statement_type", "") or "")
                for b in bundles
                if getattr(b, "statement_type", None)
            )
            return f"{_plural(len(bundles), 'bundle')}" + (f" ({types})" if types else ""), ok

        if node_name in ("validate_node", "validate_after_repair_node", "validate_final_node"):
            report = state.get("validation_report") or {}
            findings = state.get("findings") or []
            blocking = report.get("blocking_count", 0)
            verdict = report.get("status") or status or "?"
            bits = [verdict]
            if blocking:
                bits.append(_plural(int(blocking), "blocking finding"))
            elif findings:
                bits.append(_plural(len(findings), "finding"))
            detail = "  ·  ".join(bits)
            # Don't repeat an unchanged verdict from the previous validate step.
            with self._lock:
                if detail == self._last_verdict:
                    return None, ok
                self._last_verdict = detail
            return detail, ok

        if node_name == "agent_review_node":
            decisions = state.get("repair_decisions") or []
            if not decisions:
                return None, ok
            return _plural(len(decisions), "proposal"), ok

        if node_name == "repair_node":
            actions = state.get("repair_actions") or {}
            proposed = actions.get("proposed", 0)
            if not proposed:
                return None, ok
            applied = actions.get("applied", 0)
            return f"applied {applied}/{proposed}", ok

        if node_name == "sign_fix_node":
            fixes = state.get("sign_fixes") or []
            unresolved = state.get("sign_fix_unresolved") or []
            if not fixes and not unresolved:
                return None, ok          # nothing to say
            if unresolved:
                return f"{len(fixes)} fixed · ⚠ {len(unresolved)} unresolved", False
            return _plural(len(fixes), "sign") + " fixed", ok

        if node_name == "hierarchy_agent_node":
            plan = state.get("hierarchy_plan") or {}
            bits = [f"{_plural(int(plan.get('resolved_concepts', 0) or 0), 'concept')} placed"]
            if plan.get("seeded_statement_types"):
                bits.append("seeded")
            bits.append(f"by {plan.get('decided_by') or 'agent'}")
            repathed = len(plan.get("existing_updates") or [])
            if repathed:
                bits.append(f"{_plural(repathed, 'row')} re-pathed")
            return "  ·  ".join(bits), ok
        if node_name == "resolve_hierarchy_node":
            plan = state.get("hierarchy_plan") or {}
            sources = plan.get("sources") or {}
            bits = [f"{_plural(int(plan.get('resolved_concepts', 0) or 0), 'concept')} placed"]
            bits.append("seeded" if sources.get("fresh_seed") else "reused")
            if plan.get("decided_by"):
                bits.append(f"by {plan['decided_by']}")
            hidden = sum(
                int((p.get("hidden") or 0)) for p in (plan.get("agent_plans") or [])
            )
            if hidden:
                bits.append(f"{_plural(hidden, 'row')} hidden")
            if plan.get("max_depth"):
                bits.append(f"depth {plan['max_depth']}")
            repathed = len(plan.get("existing_updates") or [])
            if repathed:
                bits.append(f"{_plural(repathed, 'row')} re-pathed")
            conflicts = len(plan.get("conflicts") or [])
            if conflicts:
                bits.append(f"{_plural(conflicts, 'conflict')} resolved")
            integrity = plan.get("integrity") or {}
            problems = []
            if integrity.get("duplicate_paths"):
                problems.append(f"{_plural(integrity['duplicate_paths'], 'duplicate path')}")
            if integrity.get("orphans"):
                problems.append(f"{_plural(integrity['orphans'], 'orphan')}")
            if problems:
                bits.append("⚠ " + ", ".join(problems))
            return "  ·  ".join(bits), ok

        if node_name == "hierarchy_review_node":
            plan = state.get("hierarchy_plan") or {}
            decided_by = plan.get("decided_by")
            agent_plans = plan.get("agent_plans") or []
            if not agent_plans and decided_by != "agent":
                return None, ok
            total_placed = sum(p.get("placed", 0) for p in agent_plans)
            bits = [f"{_plural(len(agent_plans), 'statement')} reviewed by agent"]
            if total_placed:
                bits.append(f"{total_placed} placed")
            return "  ·  ".join(bits), ok

        if node_name == "decide_node":
            decision = state.get("decision") or {}
            action = str(decision.get("action") or "?")
            statements = decision.get("statements") or []
            if action == "skip" or not statements:
                return "skip — nothing written", False
            if action == "write":
                return "write all permitted", True
            return f"write {statements}", True

        if node_name == "persist_node":
            receipt = state.get("persist_receipt") or {}
            if status != "saved":
                reason = receipt.get("decision_reason") or state.get("error") or status
                return _clip(reason, 70), False
            detail = (
                f"{receipt.get('statements_written')}/{receipt.get('statements_total')} "
                f"statements written"
            )
            dropped = receipt.get("dropped_concepts") or {}
            if dropped:
                detail += f"  ·  {_plural(sum(len(v) for v in dropped.values()), 'concept')} excluded"
            return detail, True

        if node_name == "extract_guidance_node":
            info = state.get("guidance_extract") or {}
            kind = info.get("status")
            if kind == "disabled":
                return "disabled", None
            if kind == "no_mda_text":
                return "no MD&A text available", None
            found = int(info.get("normalized", 0) or 0)
            if found <= 0:
                return "no forward-looking guidance found", None
            return _plural(found, "record"), ok

        if node_name == "save_guidance_node":
            saved = state.get("guidance_save") or {}
            upserted = int(saved.get("upserted", 0) or 0)
            if saved.get("status") != "saved" or upserted <= 0:
                return None, ok          # nothing stored -> stays silent
            score = saved.get("score") or {}
            detail = f"{_plural(upserted, 'record')} saved"
            if score.get("scored"):
                detail += f"  ·  scored {score['scored']}"
            return detail, ok

        if node_name == "quarterly_deaccumulation_node":
            info = state.get("deaccumulation") or {}
            if info.get("status") != "ok":
                return None, ok
            return f"{_plural(int(info.get('statements', 0) or 0), 'statement')} deaccumulated", ok

        if node_name == "companyfacts_reconciliation_node":
            info = state.get("reconciliation") or {}
            if not info.get("filled"):
                return None, ok
            return f"{_plural(int(info['filled']), 'gap value')} filled", ok

        if node_name == "finalize_validation_reports_node":
            info = state.get("report_finalization") or {}
            if not info.get("finalized"):
                return None, ok
            return f"{_plural(int(info['finalized']), 'report')} finalized", ok

        return None, ok

    def _emit_step(self, node_name: str, state: dict, elapsed_ms: Optional[float]) -> None:
        name = STEP_LABELS.get(node_name)
        if name is None:
            return
        detail, ok = self._step_detail(node_name, state)
        if detail is None and ok is not False:
            return          # nothing worth reporting (already covered)
        self.step(name, detail or "", ok=ok, duration_ms=elapsed_ms)
        with self._lock:
            self._steps.append(name)

        # Detail lines belong to the step that produced them:
        #   * a validation step explains its blocking findings,
        #   * the repair step explains what it changed.
        if node_name in ("validate_node", "validate_after_repair_node", "validate_final_node"):
            for finding in [
                f for f in (state.get("findings") or []) if f.get("severity") == "high"
            ][:3]:
                where = finding.get("statement_type") or "filing"
                concept = finding.get("concept")
                self.write(
                    f"{' ' * 6}{finding.get('severity')}  {finding.get('type')}"
                    f"  ({where}{'/' + str(concept) if concept else ''})"
                    f"  — {_clip(finding.get('message'), 64)}"
                )
        elif node_name == "repair_node":
            for item in ((state.get("repair_actions") or {}).get("applied_details") or [])[:3]:
                self.write(
                    f"{' ' * 6}repaired  {item.get('concept')}  "
                    f"{_money(item.get('old_value'))} → {_money(item.get('new_value'))}"
                    f"  ({item.get('source')})"
                )

    def _emit_result(self, state: dict) -> None:
        """Close a filing with a single result line and total duration."""
        status = str(state.get("status") or "?")
        with self._lock:
            started = self._filed_at
        total = (time.perf_counter() - started) * 1000 if started else None
        mark = {"saved": "✓", "skipped": "–"}.get(status, "✗")
        label = {"saved": "written", "skipped": "not written"}.get(status, status)
        line = f"  {mark} {label}"
        if total is not None and total >= 50:
            line += f"   {_fmt_dur(total)}"
        self.write(line)

    def _print_filing_header(self, state: Any, ticker: str) -> None:
        if not isinstance(state, dict):
            return
        key = (state.get("cik"), state.get("accession_number"))
        with self._lock:
            if key == self._filing_key:
                return
            self._filing_key = key
        parts = [str(state.get("ticker") or ticker or state.get("cik") or "?")]
        if state.get("form_type"):
            parts.append(str(state["form_type"]))
        if state.get("accession_number"):
            parts.append(str(state["accession_number"]))
        title = "  ── " + "  ·  ".join(parts) + " "
        self.write("")
        self.write(title + "─" * max(4, 78 - len(title)))

    def call_callback(self, message: str) -> None:
        text = str(message or "").rstrip()
        if text:
            self.write(text)

    def detail_callback(self, detail: str) -> None:
        """Update live status in the filing bar when an agent step/tool runs."""
        text = str(detail or "").strip()
        if not text or self._stage_writer is None:
            return
        with self._lock:
            filing = self._filing or {}
            ticker = filing.get("ticker") or filing.get("cik") or "?"
            form = filing.get("form_type") or ""
            current = self._current or "agent"
            short_cur = _SHORT_STAGE.get(current, current[:4])
            trail = [
                f"{'✓' if ok else '✗'}{self._stage_token(label, ok)}"
                for label, ok in self._completed[-3:]
            ]
        trail_str = (" " + " ".join(trail)) if trail else ""
        stage_desc = f"{ticker:<6} {str(form):<5}{trail_str} ▶{short_cur}: {text}"
        try:
            self._stage_writer(stage_desc)
        except Exception:  # noqa: BLE001
            pass

    def close(self, summary: str = "") -> None:
        if summary:
            self.write(summary)

    # ── installation ───────────────────────────────────────────────────────
    def attach(self) -> "StepPresenter":
        """Compose onto the current hook callbacks."""
        from .hooks import (
            get_call_callback,
            get_detail_callback,
            get_node_callback,
            set_call_callback,
            set_detail_callback,
            set_node_callback,
        )

        previous_node = get_node_callback()
        previous_call = get_call_callback()
        previous_detail = get_detail_callback()

        def _node(*args: Any, **kwargs: Any) -> None:
            self.node_callback(*args, **kwargs)
            if previous_node is not None:
                previous_node(*args, **kwargs)

        def _call(message: str) -> None:
            self.call_callback(message)
            if previous_call is not None:
                previous_call(message)

        def _detail(detail: str) -> None:
            self.detail_callback(detail)
            if previous_detail is not None:
                previous_detail(detail)

        set_node_callback(_node)
        set_call_callback(_call)
        set_detail_callback(_detail)
        self._attached = True
        return self

    def detach(self) -> None:
        from .hooks import set_call_callback, set_detail_callback, set_node_callback

        if self._attached:
            set_node_callback(None)
            set_call_callback(None)
            set_detail_callback(None)
            self._attached = False


def install_presenter(
    *,
    writer: Optional[Callable[[str], None]] = None,
    stage_writer: Optional[Callable[[str], None]] = None,
    detailed: Optional[bool] = None,
    enabled: bool = True,
) -> Optional[StepPresenter]:
    """Install the terminal presenter (no-op when *enabled* is False).

    *detailed* defaults to the ``AGENT_STEPS`` env var (``1`` = full per-node
    trace), so ``AGENT_STEPS=1`` reproduces the verbose narration on demand.
    """
    if not enabled:
        return None
    if detailed is None:
        import os

        detailed = os.getenv("AGENT_STEPS", "0").strip().lower() in {"1", "true", "yes", "on"}
    return StepPresenter(
        writer=writer, stage_writer=stage_writer, detailed=detailed
    ).attach()
