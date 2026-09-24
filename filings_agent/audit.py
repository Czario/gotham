"""Append-only JSONL audit trail for agent runs (P7).

Reuses the existing hook layer rather than adding new instrumentation: it
subscribes to the node lifecycle events emitted by ``with_hooks`` and to the
LLM/tool call events emitted by ``report_call`` (which the agent loop and the
services already call).  One JSON object per line.

Callbacks are COMPOSED, not replaced, so attaching an audit log does not steal
the CLI/worker progress callbacks.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AuditLog:
    """Thread-safe JSONL writer for run events."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._attached = False

    # ── writing ────────────────────────────────────────────────────────────
    def emit(self, event: str, **fields: Any) -> dict:
        record = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:  # audit is best-effort
                logger.warning("audit write failed (%s): %s", self.path, exc)
        return record

    # ── hook adapters ──────────────────────────────────────────────────────
    def node_callback(self, node_name, event, ticker, node_state=None, elapsed_ms=None) -> None:
        fields: dict[str, Any] = {
            "node": node_name,
            "phase": event,
            "ticker": ticker,
        }
        if elapsed_ms is not None:
            fields["elapsed_ms"] = round(float(elapsed_ms), 1)
        if isinstance(node_state, dict):
            fields["status"] = node_state.get("status")
            if node_state.get("error"):
                fields["error"] = str(node_state["error"])[:500]
        self.emit("node", **fields)

    def call_callback(self, msg) -> None:
        text = str(msg or "").strip()
        if text:
            self.emit("call", message=text)

    def agent_event(self, event: Any, **fields: Any) -> Optional[dict]:
        """Structured sink used by the graphs (``on_event``).

        Accepts either a single event dict (the shape the agent loop emits:
        ``on_event({"event": "llm_step", ...})``) or ``(event, **fields)``.
        Never raises: an audit failure must not break a filing run.
        """
        try:
            if isinstance(event, dict):
                record = dict(event)
                name = str(record.pop("event", "agent"))
                return self.emit(name, **record)
            return self.emit(str(event), **fields)
        except Exception as exc:  # noqa: BLE001
            logger.debug("audit event failed: %s", exc)
            return None

    def attach(self) -> "AuditLog":
        """Compose this audit log onto the current hook callbacks."""
        from .hooks import (
            get_call_callback,
            get_node_callback,
            set_call_callback,
            set_node_callback,
        )

        previous_node = get_node_callback()
        previous_call = get_call_callback()

        def _node(*args, **kwargs):
            self.node_callback(*args, **kwargs)
            if previous_node is not None:
                previous_node(*args, **kwargs)

        def _call(msg):
            self.call_callback(msg)
            if previous_call is not None:
                previous_call(msg)

        set_node_callback(_node)
        set_call_callback(_call)
        self._attached = True
        return self

    def detach(self) -> None:
        from .hooks import set_call_callback, set_node_callback

        if self._attached:
            set_node_callback(None)
            set_call_callback(None)
            self._attached = False

    # ── reading (tests / summaries) ────────────────────────────────────────
    def records(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def build_audit_log(path: Optional[str] = None) -> Optional[AuditLog]:
    """Opt-in audit log: ``None`` unless a path is given/configured."""
    target = path or os.getenv("AGENT_AUDIT_LOG")
    if not target:
        return None
    try:
        return AuditLog(target)
    except OSError as exc:
        logger.warning("could not create audit log %s: %s", target, exc)
        return None


def default_audit_path() -> str:
    state_dir = os.getenv("AGENT_STATE_DIR") or ".filings_agent"
    return str(Path(state_dir) / "audit.jsonl")


def install_audit(path: Optional[str] = None) -> Optional[AuditLog]:
    """Install the JSONL audit trail and attach it to the hook layer.

    Enabled by default (the plan calls for a JSONL audit of every node and
    tool call); set ``AGENT_AUDIT_ENABLED=0`` to disable, or ``AGENT_AUDIT_LOG``
    to relocate the file.  Returns ``None`` when disabled.
    """
    if os.getenv("AGENT_AUDIT_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    target = path or os.getenv("AGENT_AUDIT_LOG") or default_audit_path()
    try:
        log = AuditLog(target)
    except OSError as exc:
        logger.warning("could not create audit log %s: %s", target, exc)
        return None
    return log.attach()
