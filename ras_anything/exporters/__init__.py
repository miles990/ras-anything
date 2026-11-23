"""Export formats as discovered components: one module each (CONTRIBUTING, docs/adr/0002).

A format module defines ``FORMAT`` (the ``--format`` name), optional ``ORDER`` (lower first),
and ``render(project, od, bundle, snapshots, events) -> str``. discover() finds them; nothing
here lists them. A new format is a new file — contracts, the CLI, MCP and export pick it up
(tests/test_contracts.py checks they agree).
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable


def discover() -> list[tuple[str, int, Callable]]:
    found: dict[str, tuple[int, Callable]] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module("%s.%s" % (__name__, info.name))
        fmt = getattr(mod, "FORMAT", None)
        render = getattr(mod, "render", None)
        if isinstance(fmt, str) and callable(render):
            found.setdefault(fmt, (int(getattr(mod, "ORDER", 100)), render))
    return [(name, order, fn) for name, (order, fn) in found.items()]


def _load() -> tuple[tuple[str, ...], dict[str, Callable]]:
    items = sorted(discover(), key=lambda row: (row[1], row[0]))
    return tuple(name for name, _, _ in items), {name: fn for name, _, fn in items}


FORMATS, RENDERERS = _load()
