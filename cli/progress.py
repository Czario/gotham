"""Rich progress bars and processing summary logger for the SEC scraper CLI."""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Column, Table
from rich.text import Text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Progress display (Rich)
# ---------------------------------------------------------------------------
class _RichFilingBar:
    """Adapter exposing the old ``tqdm``-style filing-bar API over a Rich task.

    Call sites use ``.update(n)``, ``.set_description_str(s)`` and ``.close()``;
    keeping those names means no changes at any of them.
    """

    def __init__(self, progress: "Progress", task_id: TaskID):
        self._progress = progress
        self._task_id = task_id
        self._closed = False

    def update(self, n: int = 1) -> None:
        if not self._closed:
            self._progress.update(self._task_id, advance=n)

    def set_description_str(self, description: str) -> None:
        if not self._closed:
            self._progress.update(self._task_id, description=description)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._progress.update(self._task_id, visible=False)
            except Exception:  # noqa: BLE001 — bar teardown must never fail
                pass


class _NullFilingBar:
    """No-op filing bar (parallel mode)."""

    def update(self, n: int = 1) -> None: ...
    def set_description_str(self, description: str) -> None: ...
    def close(self) -> None: ...


class ProgressManager:
    """Rich-powered progress bars + a clean step-by-step console.

    The public API is unchanged from the previous tqdm implementation
    (``status`` / ``set_status`` / ``error`` / ``label`` / ``start_companies`` /
    ``set_current_company`` / ``advance_company`` / ``finish_companies`` /
    ``filing_bar`` / ``close_filing_bar`` / ``close_all``) so every existing
    call site — including the Redis worker's ``WorkerProgressManager`` subclass —
    keeps working.

    Rich renders the live progress region and reprints scrolled step lines
    above it, so narration and bars never corrupt each other (the tqdm version
    produced overwritten, wrapped lines).

    In verbose mode the live bars are disabled and full log output flows to the
    console; step narration is still printed.
    """

    def __init__(self, verbose: bool = False, parallel: bool = False,
                 console: Optional[Console] = None):
        self.verbose = verbose
        self.parallel = parallel
        # ``console`` is injectable so the display can be captured in tests.
        self.console = console or Console(highlight=False, soft_wrap=True)
        self._progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn(
                "{task.description}",
                style="bold",
                markup=False,
                # Keep the live line to ONE row: a wrapped description would
                # corrupt the progress region.
                table_column=Column(no_wrap=True, overflow="ellipsis"),
            ),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=self.console,
            transient=False,
            disable=verbose,
        )
        self._started = False
        self._company_task: Optional[TaskID] = None
        self._filing_task: Optional[TaskID] = None
        self._filing_bar = None
        self._current_index: int = 0
        self._lock = threading.Lock()
        # Maps CIK -> the ticker symbol the user actually typed, so the bar
        # shows e.g. "JPM" instead of whatever the reverse CIK->ticker map
        # happens to return (a CIK can map to multiple tickers).
        self.cik_labels: dict = {}

    # -- output -------------------------------------------------------------
    def write(self, message: str) -> None:
        """Print one line above the live progress region.

        ``markup=False`` is essential: our step lines start with literal
        ``[normalize]`` / ``[llm]`` / ``[tool]`` tags, which Rich would
        otherwise swallow as style markup.
        """
        self.console.print(message, markup=False)

    def label(self, cik: str, fallback: str) -> str:
        """Return the user-supplied ticker for a CIK, else the fallback."""
        if not cik:
            return fallback
        return (self.cik_labels.get(cik)
                or self.cik_labels.get(str(cik).zfill(10))
                or self.cik_labels.get(str(cik).lstrip('0'))
                or fallback)

    def summary(self, title: str, rows: list, subtitle: str = "") -> None:
        """Render a compact end-of-run panel (``("", "")`` adds a separator)."""
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column(style="bold", no_wrap=True)
        table.add_column()
        for label, value in rows:
            if label == "" and value == "":
                table.add_section()
                continue
            table.add_row(str(label), str(value))
        body: Any = table
        if subtitle:
            grid = Table.grid()
            grid.add_row(table)
            grid.add_row(f"[dim]{subtitle}[/dim]")
            body = grid
        self.console.print(Panel(body, title=title, expand=False, border_style="cyan"))

    # -- Company-level bar --------------------------------------------------
    def start_companies(self, total: int) -> None:
        if self.verbose:
            return
        with self._lock:
            if not self._started:
                self._progress.start()
                self._started = True
            self._company_task = self._progress.add_task("waiting", total=total)

    def set_current_company(self, ticker: str, index: Optional[int] = None) -> None:
        """Show the company currently being processed in the outer bar.

        The bar position reflects *completed* companies, so while company
        ``index`` is in progress the bar sits at ``index - 1``.
        """
        if self.verbose or self._company_task is None:
            return
        with self._lock:
            task = self._progress.tasks[self._company_task]
            if index is not None and not self.parallel:
                self._current_index = index
                delta = (index - 1) - task.completed
                if delta > 0:
                    self._progress.update(self._company_task, advance=delta)
            self._progress.update(
                self._company_task, description=f"{ticker:<6} processing"
            )

    def advance_company(self, ticker: str, processed: int, skipped: int,
                        failed: int, reconciled: int = 0) -> None:
        if self.verbose or self._company_task is None:
            return
        parts = []
        if processed:
            parts.append(f"{processed} new")
        if skipped:
            parts.append(f"{skipped} skip")
        if failed:
            parts.append(f"{failed} failed")
        if reconciled:
            parts.append(f"+{reconciled} filled")
        status = ", ".join(parts) or "done"
        with self._lock:
            task = self._progress.tasks[self._company_task]
            if self.parallel:
                self._progress.update(
                    self._company_task, advance=1, description=f"{ticker:<6} {status}"
                )
            else:
                delta = self._current_index - task.completed
                if delta > 0:
                    self._progress.update(self._company_task, advance=delta)
                self._progress.update(
                    self._company_task, description=f"{ticker:<6} {status}"
                )

    def finish_companies(self) -> None:
        self.close_filing_bar()
        with self._lock:
            if self._started:
                self._progress.stop()
                self._started = False

    def filing_bar(self, ticker: str, total: int):
        """Return a filing-level bar (no-op object in parallel mode)."""
        if self.parallel or self.verbose:
            return _NullFilingBar()
        self.close_filing_bar()
        with self._lock:
            if not self._started and not self.verbose:
                self._progress.start()
                self._started = True
            self._filing_task = self._progress.add_task(f"{ticker:<6}", total=total)
        self._filing_bar = _RichFilingBar(self._progress, self._filing_task)
        return self._filing_bar

    def close_filing_bar(self) -> None:
        if self._filing_bar is not None:
            self._filing_bar.close()
            self._filing_bar = None
            self._filing_task = None

    def close_all(self) -> None:
        self.close_filing_bar()
        with self._lock:
            if self._started:
                self._progress.stop()
                self._started = False
            self._company_task = None

    # -- One-line status ----------------------------------------------------
    def set_filing_status(self, msg: str) -> None:
        """Update the filing bar's description (the live per-filing stage).

        Used by the agent presenter so the operator sees the current step in
        the bar instead of a printed line per node.
        """
        if self.verbose or self._filing_task is None:
            return
        with self._lock:
            self._progress.update(self._filing_task, description=msg)

    def set_status(self, msg: str) -> None:
        """Update the transient state shown in the company bar's description."""
        if self.verbose or self._company_task is None:
            return
        with self._lock:
            self._progress.update(self._company_task, description=msg)

    def status(self, msg: str) -> None:
        """Print a step line above the live progress region.

        ``markup=False`` keeps our literal ``[tag]`` prefixes intact instead of
        letting Rich interpret them as style markup.
        """
        if self.verbose:
            return  # verbose mode: the logger owns console output
        self.console.print(msg, markup=False)

    def error(self, msg: str) -> None:
        self.console.print(Text(str(msg), style="red"))


