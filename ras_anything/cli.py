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

    a = p.parse_args(head)
    try:
        if a.command == "scan":
            if not cmd:
                raise UserError("no command given", "scan runs your app: put its command after --",
                                "scan -- python app.py")
            from .scan import scan

            scan(cmd, seeds=a.seeds, rate=a.rate)
        else:
            p.print_help()
            return 2
    except UserError as e:
        ui.error(e.what, e.impact, e.nxt)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
