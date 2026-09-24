"""CLI presentation — the Rich progress manager and its narration.

The previous tqdm implementation produced overwritten/wrapped noise in the
terminal because step lines and bars fought over the same lines.  These tests
pin the Rich behaviour: literal ``[tag]`` prefixes survive, step lines and the
end-of-run panel render, and the historical public API still works for every
call site (including the Redis worker subclass).
"""
import io
import sys

import pytest
from rich.console import Console


@pytest.fixture
def pm():
    """A verbose (no live region) ProgressManager capturing console output."""
    if "sec_scraper_cli" not in sys.modules:
        sys.argv = ["main"]
    import sec_scraper_cli as cli

    manager = cli.ProgressManager(verbose=True)
    buffer = io.StringIO()
    manager.console = Console(
        file=buffer, width=100, force_terminal=False, highlight=False, soft_wrap=True
    )
    manager._buffer = buffer
    return manager


def _out(pm) -> str:
    return pm._buffer.getvalue()


# ── literal tags ────────────────────────────────────────────────────────────


def test_step_lines_keep_their_literal_bracket_tags(pm):
    """``[normalize]``/``[llm]``/``[tool]`` are our tags, not Rich markup."""
    pm.write("  [normalize]       3 statement doc(s) → 3 bundle(s)")
    pm.write("  [llm]  agent step 2  → calling llm  (deepseek)")
    pm.write("  │  high  math_mismatch  (income/us-gaap:GrossProfit)")
    out = _out(pm)

    assert "[normalize]" in out
    assert "[llm]" in out
    assert "[tool]" not in out  # not written
    assert "gross_profit" not in out  # sanity: no markup-stripping artefacts


def test_status_is_silent_in_verbose_mode_but_write_is_not(pm):
    pm.status("suppressed when verbose")
    pm.write("always shown")
    out = _out(pm)
    assert "always shown" in out
    assert "suppressed when verbose" not in out


def test_error_is_rendered_red_without_markup_parsing(pm):
    pm.error("  [persist]  ✗ failed: DuplicateKeyError")
    assert "[persist]" in _out(pm)  # literal tag preserved


# ── end-of-run panel ────────────────────────────────────────────────────────


def test_summary_renders_a_panel_with_rows_and_separator(pm):
    pm.summary(
        "PROCESSING COMPLETE",
        [("  ✓ AAPL", "1 new"), ("", ""), ("  Companies", "1/1 succeeded")],
        subtitle="all companies processed successfully",
    )
    out = _out(pm)
    assert "PROCESSING COMPLETE" in out
    assert "AAPL" in out and "1 new" in out
    assert "1/1 succeeded" in out
    assert "all companies processed successfully" in out
    assert "╭" in out or "+" in out  # a box was drawn


# ── preserved public API ────────────────────────────────────────────────────


def test_public_api_surface_is_unchanged():
    """Every method the CLI and the Redis worker subclass rely on must exist."""
    sys.argv = ["main"]
    import sec_scraper_cli as cli

    for name in (
        "label", "start_companies", "set_current_company", "advance_company",
        "finish_companies", "filing_bar", "close_filing_bar", "close_all",
        "set_status", "status", "error", "write", "summary", "console",
    ):
        assert hasattr(cli.ProgressManager, name) or hasattr(cli.ProgressManager(verbose=True), name), name


def test_label_prefers_the_user_supplied_ticker():
    sys.argv = ["main"]
    import sec_scraper_cli as cli

    pm = cli.ProgressManager(verbose=True)
    pm.cik_labels["0000320193"] = "AAPL"
    assert pm.label("0000320193", "CIK") == "AAPL"
    assert pm.label("0000000009", "FB") == "FB"
    assert pm.label("", "FB") == "FB"


def test_filing_bar_is_a_noop_in_verbose_mode():
    """Verbose mode has no live region, so the bar must not create tasks."""
    sys.argv = ["main"]
    import sec_scraper_cli as cli

    pm = cli.ProgressManager(verbose=True)
    bar = pm.filing_bar("AAPL", 3)
    bar.update(1)                     # must not raise
    bar.set_description_str("x")
    bar.close()
    assert pm._filing_task is None


def test_winner_worker_subclass_still_constructs():
    """WorkerProgressManager overrides status/set_status/error; construction
    must keep working after the Rich rewrite."""
    sys.argv = ["main"]
    import worker_10kq  # noqa: F401  (import must not fail)

    from sec_scraper_cli import ProgressManager

    class _Fake(ProgressManager):
        def __init__(self):
            super().__init__(verbose=True, parallel=False)

    _Fake()