# ---------------------------------------------------------------------------
# Merged normalization pipeline
# ---------------------------------------------------------------------------
from data_normalization_service.core.config import AppConfig as _NormAppConfig, DatabaseConfig as _NormDBConfig
from data_normalization_service.services.normalization_service import FinancialNormalizationService as _NormService
from data_normalization_service.services.quarterly_service import PeriodBasedFinancialCalculationService as _QuarterlyService

class ProcessingSummary:
    """Track processing statistics for companies and filings with real-time streaming"""
    def __init__(self, report_file: Optional[str] = None):
        self.companies_stats = {}
        self.start_time = datetime.now()
        self.report_file = report_file
        self.company_count = 0
    
    def write_header(self, total_companies: int):
        """Write header to report file at start"""
        if not self.report_file:
            return
        
        try:
            with open(self.report_file, 'w', buffering=1) as f:
                f.write("=" * 80 + "\n")
                f.write("SEC DATA SCRAPER - PROCESSING SUMMARY\n")
                f.write("=" * 80 + "\n\n")
                f.write(f"Start Time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Total Companies to Process: {total_companies}\n\n")
                f.write("PROCESSING PROGRESS\n")
                f.write("-" * 80 + "\n\n")
                f.flush()
        except Exception as e:
            logger.error(f"Failed to write header to report file: {e}")
    
    def update_company(self, cik: str, ticker: str, total_filings: int, 
                      filings_processed: int, filings_skipped: int, filings_failed: int):
        """Record and stream company stats to file"""
        self.company_count += 1
        self.companies_stats[cik] = {
            'ticker': ticker,
            'total_filings': total_filings,
            'processed': filings_processed,
            'skipped': filings_skipped,
            'failed': filings_failed,
            'status': 'success' if filings_failed == 0 else 'partial'
        }
        
        # Stream update to file with immediate flush
        if self.report_file:
            try:
                with open(self.report_file, 'a', buffering=1) as f:
                    f.write(f"[{self.company_count}] Company: {ticker} (CIK: {cik})\n")
                    f.write(f"    Total Filings: {total_filings}\n")
                    f.write(f"    Processed: {filings_processed} | Skipped: {filings_skipped} | Failed: {filings_failed}\n")
                    f.write(f"    Status: {self.companies_stats[cik]['status'].upper()}\n\n")
                    f.flush()
                    os.fsync(f.fileno())  # Force disk write
            except Exception as e:
                logger.error(f"Failed to update report file: {e}")
    
    def log_filing_progress(self, ticker: str, current_filing: int, total_filings: int, accession: str, status: str):
        """Log individual filing progress (e.g., 'processing', 'processed', 'skipped', 'failed')"""
        if not self.report_file:
            return
        
        try:
            with open(self.report_file, 'a', buffering=1) as f:
                f.write(f"      [{current_filing}/{total_filings}] {accession} - {status}\n")
                f.flush()
                os.fsync(f.fileno())  # Force disk write
        except Exception as e:
            logger.error(f"Failed to log filing progress: {e}")
    
    def write_final_summary(self):
        """Write final summary statistics at end"""
        if not self.report_file:
            return
        
        try:
            total_companies = len(self.companies_stats)
            successful_companies = sum(1 for s in self.companies_stats.values() if s['status'] == 'success')
            total_filings = sum(s['total_filings'] for s in self.companies_stats.values())
            total_processed = sum(s['processed'] for s in self.companies_stats.values())
            total_skipped = sum(s['skipped'] for s in self.companies_stats.values())
            total_failed = sum(s['failed'] for s in self.companies_stats.values())
            duration = (datetime.now() - self.start_time).total_seconds()
            
            with open(self.report_file, 'a', buffering=1) as f:
                f.write("=" * 80 + "\n")
                f.write("FINAL SUMMARY\n")
                f.write("-" * 80 + "\n")
                f.write(f"End Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Total Duration: {duration:.1f}s\n\n")
                f.write(f"Companies Processed: {total_companies}\n")
                f.write(f"  Successful: {successful_companies}\n")
                f.write(f"  With Errors: {total_companies - successful_companies}\n\n")
                f.write(f"Total Filings: {total_filings}\n")
                f.write(f"  Processed: {total_processed}\n")
                f.write(f"  Skipped: {total_skipped}\n")
                f.write(f"  Failed: {total_failed}\n\n")
                f.write("=" * 80 + "\n")
                f.flush()
                os.fsync(f.fileno())  # Force disk write
        except Exception as e:
            logger.error(f"Failed to write final summary: {e}")

