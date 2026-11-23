"""enable/disable refuse symlinks and damaged state, and change nothing when they refuse."""

import hashlib
import json
import os
import shutil
import unittest
from pathlib import Path

from ras_anything.enable import disable, enable
from ras_anything.paths import UserError
from tests.test_pipeline import REPO, Project, quiet

FID, FID2 = "billing.charge", "billing.render_receipt"


def snapshot(folder: Path) -> dict:
    out = {}
    for p in sorted(folder.rglob("*")):
        rel = str(p.relative_to(folder))
        if p.is_symlink():
            out[rel] = "-> " + os.readlink(p)
        elif p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class SymlinkTest(unittest.TestCase):
    def setUp(self):
        self.p = Project(REPO / "examples" / "payments")
        shutil.copytree(REPO / "examples" / "payments" / ".ras", self.p.dir / ".ras")
        self.out = Path(self.p.tmp) / "outside"
        self.out.mkdir()
        (self.out / "victim.py").write_text("victim = 1\n")

    def tearDown(self):
        self.p.close()

    def refuses(self, fn, *needles):
        before = snapshot(Path(self.p.tmp))
        with self.assertRaises(UserError) as cm:
            fn()
        e = cm.exception
        for n in needles:
            self.assertIn(n, e.what)
        self.assertIn("nothing was changed", e.impact.lower())
        self.assertTrue(e.nxt)
        self.assertEqual(snapshot(Path(self.p.tmp)), before)

    def swap_for_link(self, path: Path, target: Path):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        path.symlink_to(target)

    def en(self):
        enable(FID, quiet=True)

    def dis(self):
        disable(FID, quiet=True)

    # enable ------------------------------------------------------------------------------------

    def test_enable_refuses_symlinked_module(self):
        real = self.p.dir / "real_billing.py"
        shutil.copy(self.p.dir / "billing.py", real)
        shutil.copy(real, self.out / "billing.py")
        for target in (self.out / "billing.py", real):  # outside, and inside the project
            self.swap_for_link(self.p.dir / "billing.py", target)
            self.refuses(self.en, "the module", "symlink")

    def test_enable_refuses_symlinked_copy_folder(self):
        for target in (self.out, self.p.dir / ".ras", Path(self.p.tmp) / "nowhere"):
            self.swap_for_link(self.p.dir / "_ras_hardened", target)
            self.refuses(self.en, "_ras_hardened", "symlink")

    def test_enable_refuses_symlinked_copies(self):
        d = self.p.dir / "_ras_hardened"
        d.mkdir()
        for name in ("billing__charge.py", "_runtime.py"):
            for target in (self.out / "victim.py", self.out / "new.py"):
                self.swap_for_link(d / name, target)
                self.refuses(self.en, name, "symlink")
                (d / name).unlink()

    def test_enable_refuses_symlinked_parent_folder(self):
        shutil.move(self.p.dir / "billing.py", self.out / "billing.py")
        (self.p.dir / "pkg").symlink_to(self.out)
        mp = self.p.dir / ".ras" / "hardens" / FID / "meta.json"
        mp.write_text(json.dumps(dict(json.loads(mp.read_text()), file="pkg/billing.py")))
        self.refuses(self.en, "pkg is a symlink")
        mp.write_text(json.dumps(dict(json.loads(mp.read_text()), file="../outside/billing.py")))
        self.refuses(self.en, "outside the project")

    def test_enable_refuses_symlinked_hardened_code_and_state(self):
        hp = self.p.dir / ".ras" / "hardens" / FID / "hardened.py"
        shutil.move(hp, self.out / "hardened.py")
        hp.symlink_to(self.out / "hardened.py")
        self.refuses(self.en, "hardened.py", "symlink")
        hp.unlink()
        shutil.move(self.out / "hardened.py", hp)
        self.swap_for_link(self.p.dir / ".ras" / "state.json", self.out / "state.json")
        self.refuses(self.en, "state.json")

    def test_hard_linked_runtime_copy_is_not_written_through(self):
        d = self.p.dir / "_ras_hardened"
        d.mkdir()
        os.link(self.out / "victim.py", d / "_runtime.py")
        self.en()
        self.assertEqual((self.out / "victim.py").read_text(), "victim = 1\n")
        self.dis()

    # disable -----------------------------------------------------------------------------------

    def test_disable_refuses_symlinks_that_appeared_later(self):
        self.en()
        d = self.p.dir / "_ras_hardened"
        shutil.move(self.p.dir / "billing.py", self.out / "billing.py")
        (self.p.dir / "billing.py").symlink_to(self.out / "billing.py")
        self.refuses(self.dis, "the module", "symlink")
        (self.p.dir / "billing.py").unlink()
        shutil.move(self.out / "billing.py", self.p.dir / "billing.py")
        for name in ("billing__charge.py", "_runtime.py"):
            shutil.move(d / name, self.out / name)
            (d / name).symlink_to(self.out / name)
            self.refuses(self.dis, name, "symlink")
            (d / name).unlink()
            shutil.move(self.out / name, d / name)
        shutil.move(d, self.out / "moved")
        d.symlink_to(self.out / "moved")
        self.refuses(self.dis, "_ras_hardened", "symlink")
        d.unlink()
        shutil.move(self.out / "moved", d)
        self.dis()

    def test_disable_checks_every_entry_before_changing_anything(self):
        with quiet():
            enable(FID, quiet=True)
            enable(FID2, quiet=True)
        sp = self.p.dir / ".ras" / "state.json"
        good = sp.read_text()
        for field, value in (("copy", "app.py"), ("copy", "../outside/victim.py"), ("file", "../outside/victim.py"),
                             ("inserted", "def charge(invoice):"), ("inserted", None), ("file", "missing.py")):
            st = json.loads(good)
            st["enabled"][FID2][field] = value  # the *other* entry is the bad one
            sp.write_text(json.dumps(st))
            self.refuses(self.dis)
        sp.write_text(good)
        with quiet():
            disable(FID, quiet=True)
            r = disable(FID2, quiet=True)
        self.assertTrue(r["restored"])
        self.assertEqual((self.p.dir / "billing.py").read_bytes(), (REPO / "examples/payments/billing.py").read_bytes())
        self.assertFalse((self.p.dir / "_ras_hardened").exists())

    def test_file_mode_survives(self):
        os.chmod(self.p.dir / "billing.py", 0o750)
        self.en()
        self.dis()
        self.assertEqual((self.p.dir / "billing.py").stat().st_mode & 0o777, 0o750)


if __name__ == "__main__":
    unittest.main()
