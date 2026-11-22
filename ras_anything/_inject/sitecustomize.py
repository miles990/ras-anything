"""ras-anything: watch the dependency boundary of the app being scanned, recorded or benchmarked.

While ras-anything runs your command it puts this folder first on PYTHONPATH, so the app's Python
process loads it at startup. It does nothing unless RAS_ANYTHING_CFG is set (and removes that
variable, so processes the app starts are left alone). Your own sitecustomize, if any, still runs.
"""

import os
import sys


def _ras_anything_start():
    if not os.environ.get("RAS_ANYTHING_CFG"):
        return
    try:
        from ras_anything import _agent

        _agent.start()
    except Exception as e:  # never break the app
        sys.stderr.write("ras-anything: can't watch this process: %r\n" % (e,))


def _ras_anything_chain():
    import importlib.util
    from importlib.machinery import PathFinder

    here = os.path.dirname(os.path.abspath(__file__))
    spec = PathFinder.find_spec("sitecustomize", [p for p in sys.path if os.path.abspath(p or ".") != here])
    if spec is not None and spec.loader is not None:
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


_ras_anything_start()
_ras_anything_chain()
