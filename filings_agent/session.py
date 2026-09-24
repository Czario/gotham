"""Durable filing-run session (P7).

A JSONL ledger of what a run did — one record per filing, plus per-company
summaries — tagged with a ``run_id`` so runs are attributable and resumable.

Two uses:

* **resume** — accessions whose latest record is ``saved`` are skipped on a
  later run (``is_saved``), and
* **auditability** — a durable, greppable record of every outcome.

The real scraper keeps its authoritative de-duplication in MongoDB (the
existing-filing check); the session ledger is what makes the *headless batch*
resumable and gives every run a durable summary.  Writes are lock-protected
because companies can be processed in parallel threads.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = ".filings_agent"


def default_session_path() -> str:
    return str(Path(os.getenv("AGENT_STATE_DIR") or DEFAULT_STATE_DIR) / "session.jsonl")


class RunSession:
    """Thread-safe JSONL ledger of filing/company outcomes."""

    def __init__(self, path: str | Path):
        self.run_id = uuid.uuid4().hex
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._index: Optional[dict[str, str]] = None

    # ── writing ────────────────────────────────────────────────────────────
    def record(self, event: str, **fields: Any) -> dict:
        entry = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
            **fields,
        }
        line = json.dumps(entry, default=str, ensure_ascii=False)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:  # session logging is best-effort
                logger.warning("session write failed (%s): %s", self.path, exc)
            accession = entry.get("accession_number")
            if accession and entry.get("status") and self._index is not None:
                self._index[str(accession)] = str(entry["status"])
        return entry

    def record_filing(
        self,
        *,
        cik: str,
        accession_number: Optional[str],
        status: str,
        **fields: Any,
    ) -> dict:
        return self.record(
            "filing",
            cik=str(cik) if cik is not None else None,
            accession_number=accession_number,
            status=status,
            **fields,
        )

    def record_company(self, *, cik: str, ticker: str = "", **fields: Any) -> dict:
        return self.record("company", cik=str(cik), ticker=ticker, **fields)

    # ── reading ────────────────────────────────────────────────────────────
    def records(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def _ensure_index(self) -> dict[str, str]:
        if self._index is None:
            index: dict[str, str] = {}
            for rec in self.records():
                accession = rec.get("accession_number")
                status = rec.get("status")
                if accession and status:
                    index[str(accession)] = str(status)
            self._index = index
        return self._index

    def saved_accessions(self) -> set[str]:
        """Accessions whose latest recorded status is ``saved``."""
        return {a for a, status in self._ensure_index().items() if status == "saved"}

    def is_saved(self, accession_number: Optional[str]) -> bool:
        if not accession_number:
            return False
        return self._ensure_index().get(str(accession_number)) == "saved"

    def latest_status(self, accession_number: Optional[str]) -> Optional[str]:
        if not accession_number:
            return None
        return self._ensure_index().get(str(accession_number))

    def summary(self) -> dict:
        """Counts by status plus the company records."""
        counts: dict[str, int] = {}
        companies: list[dict] = []
        for rec in self.records():
            if rec.get("event") == "company":
                companies.append(rec)
                continue
            if rec.get("event") != "filing":
                continue
            key = str(rec.get("status") or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return {
            "run_id": self.run_id,
            "path": str(self.path),
            "filings": sum(counts.values()),
            "counts": counts,
            "companies": companies,
        }


def build_session(path: Optional[str] = None) -> RunSession:
    """Build the durable run session (P7).

    Always returns a session: ``AGENT_SESSION_FILE`` overrides the default
    ``.filings_agent/session.jsonl`` location so runs are resumable by default.
    """
    target = path or os.getenv("AGENT_SESSION_FILE") or default_session_path()
    return RunSession(target)


def build_run_session(path: Optional[str] = None) -> Optional[RunSession]:
    """Opt-in variant: ``None`` when no session path is configured."""
    target = path or os.getenv("AGENT_SESSION_FILE")
    if not target:
        return None
    try:
        return RunSession(target)
    except OSError as exc:
        logger.warning("could not create run session %s: %s", target, exc)
        return None
