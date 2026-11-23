"""Command line: ras-anything <command> ..."""

from __future__ import annotations

import argparse
import sys

from . import __version__, ui
from .paths import UserError

USAGE = """\
Find the functions that break when dependencies misbehave, have a coding agent harden only those,
prove it on recorded calls, and swap them in reversibly.

  ras-anything go --agent claude -- python app.py     everything below, in one go
  ras-anything go --agent claude -- node app.mjs

  scan -- <cmd>          rank functions by risk × exposure × hardenability (clean + seeded chaos runs)
  harden [id] [--agent]  record real calls + their dependency I/O, write TASK.md, run your agent
  verify [id]            replay: happy path must be identical, then the fault matrix vs the original
  enable / disable [id]  swap the hardened function in / out (one marked block, byte-for-byte undo)
  bench [-- <cmd>]       whole-app runs under live chaos, original vs hardened, interleaved
  bench --patterns       every resilience pattern on vs off under simulated trouble (docs/PATTERNS.md)
  report                 RAS-REPORT.md + ras-card.svg (measured numbers only)
  status · doctor · mcp
"""


def _split(argv):
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ras-anything", description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="ras-anything " + __version__)
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("scan", help="rank functions by risk × exposure × hardenability (-- <cmd>)")
    s.add_argument("--seeds", type=int, default=5, help="chaos runs (default 5)")
    s.add_argument("--rate", type=float, default=0.25, help="fault probability per dependency attempt (default 0.25)")

    h = sub.add_parser("harden", help="record real calls of one function, write TASK.md, run your agent")
    h.add_argument("id", nargs="?")
    h.add_argument("--agent", help="claude | codex | any shell command that reads the task on stdin")
    h.add_argument("--model", help="model for the agent (e.g. opus)")
    h.add_argument("--budget", type=float, help="latency budget per call in seconds (default 5)")
    h.add_argument("--calls", type=int, default=500, help="max calls to record (default 500)")
    h.add_argument("--force", action="store_true", help="overwrite the existing hardened code with a fresh stub")

    v = sub.add_parser("verify", help="replay recorded calls: happy path, then the fault matrix")
    v.add_argument("id", nargs="?")
    v.add_argument("--chaos-calls", type=int, default=60, help="recorded calls replayed under each fault (default 60)")

    for name, text in (("enable", "swap the verified hardened function in (one marked block)"),
                       ("disable", "remove the block, restoring the file byte for byte")):
        e = sub.add_parser(name, help=text)
        e.add_argument("id", nargs="?")

    b = sub.add_parser("bench", help="whole-app runs under live chaos, original vs hardened (-- <cmd>)")
    b.add_argument("--seeds", type=int, default=20)
    b.add_argument("--patterns", action="store_true", help="measure the resilience patterns instead, each on vs off, in the pattern lab")
    b.add_argument("--out", help="with --patterns: folder for results.json and RESULTS.md (default .ras/patterns-lab)")
    b.add_argument("--rate", type=float, default=0.1, help="fault probability per dependency attempt (default 0.1)")
    b.add_argument("--degraded-marker", action="append", dest="markers",
                   help="text that marks a deliberately degraded output line (repeatable)")

    r = sub.add_parser("report", help="RAS-REPORT.md + ras-card.svg")
    r.add_argument("--title", help="project name on the card (default: folder name)")

    sub.add_parser("status", help="what's scanned, hardened, verified, enabled; the next step")
    sub.add_parser("doctor", help="check python, agents, git, .ras")
    sub.add_parser("mcp", help="stdio MCP server")

    g = sub.add_parser("go", help="scan → harden → verify → enable → bench → report (-- <cmd>)")
    g.add_argument("--agent", required=True, help="claude | codex | any shell command that reads the task on stdin")
    g.add_argument("--model")
    g.add_argument("--seeds", type=int, default=20)
    g.add_argument("--budget", type=float)
    g.add_argument("--degraded-marker", action="append", dest="markers")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    head, cmd = _split(argv)
    p = parser()
    a = p.parse_args(head)
    try:
        c = a.command
        if c in ("scan", "go") and not cmd:
            raise UserError("no command given", "%s runs your app: put its command after --" % c,
                            "%s -- python app.py / node app.mjs" % ("scan" if c == "scan" else "go --agent claude"))
        if c == "scan":
            from .scan import scan

            scan(cmd, seeds=a.seeds, rate=a.rate)
        elif c == "harden":
            from .harden import harden

            harden(a.id, agent=a.agent, model=a.model, budget=a.budget, max_calls=a.calls, force=a.force)
        elif c == "verify":
            from .verify import verify

            return 0 if verify(a.id, max_chaos=a.chaos_calls).get("passed") else 1
        elif c == "enable":
            from .enable import enable

            enable(a.id)
        elif c == "disable":
            from .enable import disable

            disable(a.id)
        elif c == "bench":
            if a.patterns:
                from pathlib import Path

                from . import lab

                lab.run(a.seeds, Path(a.out) if a.out else None)
                return 0
            from .bench import bench

            bench(cmd or None, seeds=a.seeds, rate=a.rate, markers=a.markers)
        elif c == "report":
            from .report import report

            report(a.title)
        elif c == "status":
            from .doctor import status

            status()
        elif c == "doctor":
            from .doctor import doctor

            return doctor()
        elif c == "mcp":
            from .mcp import serve

            return serve()
        elif c == "go":
            from .go import go

            return go(cmd, a.agent, model=a.model, seeds=a.seeds, markers=a.markers, budget=a.budget)
        else:
            p.print_help()
            return 2
    except UserError as e:
        ui.error(e.what, e.impact, e.nxt)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
