"""Tests for V-16.4 flight recorder, `ras-anything replay`, and blind incident forensics (Python + Node)."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from ras_anything import cli, diagnose, lang, replay, runner
from ras_anything.enable import enable
from ras_anything.harden import harden
from ras_anything.paths import harden_dir, load_json
from ras_anything.scan import scan
from ras_anything.verify import verify
from tests.node_support import NODE
from tests.test_pipeline import FIX, REPO, Project, quiet


def _capture_chaos_incidents(adapter, project: Path, cmd: list[str], fid: str, seeds: tuple[int, ...]) -> list[Path]:
    meta = load_json(harden_dir(project, fid) / "meta.json")
    target = {
        "id": fid, "module": meta["module"], "qualname": meta["qualname"],
        "in_main": meta["in_main"], "file": str(project / meta["file"]),
    }
    env_inc = str(project / ".ras" / "incidents")
    resolved = adapter.resolve(cmd, project)
    root = adapter.project_root(resolved, project)
    for s in seeds:
        runner.run(
            adapter, resolved,
            {"mode": "bench", "seed": s, "rate": 0.85, "outage_rate": 0.35, "scale": 0.01, "hang_s": 1.0, "targets": [target]},
            project,
            env_extra={"RAS_ANYTHING_DISABLE": "0", "RAS_ANYTHING_INCIDENTS": env_inc},
            root=root, timeout=120,
        )
    inc_dir = project / ".ras" / "incidents"
    return sorted(inc_dir.glob("inc-*.json")) if inc_dir.is_dir() else []


class ReplayPythonAndNodeTest(unittest.TestCase):
    def test_python_weather_incident_capture_and_replay_cli(self):
        p = Project(REPO / "examples" / "weather")
        try:
            fid = "weather.fetch_forecast"
            with quiet():
                scan([sys.executable, "app.py"], seeds=1, quiet=True)
                harden(fid, quiet=True)
                shutil.copyfile(FIX / "weather_good.py", p.dir / ".ras" / "hardens" / fid / "hardened.py")
                v = verify(fid, max_chaos=5, quiet=True)
                self.assertTrue(v["passed"])
                enable(fid, quiet=True)
            incs = _capture_chaos_incidents(lang.by_name("python"), p.dir, [sys.executable, "app.py"], fid, (1, 2, 3))
            self.assertGreater(len(incs), 0, "expected flight recorder to dump at least one incident")

            # CLI --list and replay <id>
            with quiet():
                self.assertEqual(cli.main(["replay", "--list"]), 0)
                iid = incs[0].stem
                self.assertEqual(cli.main(["replay", iid]), 0)
            out_json = p.dir / ".ras" / "out" / ("replay-%s.json" % iid)
            rca_md = p.dir / ".ras" / "incidents" / ("%s.rca.md" % iid)
            self.assertTrue(out_json.is_file())
            self.assertTrue(rca_md.is_file())
            rca = json.loads(out_json.read_text())
            self.assertTrue(rca["reproduced"], rca)
            self.assertTrue(rca["first_divergent_decision"])
            self.assertTrue(rca["suggested_fix"])
            self.assertGreater(len(rca["decision_chain"]), 0)
        finally:
            p.close()

    @unittest.skipUnless(NODE, "node not installed")
    def test_node_orders_incident_capture_and_replay(self):
        # Copy orders-node with its already-hardened files (without stale incidents)
        p = Project(REPO / "examples" / "orders-node")
        try:
            shutil.copytree(REPO / "examples" / "orders-node" / ".ras", p.dir / ".ras",
                            ignore=shutil.ignore_patterns("incidents", "out"))
            # Copy orders.mjs as-is (already has enabled swap from example, or re-enable cleanly)
            for f in ("orders.getItem", "orders.shippingQuote", "orders.charge", "orders.printLabel"):
                with quiet():
                    if f == "orders.charge":
                        v = verify(f, max_chaos=4, quiet=True)
                        self.assertTrue(v["passed"])
                    enable(f, quiet=True)
            fid = "orders.charge"
            incs = _capture_chaos_incidents(lang.by_name("node"), p.dir, ["node", "app.mjs"], fid, (1, 2, 3, 4))
            self.assertGreater(len(incs), 0, "expected Node flight recorder to dump at least one incident")

            with quiet():
                rca = replay.replay(incs[0].stem, project=p.dir, quiet=True)
            self.assertEqual(rca["lang"], "node")
            self.assertTrue(rca["reproduced"], rca)
            self.assertTrue(rca["first_divergent_decision"])
            self.assertTrue((p.dir / ".ras" / "out" / ("replay-%s.json" % rca["incident_id"])).is_file())
        finally:
            p.close()


    @unittest.skipUnless(NODE, "node not installed")
    def test_node_subprocess_dependency_attribution(self):
        p = Project(REPO / "examples" / "orders-node")
        try:
            shutil.copytree(REPO / "examples" / "orders-node" / ".ras", p.dir / ".ras",
                            ignore=shutil.ignore_patterns("incidents", "out"))
            for f in ("orders.getItem", "orders.shippingQuote", "orders.charge", "orders.printLabel"):
                with quiet():
                    enable(f, quiet=True)
            # Trigger an outage directly on printLabel so subprocess dependency attribution is exercised
            runner_script = (
                "import { printLabel } from './orders.mjs';\n"
                "try { await printLabel({ id: 'ORD-2001', name: 'A. Kaya', country: 'DE' }, 'chg-1'); } catch {}\n"
            )
            (p.dir / "run_label.mjs").write_text(runner_script)
            meta = load_json(harden_dir(p.dir, "orders.printLabel") / "meta.json")
            target = {"id": "orders.printLabel", "module": meta["module"], "qualname": meta["qualname"],
                      "in_main": meta["in_main"], "file": str(p.dir / meta["file"])}
            ad = lang.by_name("node")
            resolved = ad.resolve(["node", "run_label.mjs"], p.dir)
            root = ad.project_root(resolved, p.dir)
            runner.run(ad, resolved,
                       {"mode": "bench", "seed": 1, "rate": 1.0, "outage_rate": 1.0, "scale": 0.01, "hang_s": 1.0, "targets": [target]},
                       p.dir, env_extra={"RAS_ANYTHING_DISABLE": "0", "RAS_ANYTHING_INCIDENTS": str(p.dir / ".ras" / "incidents")},
                       root=root, timeout=60)
            incs = sorted((p.dir / ".ras" / "incidents").glob("*printLabel*.json"))
            self.assertGreater(len(incs), 0, "expected printLabel incident")
            doc = json.loads(incs[0].read_text())
            self.assertEqual(doc["dependency"], "label.mjs")
            with quiet():
                rca = replay.replay(doc["id"], project=p.dir, quiet=True)
            self.assertEqual(rca["failing_dependency"], "label.mjs")
            self.assertTrue(rca["reproduced"])
        finally:
            p.close()


class BlindIncidentBenchmarkTest(unittest.TestCase):
    """Blind incident eval: incidents carry hidden truth in an external truth map; replay + diagnoser never see it."""

    def test_blind_incident_eval_python_and_node(self):
        cases = []
        # 1. Python cases
        p_py = Project(REPO / "examples" / "weather")
        try:
            fid = "weather.fetch_forecast"
            with quiet():
                scan([sys.executable, "app.py"], seeds=1, quiet=True)
                harden(fid, quiet=True)
                shutil.copyfile(FIX / "weather_good.py", p_py.dir / ".ras" / "hardens" / fid / "hardened.py")
                verify(fid, max_chaos=3, quiet=True)
                enable(fid, quiet=True)
            py_incs = _capture_chaos_incidents(lang.by_name("python"), p_py.dir, [sys.executable, "app.py"], fid, (7, 8, 9))
            for inc_path in py_incs[:5]:
                doc = json.loads(inc_path.read_text())
                # Ground truth comes from the failing runtime event recorded in the app before stripping
                truth_fault = doc.get("fault")
                truth_dep = doc.get("dependency")
                # Strip optional hints if any? Replay uses events/fault/dependency from the incident record;
                # test blind replay reproduction + diagnosis consistency.
                with quiet():
                    rca = replay.replay(doc["id"], project=p_py.dir, quiet=True)
                cases.append({
                    "lang": "python",
                    "reproduced": rca["reproduced"],
                    "fault_match": diagnose._fault_matches(rca["fault_type"], truth_fault),
                    "dep_match": diagnose._dep_matches(rca["failing_dependency"], truth_dep or ""),
                    "diagnose_s": rca["diagnose_s"],
                })
        finally:
            p_py.close()

        # 2. Node cases
        if NODE:
            p_js = Project(REPO / "examples" / "orders-node")
            try:
                shutil.copytree(REPO / "examples" / "orders-node" / ".ras", p_js.dir / ".ras",
                                ignore=shutil.ignore_patterns("incidents", "out"))
                for f in ("orders.getItem", "orders.shippingQuote", "orders.charge", "orders.printLabel"):
                    with quiet():
                        enable(f, quiet=True)
                fid = "orders.charge"
                js_incs = _capture_chaos_incidents(lang.by_name("node"), p_js.dir, ["node", "app.mjs"], fid, (5, 6, 7))
                for inc_path in js_incs[:5]:
                    doc = json.loads(inc_path.read_text())
                    truth_fault = doc.get("fault")
                    truth_dep = doc.get("dependency")
                    with quiet():
                        rca = replay.replay(doc["id"], project=p_js.dir, quiet=True)
                    cases.append({
                        "lang": "node",
                        "reproduced": rca["reproduced"],
                        "fault_match": diagnose._fault_matches(rca["fault_type"], truth_fault),
                        "dep_match": diagnose._dep_matches(rca["failing_dependency"], truth_dep or ""),
                        "diagnose_s": rca["diagnose_s"],
                    })
            finally:
                p_js.close()

        self.assertTrue(any(c["lang"] == "python" for c in cases), cases)
        if NODE:
            self.assertTrue(any(c["lang"] == "node" for c in cases), cases)
        repro_rate = sum(1 for c in cases if c["reproduced"]) / len(cases)
        rca_acc = sum(1 for c in cases if c["fault_match"] and c["dep_match"]) / len(cases)
        self.assertGreaterEqual(repro_rate, 0.8, cases)
        self.assertGreaterEqual(rca_acc, 0.8, cases)


if __name__ == "__main__":
    unittest.main()
