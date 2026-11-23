"""The pattern lab: the simulation (the same in both runtimes), the arithmetic, the decision rule, and that the
committed numbers, verdicts and docs agree."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from ras_anything import lab, lang, patterns
from ras_anything.cli import main
from tests.node_support import needs_node
from tests.test_pipeline import REPO, quiet

PY = lang.by_name("python")


def simulate(adapter, specs, vs):
    return dict(zip(specs, lab.simulate(adapter, specs, vs)))


class SimulationTest(unittest.TestCase):
    def test_seeded_and_the_happy_path_is_untouched_by_everything(self):
        vs = lab.variants()
        specs = [("healthy", v, 1) for v in vs if "hedge" not in v]
        ms = simulate(PY, specs, vs)
        self.assertEqual(ms, simulate(PY, specs, vs))
        for (_, v, _), m in ms.items():
            self.assertEqual((m["availability"], m["fresh"], m["amplification"], m["dups"], m["wasted"]), (1.0, 1.0, 1.0, 0, 0), v)
        self.assertEqual(len({m["p99_s"] for m in ms.values()}), 1)

    def test_a_retry_storm_is_one_and_the_stack_ends_it(self):
        ms = simulate(PY, [("retry storm", v, 1) for v in ("bare", "stack")], lab.variants())
        bare, stack = ms["retry storm", "bare", 1], ms["retry storm", "stack", 1]
        self.assertGreater(bare["outage_amplification"], 2.0)
        self.assertGreater(bare["recovery_s"], 60)   # the trouble lasted 15 s; the retries keep it alive
        self.assertLess(stack["recovery_s"], 10)
        self.assertLess(stack["outage_amplification"], 1.0)

    def test_writes_without_one_key_are_done_twice(self):
        ms = simulate(PY, [("lost answers", v, 1) for v in ("stack", "stack-idempotency")], lab.variants())
        self.assertEqual(ms["lost answers", "stack", 1]["dups"], 0)
        self.assertGreater(ms["lost answers", "stack-idempotency", 1]["dups"], 10)

    @needs_node
    def test_node_lives_through_the_same(self):
        """Same arrivals and faults; what has no jitter in it is equal, the rest is close."""
        vs = lab.variants()
        specs = [(s, v, 2) for s in lab.SCENARIOS for v in ("bare", "stack")]
        a, b = simulate(PY, specs, vs), simulate(lang.by_name("node"), specs, vs)
        for k in specs:
            self.assertEqual(a[k]["calls"], b[k]["calls"])
            if not (k[0] == "layered calls" and k[1] == "bare"):  # Node doesn't cancel what nobody awaits
                self.assertAlmostEqual(a[k]["availability"], b[k]["availability"], delta=0.04, msg=k)
                self.assertAlmostEqual(a[k]["amplification"], b[k]["amplification"], delta=0.12, msg=k)


class RuleTest(unittest.TestCase):
    def c(self, **effects):
        """A comparison in which availability etc. moved: metric=(off, on)."""
        out = {m: {"off": 1.0, "on": 1.0, "effect": 0} for m in ("availability", "p99_s", "dups", "amplification", "recovery_s")}
        for m, (off, on) in effects.items():
            out[m] = {"off": off, "on": on, "effect": lab.effect(m, [on] * 5, [off] * 5)}
        return out

    def verdict(self, in_stack=True, bare=None, healthy=None, **scenarios):
        st = {"healthy": healthy or self.c(), **{k: v for k, v in scenarios.items()}}
        return lab.decide({"stack": st, "bare": {k: (bare or {}).get(k, self.c()) for k in st}}, in_stack)

    def test_effect_needs_a_margin_and_agreement(self):
        self.assertEqual(lab.effect("availability", [0.99] * 5, [0.98] * 5), 1)
        self.assertEqual(lab.effect("availability", [0.982] * 5, [0.98] * 5), 0)
        self.assertEqual(lab.effect("p99_s", [2.0] * 5, [1.0] * 5), -1)
        self.assertEqual(lab.effect("amplification", [1.0, 1.0, 1.0, 9.0, 9.0], [3.0] * 5), 0)  # 3 of 5 seeds: no agreement
        self.assertEqual(lab.effect("dups", [0] * 5, [0] * 5), 0)

    def test_verdicts(self):
        self.assertEqual(self.verdict(a=self.c())["verdict"], "rejected")
        self.assertEqual(self.verdict(a=self.c(availability=(0.5, 0.9)))["verdict"], "adopted")
        self.assertEqual(self.verdict(a=self.c(availability=(0.5, 0.9)), b=self.c(availability=(0.9, 0.87)))["verdict"], "adopted")
        for costs, why in ((dict(b=self.c(availability=(0.9, 0.8))), "points of availability in `b`"),
                           (dict(b=self.c(dups=(0.0, 5.0))), "duplicate"), (dict(b=self.c(recovery_s=(0.0, 5.0))), "recovery"),
                           (dict(healthy=self.c(p99_s=(1.0, 2.0))), "healthy")):
            v = self.verdict(a=self.c(availability=(0.5, 0.9)), **costs)
            self.assertEqual(v["verdict"], "opt-in")
            self.assertTrue(any(why in w for w in v["because"]), v["because"])
        self.assertEqual(self.verdict(a=self.c(p99_s=(2.0, 1.0)), b=self.c(amplification=(1.0, 1.5)))["verdict"], "opt-in")
        self.assertEqual(self.verdict(a=self.c(amplification=(1.0, 1.5)), b=self.c(availability=(0.5, 0.9)))["verdict"], "adopted")
        bare = {"a": self.c(availability=(0.5, 0.9))}
        self.assertEqual(self.verdict(a=self.c(), bare=bare)["verdict"], "adopted")
        self.assertEqual(self.verdict(a=self.c(), bare=bare, in_stack=False)["verdict"], "opt-in")

    def test_metrics(self):
        sc = {"load": [{"dep": "x"}], "window": [10, 20], "duration_s": 30}
        calls = [[0, 1.0, "fresh", 0.1], [0, 11.0, "error", 0.2], [0, 12.0, "stale", 5.0], [0, 21.0, "late", 5.0], [0, 22.5, "fresh", 0.1], [0, 25.0, "fresh", 0.1]]
        m = lab.metrics(sc, {"calls": calls, "arrivals": {"x": [1.0, 11.0, 11.1, 11.2, 12.0, 21.0, 22.5, 25.0]}, "wasted": 3, "dups": 2})
        self.assertEqual((m["calls"], m["availability"], m["fresh"], m["p99_s"], m["dups"], m["wasted"]), (6, 4 / 6, 3 / 6, 5.0, 2, 0.5))
        self.assertAlmostEqual(m["amplification"], 8 / 6)
        self.assertEqual((m["outage_amplification"], m["outage_availability"], m["recovery_s"]), (2.0, 0.5, 2.0))

    def test_pairs_compare_one_pattern_at_a_time(self):
        vs = lab.variants()
        for n in lab.ROWS:
            for b in ("stack", "bare"):
                on, off = (vs[x] for x in lab.pair(b, n))
                self.assertEqual({k for k in on if on[k] != off[k]}, {"retry" if n == "jitter" else n}, (b, n))


class CommittedResultsTest(unittest.TestCase):
    """docs/PATTERNS.md, the registry's verdicts and examples/patterns-lab/results.json say the same."""

    @classmethod
    def setUpClass(cls):
        cls.res = json.loads((REPO / "examples" / "patterns-lab" / "results.json").read_text())

    def test_verdicts_are_the_measured_ones(self):
        self.assertEqual({p.name: p.verdict for p in patterns.all_patterns()}, {n: v for n, v in self.res["verdicts"].items() if n != "jitter"})
        self.assertEqual(set(self.res["languages"]), {"python", "node"})
        self.assertEqual(self.res["verdicts"]["jitter"], "adopted" if patterns.get("retry").params["full_jitter"] else "opt-in")

    def test_verdicts_follow_from_the_numbers(self):
        for lg, x in self.res["languages"].items():
            for n, p in x["patterns"].items():
                d = lab.decide(p["compare"], n == "jitter" or bool(lab.STACK[n]) or n == "fallback")
                self.assertEqual(d["verdict"], p["verdict"], (lg, n))

    def test_docs_are_generated_from_them(self):
        doc = (REPO / "docs" / "PATTERNS.md").read_text()
        self.assertEqual(lab.fill(doc, self.res), doc, "run: python -m ras_anything.lab docs/PATTERNS.md examples/patterns-lab/results.json")
        self.assertEqual((REPO / "examples" / "patterns-lab" / "RESULTS.md").read_text(), lab.results_md(self.res))
        for p in patterns.all_patterns():
            self.assertRegex(doc, r"### [^\n]*\(`%s`\): %s\n" % (p.name, p.verdict))

    def test_fill_empty_regions_do_not_swallow_neighbours(self):
        """Empty `<!-- lab:x -->\\n<!-- /lab -->` pairs must fill in place, not eat the next section."""
        doc = "# T\n\n<!-- lab:summary -->\n<!-- /lab -->\n\n## Verdicts\n\n<!-- lab:reasons -->\n<!-- /lab -->\n\nkept\n"
        out = lab.fill(doc, self.res)
        self.assertIn("## Verdicts", out)
        self.assertIn("kept", out)
        self.assertIn("| pattern | scenario |", out)
        self.assertIn("| pattern | verdict |", out)
        self.assertEqual(lab.fill(out, self.res), out)

    def test_every_stack_variant_keeps_the_happy_path_of_the_examples(self):
        for fid, vs in self.res["examples"].items():
            for name, r in vs.items():
                a, b = r["happy"].split("/")
                self.assertEqual(a, b, (fid, name))
            self.assertTrue(vs["stack"]["passed"], fid)

    def test_defaults_documented_are_the_defaults(self):
        doc = (REPO / "docs" / "PATTERNS.md").read_text()
        for name, key in (("circuit_breaker", "cooldown_s"), ("circuit_breaker", "fail_threshold"), ("circuit_breaker", "window"), ("retry", "attempts"),
                          ("retry_budget", "ratio"), ("retry_budget", "min_per_s"), ("retry_budget", "burst"), ("deadline", "timeout_s"),
                          ("fallback", "stale_ttl_s"), ("bulkhead", "max_concurrent"), ("adaptive_throttle", "window_s")):
            section = re.search(r"### [^\n]*\(`%s`\).*?(?=\n### |\n## )" % name, doc, re.S)[0]
            self.assertRegex(section, r"`%s`\s+\(%g" % (key, patterns.get(name).params[key]), (name, key))


