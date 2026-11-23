"""End-to-end tests for the Go adapter, holdout bad-hardening rejections, enable/disable, and inventory-go."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ras_anything import lang
from ras_anything.bench import bench
from ras_anything.enable import disable, enable
from ras_anything.harden import harden
from ras_anything.paths import UserError
from ras_anything.scan import scan
from ras_anything.verify import verify
from tests.test_pipeline import REPO, quiet

HOLDOUT_GO = REPO / "wow" / "work" / "holdout" / "go"

GOOD_RAS_SYNC = '''package target

import (
\t"bytes"
\t"context"
\t"encoding/json"
\t"fmt"
\t"net/http"
\t"time"
\t_ras_rt "holdoutgo/target/_ras_hardened"
)

var syncBreaker = _ras_rt.NewCircuitBreaker("harvest", 5, 15*time.Second)

func Sync(base, crop string) (Result, error) {
\tvar zero Result
\tif err := syncBreaker.Allow(); err != nil {
\t\t_ras_rt.LogEvent("ras.dependency.gave_up", map[string]any{
\t\t\t"ras.dependency": "harvest", "ras.fault": "circuit_open",
\t\t})
\t\treturn zero, &_ras_rt.RasError{Message: "harvest circuit open", Category: "circuit_open", Dependency: "harvest", Cause: err}
\t}
\tctx, cancel := context.WithTimeout(context.Background(), 4700*time.Millisecond)
\tdefer cancel()
\tclient := &http.Client{}
\tvar stock struct {
\t\tAvailable int `json:"available"`
\t}
\terr := _ras_rt.Retry(ctx, _ras_rt.RetryOpts{
\t\tAttempts: 4, Base: 50 * time.Millisecond, Cap: 1500 * time.Millisecond,
\t\tBudget: 4700 * time.Millisecond, Dependency: "harvest",
\t\tRetryOn: func(e error) bool { return _ras_rt.IsTransient(e) },
\t}, func(actx context.Context) error {
\t\tactx, acancel := context.WithTimeout(actx, 3400*time.Millisecond)
\t\tdefer acancel()
\t\treq, rerr := http.NewRequestWithContext(actx, "GET", base+"/harvest/"+crop, nil)
\t\tif rerr != nil {
\t\t\treturn rerr
\t\t}
\t\tres, rerr := client.Do(req)
\t\tif rerr != nil {
\t\t\treturn rerr
\t\t}
\t\tdefer res.Body.Close()
\t\tif res.StatusCode >= 400 {
\t\t\treturn &_ras_rt.HTTPError{StatusCode: res.StatusCode, Header: res.Header, URL: req.URL.String()}
\t\t}
\t\treturn json.NewDecoder(res.Body).Decode(&stock)
\t})
\tif err != nil {
\t\tsyncBreaker.Failure()
\t\tre := &_ras_rt.RasError{Message: "harvest read failed", Dependency: "harvest", Cause: err}
\t\t_ras_rt.LogEvent("ras.dependency.gave_up", map[string]any{
\t\t\t"ras.dependency": "harvest", "ras.fault": _ras_rt.FaultClass(err), "problem": re.Problem(),
\t\t})
\t\treturn zero, re
\t}
\tn := 10 - stock.Available
\tif n < 0 {
\t\tn = 0
\t}
\tb, _ := json.Marshal(map[string]any{"crop": crop, "quantity": n})
\tpctx, pcancel := context.WithTimeout(ctx, 3400*time.Millisecond)
\tdefer pcancel()
\treq, err := http.NewRequestWithContext(pctx, "POST", base+"/shipments", bytes.NewReader(b))
\tif err != nil {
\t\treturn zero, &_ras_rt.RasError{Message: "shipment build failed", Dependency: "harvest", Cause: err}
\t}
\treq.Header.Set("Content-Type", "application/json")
\tres, err := client.Do(req)
\tif err != nil {
\t\tsyncBreaker.Failure()
\t\tre := &_ras_rt.RasError{Message: "shipment post failed", Dependency: "harvest", Cause: err}
\t\t_ras_rt.LogEvent("ras.dependency.gave_up", map[string]any{
\t\t\t"ras.dependency": "harvest", "ras.fault": _ras_rt.FaultClass(err), "problem": re.Problem(),
\t\t})
\t\treturn zero, re
\t}
\tdefer res.Body.Close()
\tif res.StatusCode != 200 {
\t\tsyncBreaker.Failure()
\t\the := &_ras_rt.HTTPError{StatusCode: res.StatusCode, Header: res.Header, URL: req.URL.String()}
\t\tre := &_ras_rt.RasError{Message: fmt.Sprintf("shipment HTTP %d", res.StatusCode), Dependency: "harvest", Cause: he}
\t\t_ras_rt.LogEvent("ras.dependency.gave_up", map[string]any{
\t\t\t"ras.dependency": "harvest", "ras.fault": _ras_rt.FaultClass(he), "problem": re.Problem(),
\t\t})
\t\treturn zero, re
\t}
\tvar order struct {
\t\tID string `json:"id"`
\t}
\tif err = json.NewDecoder(res.Body).Decode(&order); err != nil {
\t\tsyncBreaker.Failure()
\t\tre := &_ras_rt.RasError{Message: "shipment decode failed", Dependency: "harvest", Cause: err}
\t\t_ras_rt.LogEvent("ras.dependency.gave_up", map[string]any{
\t\t\t"ras.dependency": "harvest", "ras.fault": "bad_body", "problem": re.Problem(),
\t\t})
\t\treturn zero, re
\t}
\tsyncBreaker.Success()
\treturn Result{Crop: crop, Available: stock.Available, Ordered: n, OrderID: order.ID}, nil
}
'''


class GoAdapterUnitTest(unittest.TestCase):
    def test_detect_and_doctor(self):
        a = lang.detect(["go", "run", "."])
        self.assertEqual(a.name, "go")
        self.assertTrue(a.detect(["go", "run", "./cmd/app", "--flag"]))
        self.assertFalse(a.detect(["go", "test", "./..."]))
        with self.assertRaises(UserError):
            lang.detect(["go", "build", "."])
        doc = a.doctor()
        self.assertTrue(doc and doc[0][0] is True, doc)

    def test_copy_name_not_go_suffix_and_refuse_forbidden(self):
        a = lang.by_name("go")
        self.assertFalse(a.copy_name("target.Sync", Path("target/target.go")).endswith(".go"))
        with tempfile.NamedTemporaryFile("w", suffix=".go", delete=False) as f:
            f.write('package target\nimport _ "unsafe"\nfunc Sync() {}\n')
            p = Path(f.name)
        try:
            bad = a.refuse_hardened(p)
            self.assertIsNotNone(bad)
            self.assertIn("unsafe", bad)
        finally:
            p.unlink(missing_ok=True)


class GoHoldoutPipelineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "holdout_go"
        shutil.copytree(HOLDOUT_GO, self.proj)
        self._cwd = os.getcwd()
        os.chdir(self.proj)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_scan_harden_verify_good_bad_enable_disable_bench(self):
        orig_sha = hashlib.sha256((self.proj / "target" / "target.go").read_bytes()).hexdigest()
        with quiet():
            sc = scan(["go", "run", "."], seeds=2, quiet=True)
        self.assertEqual(sc["lang"], "go")
        self.assertEqual(sc["functions"][0]["id"], "target.Sync")
        self.assertTrue(sc["functions"][0]["hardenable"])
        self.assertIn("no_timeout", sc["functions"][0]["signals"])
        self.assertIn("write_no_key", sc["functions"][0]["signals"])

        with quiet():
            h = harden("target.Sync", quiet=True)
        self.assertEqual(h["recorded"], 1)
        hp = self.proj / ".ras" / "hardens" / "target.Sync" / "hardened.go"
        self.assertTrue(hp.exists())

        # Positive control: rasruntime-hardened Sync passes every gate
        hp.write_text(GOOD_RAS_SYNC)
        with quiet():
            vg = verify("target.Sync", quiet=True)
        self.assertTrue(vg["passed"], vg)
        self.assertEqual(vg["ax"]["leaks"], [])
        self.assertGreater(vg["hardened"]["availability"], vg["original"]["availability"])

        # Enable, run live (enabled + RAS_ANYTHING_DISABLE=1), bench, disable byte-for-byte
        with quiet():
            enable("target.Sync", quiet=True)
        r_en = subprocess.run(["go", "run", "."], cwd=self.proj, capture_output=True, text=True, check=True)
        r_dis = subprocess.run(["go", "run", "."], cwd=self.proj,
                               env={**os.environ, "RAS_ANYTHING_DISABLE": "1"},
                               capture_output=True, text=True, check=True)
        self.assertEqual(r_en.stdout.strip(), r_dis.stdout.strip())

        with quiet():
            bn = bench(["go", "run", "."], seeds=3, quiet=True)
        self.assertEqual(bn["summary"]["hardened"]["runs"], 3)

        with quiet():
            dis = disable("target.Sync", quiet=True)
        self.assertTrue(dis["restored"])
        self.assertEqual(hashlib.sha256((self.proj / "target" / "target.go").read_bytes()).hexdigest(), orig_sha)

        # Holdout bad controls: all 4 MUST be rejected
        good_adapted = (
            (self.proj / "good" / "good.go").read_text()
            .replace("package good", "package target")
            .replace('\t"holdoutgo/target"\n', "")
            .replace("target.Result", "Result")
        )
        good_body = good_adapted.split("func Sync(", 1)[1]
        bad_src = (self.proj / "bad" / "bad.go").read_text()

        expected_gate_fail = {
            "Swallow": "no_wrong",
            "RetryPost": "dups",
            "ChangeOutput": "happy",
            "FakeFallback": "no_wrong",
        }
        for bname, gate in expected_gate_fail.items():
            b_adapted = (
                bad_src.replace("package bad", "package target")
                .replace('\t"holdoutgo/good"\n', '\t"bytes"\n\t"context"\n\t"encoding/json"\n\t"math/rand/v2"\n\t"net/http"\n\t"time"\n')
                .replace('\t"holdoutgo/target"\n', "")
                .replace("target.Result", "Result")
                .replace("good.Sync(", "_goodSync(")
            )
            for other in expected_gate_fail:
                if other == bname:
                    b_adapted = b_adapted.replace(f"func {other}(", "func Sync(")
                else:
                    b_adapted = b_adapted.replace(f"func {other}(", f"func _unused_{other}(")
            b_adapted += "\nfunc _goodSync(" + good_body
            hp.write_text(b_adapted)
            with quiet():
                vb = verify("target.Sync", quiet=True)
            self.assertFalse(vb["passed"], f"bad.{bname} unexpectedly passed")
            self.assertFalse(vb["gates"][gate], f"bad.{bname} expected gate {gate}=False, got {vb['gates']}")


class InventoryGoExampleTest(unittest.TestCase):
    def test_committed_artifacts_and_portability(self):
        ex = REPO / "examples" / "inventory-go"
        for rel in (
            "go.mod", "main.go", "warehouse/warehouse.go",
            "RAS-REPORT.md", "REPRODUCE.md", "docs/RUNBOOK.md",
            ".ras/scan.json", ".ras/bench.json", ".ras/slo.json", ".ras/state.json",
        ):
            self.assertTrue((ex / rel).is_file(), f"missing {rel}")
        self.assertTrue((REPO / "docs" / "cards" / "inventory-go.svg").is_file())
        state = json.loads((ex / ".ras" / "state.json").read_text())
        self.assertEqual(state["enabled"], {})
        bench_data = json.loads((ex / ".ras" / "bench.json").read_text())
        self.assertEqual(bench_data["summary"]["hardened"]["availability"], 1.0)
        for jp in (ex / ".ras").rglob("*.json"):
            if "runs" in jp.parts:
                continue
            txt = jp.read_text()
            self.assertNotIn("/workspace/", txt, str(jp))
            self.assertNotIn("/home/", txt, str(jp))
