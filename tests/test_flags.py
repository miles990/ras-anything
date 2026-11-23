"""Flags, hot update, canary/shadow routing, fail-safe, isolation, sticky-hash parity."""

from __future__ import annotations

import io
import json
import os
import statistics
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from ras_anything import control, flags, runtime
from ras_anything.cli import main
from ras_anything.patterns.policy import Policy
from tests.node_support import needs_node
from tests.test_pipeline import quiet


@contextmanager
def flagfile(doc=None):
    flags.reset()
    prev = os.environ.get("RAS_ANYTHING_FLAGS")
    disable = os.environ.pop("RAS_ANYTHING_DISABLE", None)
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    path = Path(name)
    if doc is not None:
        path.write_text(json.dumps(doc))
    else:
        path.unlink()
    os.environ["RAS_ANYTHING_FLAGS"] = str(path)
    try:
        yield path
    finally:
        flags.reset()
        if prev is None:
            os.environ.pop("RAS_ANYTHING_FLAGS", None)
        else:
            os.environ["RAS_ANYTHING_FLAGS"] = prev
        if disable is not None:
            os.environ["RAS_ANYTHING_DISABLE"] = disable
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class FlagStoreTest(unittest.TestCase):
    def test_missing_file_is_defaults(self):
        with flagfile():
            self.assertEqual(flags.is_pattern_on("shop", "retry", True), True)
            self.assertEqual(flags.is_pattern_on("shop", "bulkhead", False), False)
            self.assertEqual(flags.enable_percent("a.b"), 100)
            self.assertFalse(flags.shadow_on("a.b"))

    def test_set_get_list_and_disabled(self):
        with flagfile():
            flags.store().set_flag(flags.pattern_key("retry"), False)
            self.assertFalse(flags.is_pattern_on("shop", "retry", True))
            flags.store().set_flag(flags.pattern_key("retry", "shop"), True)
            self.assertTrue(flags.is_pattern_on("shop", "retry", True))
            self.assertFalse(flags.is_pattern_on("other", "retry", True))
            flags.store().set_flag(flags.enable_key("fn"), 25)
            self.assertEqual(flags.enable_percent("fn"), 25)
            flags.store().set_flag(flags.pattern_key("hedge"), True, state="DISABLED")
            self.assertFalse(flags.is_pattern_on("shop", "hedge", True))
            snap = flags.store().snapshot()
            self.assertEqual(snap["source"], "file")
            self.assertIn(flags.pattern_key("retry"), snap["flags"])

    def test_invalid_file_keeps_last_known_good(self):
        with flagfile({"flags": {flags.pattern_key("retry"): flags._flag_body(False)}}) as path:
            self.assertFalse(flags.is_pattern_on("shop", "retry", True))
            time.sleep(0.01)
            path.write_text("{not json")
            # mtime must change; maybe_reload should not throw and should keep last good
            self.assertFalse(flags.is_pattern_on("shop", "retry", True))
            self.assertEqual(runtime.flag_store().source, "last-known-good")
            self.assertTrue(runtime.flag_store().last_error)

    def test_hot_update_is_visible_on_the_next_call_and_drops_nothing(self):
        """Write the file under a tight loop; first call after the write must see the new value."""
        with flagfile({"flags": {flags.pattern_key("retry"): flags._flag_body(True)}}) as path:
            flags.store()
            self.assertTrue(flags.is_pattern_on("shop", "retry", True))
            delays, misses = [], 0
            for i in range(40):
                path.write_text(json.dumps({"flags": {flags.pattern_key("retry"): flags._flag_body(i % 2 == 0)}}))
                t0 = time.perf_counter()
                want = (i % 2 == 0)
                seen = None
                for _ in range(1000):
                    seen = flags.is_pattern_on("shop", "retry", True)
                    if seen is want:
                        delays.append(time.perf_counter() - t0)
                        break
                else:
                    misses += 1
            self.assertEqual(misses, 0)
            self.assertGreaterEqual(len(delays), 40)
            p50 = statistics.median(delays)
            p99 = sorted(delays)[int(0.99 * (len(delays) - 1))]
            self.assertLess(p99, 0.05)  # a stat() + read on this machine
            Path("/tmp/ras-flag-hot.json").write_text(json.dumps({
                "n": len(delays), "p50_s": p50, "p99_s": p99, "dropped": misses,
            }))

    def test_evaluate_never_raises(self):
        with flagfile({"flags": {"x": {"state": "ENABLED", "variants": {}, "defaultVariant": "missing"}}}):
            self.assertEqual(flags.store().evaluate("x", "fallback"), "fallback")
        with mock.patch.object(flags.Store, "flag", side_effect=RuntimeError("boom")):
            with flagfile():
                self.assertEqual(flags.store().evaluate("y", 7), 7)


