"""Retry amplification and the sustained-outage sequence of verify: the metric, its judging, and that it catches
what a per-call trial can't (state that goes wrong across calls)."""

import sys
import unittest

from ras_anything import bench as bench_mod
from ras_anything import faults, judge
from ras_anything.harden import harden
from ras_anything.scan import scan
from tests.node_support import HAVE, needs_node
from tests.test_pipeline import REPO, Project, quiet

HEAD = '''
import json, os
from urllib.parse import quote
from urllib.request import urlopen
from ras_anything.runtime import *
from ras_anything.runtime import Policy, RejectedError, CircuitBreaker, CircuitOpenError, retry, log_event, is_transient

RAS_FALLBACK = lambda city: {"city": city, "status": "unavailable"}

def get(city, timeout):
    with urlopen("%s/v1/forecast?city=%s" % (os.environ["WEATHER_API_URL"], quote(city)), timeout=timeout) as r:
        return json.loads(r.read())

bad = lambda e: is_transient(e) or isinstance(e, ValueError)
'''
POLICY = HEAD + '''
weather = Policy("weather", retry={"attempts": 4, "retry_on": bad}@@)

def fetch_forecast(city):
    try:
        return weather.call(lambda a: get(city, a.timeout_s))
    except (OSError, ValueError, RejectedError) as e:
        log_event("degraded", city=city, error=repr(e))
        return RAS_FALLBACK(city)
'''
# never closes again once it has opened, whatever the dependency does
LATCH = HEAD + '''
state = {"fails": 0}

def fetch_forecast(city):
    if state["fails"] >= 8:
        log_event("degraded", city=city, error="latched")
        return RAS_FALLBACK(city)
    try:
        out = retry(lambda: get(city, 3.5), attempts=4, budget_s=4.5, retry_on=bad)
        state["fails"] = 0
        return out
    except Exception as e:
        state["fails"] += 1
        log_event("degraded", city=city, error=repr(e))
        return RAS_FALLBACK(city)
'''
# gives up without having seen anything fail
LIAR = HEAD + '''
state = {"n": 0}

def fetch_forecast(city):
    state["n"] += 1
    if state["n"] %% 7 == 0:
        log_event("degraded", city=city, error="so it says")
        return RAS_FALLBACK(city)
    return get(city, 3.5)
'''.replace("%%", "%")


class SequenceTest(unittest.TestCase):
    fid = "weather.fetch_forecast"

    @classmethod
    def setUpClass(cls):
        cls.p = Project(REPO / "examples" / "weather")
        with quiet():
            scan([sys.executable, "app.py"], seeds=1, quiet=True)
            harden(cls.fid, quiet=True)

    @classmethod
    def tearDownClass(cls):
        cls.p.close()

    def verify(self, code):
        from ras_anything.verify import verify
        (self.p.dir / ".ras" / "hardens" / self.fid / "hardened.py").write_text(code)
        v = verify(self.fid, quiet=True)
        self.assertNotIn("fatal", v, v.get("fatal"))
        return v

    def test_naive_retry_amplifies_and_a_policy_does_not(self):
        naive = self.verify(POLICY.replace("@@", ", retry_budget=False, circuit_breaker=False"))
        good = self.verify(POLICY.replace("@@", ""))
        self.assertTrue(naive["passed"] and good["passed"], (naive["gates"], good["gates"]))
        self.assertEqual(naive["outage"]["original"]["amplification"], 1.0)
        self.assertEqual(naive["outage"]["hardened"]["amplification"], 4.0)
        self.assertLess(good["outage"]["hardened"]["amplification"], 1.0)
        out = good["outage"]["hardened"]
        self.assertEqual(out["degraded"] + out["ok"], 40)  # (the recorded answer for one city equals the fallback)
        self.assertEqual(good["hardened"]["availability"], 1.0)   # per-call trials are independent: no breaker left open
        self.assertLessEqual(good["outage"]["hardened"]["recovered_after_s"], 6.0)  # one cooldown
        self.assertEqual(naive["outage"]["hardened"]["recovered_after_s"], 0)
        self.assertEqual(good["outage"]["hardened"]["healthy_per_call"], 1.0)
        self.assertGreater(naive["hardened"]["amplification"], 1.0)

    def test_a_breaker_that_never_closes_is_caught(self):
        v = self.verify(LATCH)
        self.assertIsNone(v["outage"]["hardened"]["recovered_after_s"])
        self.assertFalse(v["gates"]["no_wrong"])
        self.assertEqual(v["hardened"]["wrong"], 0)  # (no per-call trial shows it: a process that starts anew)

    def test_a_fallback_nothing_has_failed_for_is_still_wrong(self):
        v = self.verify(LIAR)
        self.assertFalse(v["passed"])
        self.assertFalse(v["gates"]["no_wrong"])
        self.assertGreater(v["outage"]["hardened"]["wrong"], 0)

    def test_bench_counts_attempts_per_call(self):
        from ras_anything.enable import disable, enable
        v = self.verify(POLICY.replace("@@", ""))
        self.assertTrue(v["passed"])
        with quiet():
            enable(self.fid, quiet=True)
            try:
                b = bench_mod.bench(seeds=3, rate=0.3, quiet=True)
            finally:
                disable(self.fid, quiet=True)
        o, h = b["summary"]["original"], b["summary"]["hardened"]
        self.assertEqual((o["clean_per_call"], h["clean_per_call"]), (1.0, 1.0))
        self.assertEqual(o["amplification"], 1.0)
        self.assertGreater(h["amplification"], 1.0)
        st = b["functions"]["hardened"][self.fid]
        self.assertAlmostEqual(h["amplification"], st["attempts"] / st["calls"])


