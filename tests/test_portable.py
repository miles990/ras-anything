"""What gets committed (.ras/**/*.json) carries no machine-specific paths, and still works elsewhere."""

import json
import os
import re
import shutil
import sys
import unittest
from pathlib import Path

from ras_anything.paths import load_local, portable_cmd, scrub
from ras_anything.verify import verify
from tests.test_pipeline import FIX, REPO, Project, quiet, scan, harden

ABS = re.compile(r"""^(/|[A-Za-z]:\\)|(^|[\s"'=:(,;\[])/(workspace|home|usr|tmp|opt|var|root|Users|private|mnt)\b|/workspace|/home/""")


def strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from strings(v)


def absolute_paths(folder: Path):
    return ["%s: %s" % (p.name, s[:120]) for p in sorted(folder.rglob("*.json")) if ".ras" in p.parts
            for s in strings(json.loads(p.read_text())) if ABS.search(s)]


class CommittedJsonTest(unittest.TestCase):
    def test_example_json_has_no_absolute_paths(self):
        files = [p for p in (REPO / "examples").rglob("*.json") if ".ras" in p.parts]
        self.assertGreater(len(files), 8)
        self.assertEqual(absolute_paths(REPO / "examples"), [])

    def test_the_check_itself_catches_paths(self):
        for s in ("/x", "a /usr/bin/python", "File \"/tmp/x.py\"", "PATH=/opt/x", "C:\\x", "x:/home/me"):
            self.assertTrue(ABS.search(s), s)
        for s in ("http://127.0.0.1:80/v1/x", "GET http://a/b", "a/b", "${PROJECT}/render.py", "and/or"):
            self.assertFalse(ABS.search(s), s)


class FreshRecordingTest(unittest.TestCase):
    def test_absolute_command_is_stored_portably_and_replays_after_a_move(self):
        p = Project(REPO / "examples" / "payments")
        try:
            fid = "billing.render_receipt"
            shutil.copytree(REPO / "examples" / "payments" / ".ras" / "hardens" / fid, p.dir / "keep")
            with quiet():
                scan([sys.executable, str(p.dir / "app.py")], seeds=1, quiet=True)
                harden(fid, quiet=True)
            shutil.copy(p.dir / "keep" / "hardened.py", p.dir / ".ras" / "hardens" / fid / "hardened.py")
            self.assertEqual(absolute_paths(p.dir), [])
            meta = json.loads((p.dir / ".ras" / "hardens" / fid / "meta.json").read_text())
            self.assertEqual(meta["cmd"][1], "app.py")
            self.assertNotIn("executable", meta)
            self.assertEqual(load_local(p.dir)["executable"], sys.executable)
            # the project moves, and the machine-local notes are gone (a fresh clone)
            moved = Path(p.tmp) / "elsewhere"
            os.chdir(p.tmp)
            shutil.move(p.dir, moved)
            shutil.rmtree(moved / ".ras" / "runs")
            os.chdir(moved)
            with quiet():
                v = verify(fid, quiet=True)
            self.assertTrue(v["passed"], v.get("gates") or v.get("fatal"))
        finally:
            p.close()

    def test_failed_verify_report_is_scrubbed(self):
        p = Project(FIX / "shop")
        try:
            with quiet():
                scan([sys.executable, "app.py"], seeds=1, quiet=True)
                harden("orders.place_order", quiet=True)
                (p.dir / ".ras/hardens/orders.place_order/hardened.py").write_text("import os\nraise RuntimeError(os.getcwd())\n")
                v = verify("orders.place_order", quiet=True)
            self.assertIn("RuntimeError", v["fatal"]["message"])
            self.assertEqual(absolute_paths(p.dir), [])
        finally:
            p.close()


class PortableCmdTest(unittest.TestCase):
    def test_portable_cmd(self):
        proj = Path("/srv/app")
        self.assertEqual(portable_cmd(["/srv/app/.venv/bin/python", "/srv/app/x.py", "--n", "3"], proj), [".venv/bin/python", "x.py", "--n", "3"])
        self.assertEqual(portable_cmd(["/usr/bin/python3", "app.py"], proj), ["python3", "app.py"])
        self.assertEqual(scrub({"a": ["see /srv/app/x.py and /srv/app2"]}, proj), {"a": ["see ./x.py and /srv/app2"]})


if __name__ == "__main__":
    unittest.main()