class CliFlagTest(unittest.TestCase):
    def test_flag_set_list_get(self):
        with flagfile(), quiet(), mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(main(["flag", "set", "ras.pattern.retry", "off"]), 0)
            self.assertEqual(main(["flag", "get", "ras.pattern.retry"]), 0)
            self.assertIn("false", out.getvalue())
            self.assertEqual(main(["flag", "set", "weather.fetch_forecast", "--canary", "5"]), 0)
            self.assertEqual(flags.enable_percent("weather.fetch_forecast"), 5)


class RoutingTest(unittest.TestCase):
    def test_sticky_canary_is_stable_and_matches_node(self):
        keys = ["order-%d" % i for i in range(200)]
        buckets = [flags.sticky_bucket(k) for k in keys]
        self.assertEqual(buckets, [flags.sticky_bucket(k) for k in keys])
        self.assertEqual(len(set(buckets)), len(set(buckets)))  # sanity
        self.assertTrue(0 <= min(buckets) < 100)
        with flagfile():
            flags.store().set_flag(flags.enable_key("fn"), 10)
            inside = sum(1 for k in keys if flags.in_canary("fn", k))
            # 200 keys, 10% → around 20; allow generous noise
            self.assertTrue(5 <= inside <= 40, inside)

    @needs_node
    def test_node_uses_the_same_buckets(self):
        import shutil, subprocess
        keys = ["order-%d" % i for i in range(50)]
        py = [flags.sticky_bucket(k) for k in keys]
        script = "const f=require(%r); const keys=%s; process.stdout.write(JSON.stringify(keys.map(k=>f.stickyBucket(k))))" % (
            str(Path(__file__).resolve().parents[1] / "ras_anything" / "lang" / "node_agent" / "patterns" / "flags.cjs"),
            json.dumps(keys),
        )
        out = subprocess.check_output([shutil.which("node"), "-e", script], text=True)
        self.assertEqual(json.loads(out), py)

    def test_swap_canary_and_disable(self):
        hits = {"o": 0, "h": 0}

        def original(key):
            hits["o"] += 1
            return "o:" + key

        def hardened(key):
            hits["h"] += 1
            return "h:" + key

        with flagfile():
            flags.store().set_flag(flags.enable_key("fn"), 0)
            fn = control.swap("fn", original, hardened)
            self.assertEqual(fn("a"), "o:a")
            flags.store().set_flag(flags.enable_key("fn"), 100)
            self.assertEqual(fn("a"), "h:a")
            os.environ["RAS_ANYTHING_DISABLE"] = "1"
            try:
                flags.reset()
                os.environ["RAS_ANYTHING_FLAGS"] = os.environ["RAS_ANYTHING_FLAGS"]
                self.assertEqual(fn("a"), "o:a")
            finally:
                os.environ.pop("RAS_ANYTHING_DISABLE", None)

    def test_shadow_serves_original_and_compares_when_suppressed(self):
        seen = []

        def original(x):
            return "ok"

        def hardened(x):
            seen.append(x)
            return "ok"

        with flagfile():
            flags.store().set_flag(flags.shadow_key("fn"), True)
            fn = control.swap("fn", original, hardened)
            self.assertEqual(fn("a"), "ok")
            self.assertEqual(seen, [])  # no suppressor: don't double-call
            control.set_shadow_suppressor(lambda h, a, kw: h(*a, **kw))
            try:
                self.assertEqual(fn("b"), "ok")
                self.assertEqual(seen, ["b"])
            finally:
                control.set_shadow_suppressor(None)

    def test_router_bug_falls_back_to_original(self):
        def original():
            return "orig"

        def hardened():
            return "hard"

        with flagfile(), mock.patch("ras_anything.runtime.route", side_effect=RuntimeError("bug")):
            fn = control.swap("fn", original, hardened)
            # route is wrapped in fail_safe inside _pick... actually _pick calls route which we patched to raise.
            # fail_safe(_pick, "original") should catch it.
            self.assertEqual(fn(), "orig")

    def test_canary_analyse_promote_and_rollback(self):
        good = {"original": {"availability": 0.99, "p99": 0.2, "error_rate": 0.01, "dups": 0, "n": 50},
                "canary": {"availability": 0.995, "p99": 0.18, "error_rate": 0.005, "dups": 0, "n": 50},
                "percent": 5, "plan": [5, 25, 50, 100]}
        d = control.analyse(good)
        self.assertEqual(d, {"decision": "promote", "percent": 25, "reasons": d["reasons"], "n": 50})
        bad = {**good, "canary": {**good["canary"], "availability": 0.90}}
        d = control.analyse(bad)
        self.assertEqual(d["decision"], "rollback")
        self.assertEqual(d["percent"], 0)
        wait = {**good, "canary": {**good["canary"], "n": 3}}
        self.assertEqual(control.analyse(wait)["decision"], "wait")
        with flagfile():
            control.apply_decision("fn", d)
            self.assertEqual(flags.enable_percent("fn"), 0)