class TaskGuidanceTest(unittest.TestCase):
    def test_only_adopted_patterns_are_recommended(self):
        names = {p.name: p.default_on for p in patterns.all_patterns()}
        for a, spell in ((lang.by_name("python"), lambda n: n), (lang.by_name("node"), lambda n: re.sub(r"_(\w)", lambda m: m[1].upper(), n))):
            for is_async in (True, False):
                text = a.task_texts({"async": is_async, "kind": "function", "id": "m.f", "name": "f"})["helpers"]
                listed = set(re.findall(r"^- `(\w+)`(?: \(|:)", text, re.M))
                for n, on in names.items():
                    self.assertEqual(spell(n) in listed, on, (a.name, n))
                self.assertIn("Policy(", text)
                for word in ("Bulkhead", "Throttle", "AdaptiveConcurrency", "hedge"):
                    self.assertNotIn(word, text)


class CliTest(unittest.TestCase):
    def test_bench_patterns(self):
        with tempfile.TemporaryDirectory() as d, quiet():
            from unittest import mock
            small = {k: lab.SCENARIOS[k] for k in ("healthy", "outage")}
            meant = {n: "outage" for n in lab.MEANT_FOR}
            with mock.patch.object(lab, "SCENARIOS", small), mock.patch.object(lab, "MEANT_FOR", meant), mock.patch.object(lab, "EXAMPLES", lab.EXAMPLES[:1]):
                self.assertEqual(main(["bench", "--patterns", "--seeds", "1", "--out", d]), 0)
            res = json.loads((Path(d) / "results.json").read_text())
            self.assertEqual(set(res["verdicts"]), set(lab.ROWS))
            self.assertEqual(list(res["examples"]), ["weather.fetch_forecast"])
            self.assertIn("# Pattern lab results", (Path(d) / "RESULTS.md").read_text())


if __name__ == "__main__":
    unittest.main()
