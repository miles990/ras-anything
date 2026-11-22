"""go: scan → harden the top function with an agent → verify (one retry) → enable → bench → report."""

from __future__ import annotations

import json

from . import ui
from .paths import UserError, find_project, harden_dir


def failure_note(res: dict) -> str:
    if res.get("fatal"):
        return "## verify failed last time\n\n%s:\n\n```\n%s\n```\n" % (res["fatal"]["stage"], res["fatal"]["message"][-2000:])
    failed = [k for k, v in res["gates"].items() if not v]
    parts = ["## verify failed last time: %s\n" % ", ".join(failed),
             "original: %s\nhardened: %s\n" % (json.dumps(res["original"]), json.dumps(res["hardened"]))]
    for key in ("happy", "chaos"):
        ff = res[key].get("first_failure")
        if ff:
            parts.append("first failing call (%s):\n```\n%s\n```\n" % (key, json.dumps(ff, indent=1, default=str)[:3000]))
    parts.append("Fix hardened.py so every gate passes, then run `ras-anything verify %s` again." % res["id"])
    return "\n".join(parts)


def go(cmd: list[str], agent: str, model: str | None = None, seeds: int = 20, markers=None, budget=None) -> int:
    from .bench import bench
    from .enable import enable
    from .harden import harden
    from .report import report
    from .scan import scan
    from .verify import verify

    res = scan(cmd)
    top = next((f for f in res["functions"] if f["hardenable"]), None)
    if top is None:
        raise UserError("nothing to harden", "no function both calls a dependency and can be swapped", "doctor")
    fid = top["id"]
    print()
    ui.rule()
    harden(fid, agent=agent, model=model, budget=budget)
    print()
    ui.rule()
    v = verify(fid)
    if not v.get("passed"):
        print()
        ui.rule()
        print("  %s verify failed: giving the agent the failure, once" % ui.warn(ui.WARN))
        from .harden import run_agent

        run_agent(agent, harden_dir(find_project(), fid), find_project(), model, failure_note(v))
        v = verify(fid)
        if not v.get("passed"):
            raise UserError("%s still fails verify after a retry" % fid, "nothing was enabled; your code is unchanged",
                            "verify %s" % fid)
    print()
    ui.rule()
    enable(fid)
    print()
    ui.rule()
    bres = bench(None, seeds=seeds, markers=markers)
    print()
    ui.rule()
    report()
    hs = bres["summary"]["hardened"]
    if hs["wrong_rate"] > 0 or not bres["clean_outputs_equal"]:
        print("  " + ui.warn(ui.WARN + " bench saw wrong output while %s is enabled — inspect RAS-REPORT.md or run: ras-anything disable %s" % (fid, fid)))
    return 0
