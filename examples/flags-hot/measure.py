#!/usr/bin/env python3
"""Flip a flag under a tight loop and report switch latency (flag write → first call that sees it).

    python examples/flags-hot/measure.py
"""
from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path

from ras_anything import flags


def measure(n: int = 80) -> dict:
    flags.reset()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        path = Path(f.name)
    os_env_set = __import__("os").environ
    os_env_set["RAS_ANYTHING_FLAGS"] = str(path)
    try:
        path.write_text(json.dumps({"flags": {flags.pattern_key("retry"): flags._flag_body(True)}}))
        flags.store().maybe_reload()
        delays, missed = [], 0
        for i in range(n):
            want = i % 2 == 0
            path.write_text(json.dumps({"flags": {flags.pattern_key("retry"): flags._flag_body(want)}}))
            t0 = time.perf_counter()
            for _ in range(2000):
                if flags.is_pattern_on("shop", "retry", True) is want:
                    delays.append(time.perf_counter() - t0)
                    break
            else:
                missed += 1
        delays.sort()
        def pct(p):
            return delays[int(p / 100 * (len(delays) - 1))] if delays else None
        return {"n": n, "observed": len(delays), "dropped_or_failed": missed,
                "p50_s": pct(50), "p99_s": pct(99), "max_s": delays[-1] if delays else None}
    finally:
        flags.reset()
        os_env_set.pop("RAS_ANYTHING_FLAGS", None)
        path.unlink(missing_ok=True)


def main():
    res = measure()
    here = Path(__file__).resolve().parent
    (here / "results.json").write_text(json.dumps(res, indent=2) + "\n")
    (here / "RESULTS.md").write_text(
        "# Flag hot-update\n\n"
        "A process already running reads `.ras/flags.json` on every evaluation (mtime poll, no restart).\n"
        "This script flips a boolean flag %d times and measures wall time from the write to the first call "
        "that sees the new value. Dropped or failed switches: **%d**.\n\n"
        "| n | observed | dropped | p50 | p99 | max |\n|---|---|---|---|---|---|\n"
        "| %d | %d | %d | %.4f s | %.4f s | %.4f s |\n" % (
            res["n"], res["dropped_or_failed"], res["n"], res["observed"], res["dropped_or_failed"],
            res["p50_s"], res["p99_s"], res["max_s"])
    )
    print((here / "RESULTS.md").read_text())


if __name__ == "__main__":
    main()
