"""Command line: ras-anything <command> ..."""

from __future__ import annotations

import argparse
import sys

from . import __version__, ui
from .paths import UserError


def _split(argv):
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    head, cmd = _split(argv)
    p = argparse.ArgumentParser(prog="ras-anything", description="Harden the 5% of your app that breaks.")
    p.add_argument("--version", action="version", version="ras-anything " + __version__)
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("scan", help="rank functions by risk × exposure × hardenability (-- <cmd>)")
    s.add_argument("--seeds", type=int, default=5)
    s.add_argument("--rate", type=float, default=0.25)

    h = sub.add_parser("harden", help="record real calls of one function, write TASK.md, run your agent")
    h.add_argument("id", nargs="?")
    h.add_argument("--agent", help="claude | codex | any shell command that reads the task on stdin")
    h.add_argument("--model", help="model for the agent (e.g. opus)")
    h.add_argument("--budget", type=float, help="latency budget per call in seconds (default 5)")
    h.add_argument("--calls", type=int, default=500, help="max calls to record (default 500)")
    h.add_argument("--force", action="store_true", help="overwrite an existing hardened.py with a fresh stub")

    v = sub.add_parser("verify", help="replay recorded calls: happy path, then the fault matrix")
    v.add_argument("id", nargs="?")
    v.add_argument("--chaos-calls", type=int, default=60, help="recorded calls replayed under each fault (default 60)")

    for name, text in (("enable", "swap the verified hardened function in (one marked block)"),
                       ("disable", "remove the block, restoring the file byte for byte")):
        e = sub.add_parser(name, help=text)
        e.add_argument("id", nargs="?")

    b = sub.add_parser("bench", help="whole-app runs under live chaos, original vs hardened (-- <cmd>)")
    b.add_argument("--seeds", type=int, default=20)
    b.add_argument("--rate", type=float, default=0.1, help="fault probability per dependency attempt (default 0.1)")
    b.add_argument("--degraded-marker", action="append", dest="markers",
                   help="text that marks a deliberately degraded output line (repeatable)")

    a = p.parse_args(head)
    try:
        if a.command == "scan":
            if not cmd:
                raise UserError("no command given", "scan runs your app: put its command after --",
                                "scan -- python app.py")
            from .scan import scan

            scan(cmd, seeds=a.seeds, rate=a.rate)
        elif a.command == "harden":
            from .harden import harden

            harden(a.id, agent=a.agent, model=a.model, budget=a.budget, max_calls=a.calls, force=a.force)
        elif a.command == "verify":
            from .verify import verify

            return 0 if verify(a.id, max_chaos=a.chaos_calls).get("passed") else 1
        elif a.command == "enable":
            from .enable import enable

            enable(a.id)
        elif a.command == "disable":
            from .enable import disable

            disable(a.id)
        elif a.command == "bench":
            from .bench import bench

            bench(cmd or None, seeds=a.seeds, rate=a.rate, markers=a.markers)
        else:
            p.print_help()
            return 2
    except UserError as e:
        ui.error(e.what, e.impact, e.nxt)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
