"""The importable library's public API is a contract (docs/LIBRARY.md, docs/adr/0004): a snapshot of
ras_anything.runtime.__all__ with kinds and function signatures. Removing or changing an entry needs a major version
bump and an updated snapshot; adding one is a minor change. Regenerate: python -m tests.test_library_api --write"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[1] / "docs" / "api" / "runtime-api.json"


def current() -> dict:
    import ras_anything.runtime as r

    api = {}
    for n in sorted(r.__all__):
        o = getattr(r, n)
        kind = "class" if inspect.isclass(o) else "function" if callable(o) else "constant"
        sig = ""
        if kind == "function":
            try:
                sig = re.sub(r" at 0x[0-9a-f]+", "", str(inspect.signature(o)))
            except (TypeError, ValueError):
                sig = ""
        api[n] = {"kind": kind, "signature": sig} if sig else {"kind": kind}
    return api


class LibraryApi(unittest.TestCase):
    def test_public_api_matches_snapshot(self):
        snap = json.loads(SNAPSHOT.read_text())["api"]
        now = current()
        removed = sorted(set(snap) - set(now))
        changed = sorted(n for n in set(snap) & set(now) if snap[n] != now[n])
        added = sorted(set(now) - set(snap))
        self.assertEqual(removed, [], "public names removed (major version): %s" % removed)
        self.assertEqual(changed, [], "public signatures changed (major version unless only additive): %s" % changed)
        self.assertEqual(added, [], "new public names: update docs/api/runtime-api.json (minor version): %s" % added)

    def test_import_has_no_side_effects(self):
        code = "import sys, io; o=io.StringIO(); sys.stdout=o; import ras_anything.runtime; sys.stdout=sys.__stdout__; print(len(o.getvalue()))"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "0")
        self.assertEqual(out.stderr.strip(), "")

    def test_typed_marker_ships(self):
        self.assertTrue((Path(__file__).resolve().parents[1] / "ras_anything" / "py.typed").exists())


if __name__ == "__main__" and "--write" in sys.argv:
    doc = json.loads(SNAPSHOT.read_text())
    doc["api"] = current()
    SNAPSHOT.write_text(json.dumps(doc, indent=1, sort_keys=True))
    print("wrote", SNAPSHOT)