def trial(outcome="return", matches=False, is_fallback=False, attempts=(), logs=1, latency=0.0):
    return {"outcome": outcome, "matches": matches, "is_fallback": is_fallback, "logs": logs, "latency": latency,
            "attempts": [{"key": k, "failed": f, "nonidempotent": False} for k, f in attempts]}


class JudgeTest(unittest.TestCase):
    K = "GET http://a/x"

    def classes(self, ts, gap=0.5, keys=None, allowed=True):
        n = len(ts)
        out = judge.classify_sequence(ts, [{"result": 1}] * n, keys or [[self.K]] * n, 5.0, allowed, [gap] * n)
        return [t["class"] for t in out]

    FAIL, OPEN = trial(is_fallback=True, attempts=[(K, True)]), trial(is_fallback=True)
    OK = trial(matches=True, attempts=[(K, False)])

    def test_open_circuit_is_earned_by_what_was_seen_before(self):
        self.assertEqual(self.classes([self.FAIL, self.OPEN, self.OPEN]), ["degraded"] * 3)
        self.assertEqual(self.classes([self.OPEN, self.FAIL]), ["wrong", "degraded"])             # nothing had failed yet
        self.assertEqual(self.classes([self.FAIL, self.OK, self.OPEN]), ["degraded", "ok", "wrong"])  # it answered since
        self.assertEqual(self.classes([self.FAIL, self.OPEN], gap=61), ["degraded", "wrong"])      # too long ago
        self.assertEqual(self.classes([self.FAIL, trial(is_fallback=True, logs=0)]), ["degraded", "wrong"])
        self.assertEqual(self.classes([self.FAIL, self.OPEN], allowed=False), ["wrong", "wrong"])

    def test_evidence_is_per_dependency(self):
        other, same = "GET http://b/x", "GET http://a/other"
        self.assertEqual(self.classes([self.FAIL, self.OPEN], keys=[[self.K], [other]]), ["degraded", "wrong"])
        self.assertEqual(self.classes([self.FAIL, self.OPEN], keys=[[self.K], [same]]), ["degraded", "degraded"])
        self.assertEqual(self.classes([self.FAIL, self.OPEN], keys=[[self.K], [self.K, other]]), ["degraded", "wrong"])

    def test_summary(self):
        ts = judge.classify_sequence([self.OK, self.FAIL, self.OPEN, self.OPEN, self.OK], [{"result": 1}] * 5, [[self.K]] * 5, 5.0, True, [0.5] * 5)
        s = judge.summarize_outage(["healthy", "down", "down", "recovered", "recovered"], ts)
        self.assertEqual((s["calls"], s["attempts"], s["amplification"], s["healthy_per_call"], s["recovered_after_s"], s["degraded"]), (2, 1, 0.5, 1.0, 0.5, 2))

    def test_dependency_of(self):
        for key, want in (("GET http://h/a?b #12", "http://h"), ("POST https://h/x", "https://h"), ("run python render.py {}", "run python render.py"),
                          ("CONNECT db:5432", "CONNECT db:5432")):
            self.assertEqual(judge.dependency_of(key), want)