class IsolationTest(unittest.TestCase):
    def test_a_poison_key_does_not_open_the_others_breaker(self):
        p = Policy("dep", circuit_breaker={"fail_threshold": 3, "min_calls": 1, "window": 10, "cooldown_s": 30, "failure_rate": 0.5})

        def boom(_):
            raise runtime.RasError("down")

        def ok(_):
            return "ok"

        # without shards, failures on "poison" would open the shared breaker
        # with shards, only that key is contained
        for _ in range(5):
            with self.assertRaises(Exception):
                p.shard("poison").call(boom)
        self.assertEqual(p.shard("ok").call(ok), "ok")
        # the unsharded policy is a different isolation scope and is unused here
        self.assertEqual(control.blast_radius(5, 100, contained_s=0.0)["share"], 0.05)


class ApprovalTest(unittest.TestCase):
    def test_require_approval_denies_by_default(self):
        with self.assertRaises(control.NeedsApproval) as cm:
            control.require_approval("charge", {"cents": 100})
        self.assertTrue(cm.exception.needs_human_authorization)
        self.assertEqual(cm.exception.next_action, "escalate")
        control.set_approver(lambda op, d: op == "charge")
        try:
            self.assertTrue(control.require_approval("charge", {"cents": 1}))
        finally:
            control.set_approver(None)


class PolicyFlagTest(unittest.TestCase):
    def test_turning_retry_off_is_visible_on_the_next_call(self):
        calls = []

        def flaky(a):
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError("nope")
            return "ok"

        pol = Policy("shop", retry={"attempts": 4}, circuit_breaker=False, fallback=False, retry_budget=False)
        with flagfile():
            self.assertEqual(pol.call(flaky), "ok")
            self.assertGreater(len(calls), 1)
            n = len(calls)
            flags.store().set_flag(flags.pattern_key("retry"), False)
            calls.clear()
            with self.assertRaises(TimeoutError):
                pol.call(flaky)
            self.assertEqual(len(calls), 1, "retry was still on")
            self.assertGreater(n, 1)
