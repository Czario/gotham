"""Unified per-run file logger with ``<serial>-<ticker>-<timestamp_utc>.log`` naming.

Every CLI run writes all detailed logs (Python ``logging`` across all modules
plus ``shared.hooks`` trace lines) to a single file under ``<repo>/logs/``:

    logs/0001-AAPL-20261124T061530Z.log

The serial number auto-increments by scanning existing ``<digits>-*.log`` files
in the log directory.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

_SERIAL_RE = re.compile(r"^(\d+)-")
_DEFAULT_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


def sanitize_ticker_tag(tickers: Iterable[str] | str | None) -> str:
    """Return a clean uppercase ticker tag for log filenames."""
    if tickers is None:
        return "RUN"
    if isinstance(tickers, str):
        items = [tickers]
    else:
        items = [str(t) for t in tickers if t]
    cleaned: list[str] = []
    for raw in items:
        tok = re.sub(r"[^A-Za-z0-9]+", "", str(raw).upper())
        if tok and tok not in cleaned:
            cleaned.append(tok)
    if not cleaned:
        return "RUN"
    if len(cleaned) <= 3:
        return "_".join(cleaned)
    return f"{cleaned[0]}_AND_{len(cleaned) - 1}MORE"


def next_serial_number(log_dir: Path, width: int = 4) -> str:
    """Return the next zero-padded serial number in *log_dir* (e.g. ``'0001'``)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    max_num = 0
    try:
        for entry in log_dir.iterdir():
            if not entry.is_file():
                continue
            m = _SERIAL_RE.match(entry.name)
            if m:
                val = int(m.group(1))
                if val > max_num:
                    max_num = val
    except OSError:
        pass
    return str(max_num + 1).zfill(width)


def build_run_log_path(
    ticker: Iterable[str] | str | None,
    *,
    log_dir: Optional[Path | str] = None,
    now_utc: Optional[datetime] = None,
) -> tuple[str, Path]:
    """Allocate ``(serial_number, log_path)`` as ``<serial>-<ticker>-<YYYYMMDDTHHMMSSZ>.log``."""
    directory = Path(log_dir) if log_dir is not None else _DEFAULT_LOGS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    serial = next_serial_number(directory)
    tag = sanitize_ticker_tag(ticker)
    dt = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = dt.strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{serial}-{tag}-{stamp}.log"
    # Collision guard if two runs start in the exact same UTC second
    while path.exists():
        serial = str(int(serial) + 1).zfill(4)
        path = directory / f"{serial}-{tag}-{stamp}.log"
    return serial, path


class RunLogSession:
    """Manages the root-logger ``FileHandler`` for a single CLI run."""

    def __init__(
        self,
        ticker: Iterable[str] | str | None,
        *,
        log_dir: Optional[Path | str] = None,
        debug: bool = False,
        now_utc: Optional[datetime] = None,
    ) -> None:
        self.started_at_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.serial_number, self.path = build_run_log_path(
            ticker, log_dir=log_dir, now_utc=self.started_at_utc
        )
        self.handler: Optional[logging.FileHandler] = None
        self._debug = debug

    def attach(self) -> Path:
        """Attach a detailed FileHandler to the root logger."""
        if self.handler is not None:
            return self.path
        root = logging.getLogger()
        level = logging.DEBUG if self._debug else logging.INFO
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(level)
        fh = logging.FileHandler(self.path, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)sZ | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        # Format timestamps in UTC
        import time as _time
        fh.formatter.converter = _time.gmtime  # type: ignore[union-attr]
        root.addHandler(fh)
        self.handler = fh
        return self.path

    def write_raw(self, text: str) -> None:
        """Append raw text (e.g. rendered summary report) directly to the log file."""
        try:
            with open(self.path, "a", encoding="utf-8") as fp:
                fp.write(text)
                if not text.endswith("\n"):
                    fp.write("\n")
        except OSError:
            pass

    def close(self) -> None:
        """Detach and close the FileHandler."""
        if self.handler is not None:
            root = logging.getLogger()
            try:
                root.removeHandler(self.handler)
            except Exception:
                pass
            try:
                self.handler.flush()
                self.handler.close()
            except Exception:
                pass
            self.handler = None