class PlanTest(unittest.TestCase):
    def test_outage_windows(self):
        f = faults.FirstAttempt("outage")
        self.assertEqual([f.outage("k", n) for n in range(5)], [True] * 3 + [False] * 2)
        self.assertFalse(faults.FirstAttempt("refused").outage("k", 0) or faults.Plan().outage("k", 0))
        self.assertTrue(faults.Down().outage("k", 99) and faults.Down().fault("k", 99) == "refused")
        s = faults.Seeded(1, rate=0.0, outage_rate=0.5)
        for i in range(40):
            k = "k%d" % i
            self.assertEqual([s.outage(k, n) for n in range(4)], [s.fault(k, n) == "refused" for n in range(4)])

    @needs_node
    def test_same_in_node(self):
        import json
        from tests.node_support import AGENT, run_json
        plans = [{"type": "down"}, {"type": "none"}, {"type": "first", "fault": "outage", "key": None}, {"type": "first", "fault": "outage", "key": "k1"},
                 {"type": "first", "fault": "slow", "key": None}, {"type": "seeded", "seed": 3, "rate": 0.3, "outage_rate": 0.4, "salt": ""}]
        theirs = run_json("import f from %s; const ps = %s; console.log(JSON.stringify(ps.map((p) => { const x = f.plan(p); const o = []; "
                          "for (let k = 0; k < 8; k++) for (let n = 0; n < 5; n++) o.push(x.outage('k' + k, n)); return o; })))" % (
                              json.dumps("file://" + str(AGENT / "faults.cjs")), json.dumps(plans)))
        from ras_anything.agents.python import worker
        mine = [[worker.plan_of(p).outage("k%d" % k, n) for k in range(8) for n in range(5)] for p in plans]
        self.assertEqual(theirs, mine)


@unittest.skipUnless(HAVE, "needs Node.js")
class NodeSequenceTest(unittest.TestCase):
    def test_policy_in_node(self):
        from tests.node_support import NodeCase, js
        from ras_anything.verify import verify
        case = NodeCase()
        self.addCleanup(case.doCleanups)
        p = case.project({"lib.mjs": """
            export async function get(i) {
              return (await fetch(`${process.env.API}/v1/${i}`)).json();
            }
        """})
        with quiet():
            scan(["node", "app.mjs"], seeds=1, quiet=True)
            harden("lib.get", quiet=True)
        code = """
            import { Policy, RasError, isTransient, raiseForStatus } from "./_runtime.cjs";
            export const RAS_FALLBACK = null;
            const api = new Policy("api", { retry: { attempts: 4, retryOn: (e) => isTransient(e) || e instanceof SyntaxError } %s });
            export async function get(i) {
              try {
                return await api.call(async ({ signal }) => (await raiseForStatus(await fetch(`${process.env.API}/v1/${i}`, { signal }))).json());
              } catch (e) {
                throw new RasError("get failed", { cause: e });
              }
            }
        """
        out = []
        for extra in ("", ", retryBudget: false, circuitBreaker: false"):
            (p.dir / ".ras/hardens/lib.get/hardened.mjs").write_text(js(code % extra))
            with quiet():
                v = verify("lib.get", quiet=True)
            self.assertTrue(v["passed"], v.get("fatal") or v["gates"])
            out.append(v["outage"]["hardened"])
        self.assertLess(out[0]["amplification"], 1.0)
        self.assertEqual(out[1]["amplification"], 4.0)
        self.assertEqual(out[0]["error"], 40)
        self.assertLessEqual(out[0]["recovered_after_s"], 6.0)


if __name__ == "__main__":
    unittest.main()
