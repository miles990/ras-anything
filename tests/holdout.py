"""Skip holdout pipeline tests when their fixtures are missing.

`wow/work/` is gitignored (intermediate wow artifacts). The holdout trees under
`wow/work/holdout/` therefore exist only on a working copy that has already run
those checks, not in a fresh clone or on CI. Tests that need them skip with a
clear reason instead of failing.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOLDOUT = REPO / "wow" / "work" / "holdout"


def skip_unless_holdout(name: str):
    """Skip the decorated class or method when `wow/work/holdout/<name>` is absent."""
    path = HOLDOUT / name
    return unittest.skipUnless(
        path.is_dir(),
        "wow/work/holdout/%s is untracked and not present in a fresh clone or CI" % name,
    )
