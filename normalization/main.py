#!/usr/bin/env python3
"""Backwards-compatible shim.

The CLI now lives in the installed package as
``data_normalization_service.cli`` (the ``normalize-data`` console script).
This file is kept so ``python normalization/main.py`` keeps working from a
source checkout.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from data_normalization_service.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
