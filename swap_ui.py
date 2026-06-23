#!/usr/bin/env python3
"""Swappy desktop GUI launcher (kept for backwards compatibility).

The real implementation now lives in the package at ``swap.gui`` so it can be
launched the canonical way::

    python -m swap gui

This shim just forwards to it, whether Swappy is installed or run from a source
checkout.
"""

import sys
from pathlib import Path

# Allow running straight from a source checkout (no install needed).
SRC = Path(__file__).resolve().parent / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

from swap.gui import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
