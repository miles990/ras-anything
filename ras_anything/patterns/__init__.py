"""Resilience patterns as plugins: one module each, found through this registry (docs/PATTERNS.md).

A pattern module stands on its own: it imports ras_anything.runtime (the core) and this contract, never
another pattern, the pipeline or a language adapter. It defines

    PATTERN = Pattern(name=..., order=..., verdict=..., params={...}, ...)
    class <Layer>: how it takes part in a Policy (see Layer)

plus the helpers it offers on their own (a breaker, a budget, ...). Cooperation between patterns goes
through the `Call` a policy passes along (retry gates, delay rules, ...): with a pattern switched off or
its file missing, the others still work. The Node.js runtime has the same patterns, parameters and
semantics (lang/node_agent/patterns/; tests/test_patterns_parity.py compares them).
"""

from __future__ import annotations

import importlib
import itertools
from dataclasses import dataclass, field

# Outermost first. A new pattern is a new module and an entry here.
NAMES = ("fallback", "deadline", "bulkhead", "idempotency", "retry_budget", "retry_after", "retry", "circuit_breaker",
         "adaptive_throttle", "adaptive_concurrency", "hedge", "attempt_timeout")

ADOPTED, OPT_IN, REJECTED = "adopted", "opt-in", "rejected"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "RUN"})  # RUN: a subprocess, like the recordings call it


@dataclass(frozen=True)
class Pattern:
    name: str
    title: str
    order: int      # position in a policy: lower is further out
    verdict: str    # adopted (on unless switched off) | opt-in | rejected; measured, see docs/PATTERNS.md
    hint: str       # one line for whoever writes hardened code
    params: dict    # parameter -> default, as a Policy takes them
    layer: type     # Layer subclass
    refs: tuple = ()
    exports: tuple = ()  # what ras_anything.runtime offers of it

    @property
    def default_on(self) -> bool:
        return self.verdict == ADOPTED


class Layer:
    """One pattern inside one Policy (one dependency). State lives here; every call passes through.

    run(call, inner) is a generator. `yield from inner(call)` is the rest of the policy: its value is the
    result, its exceptions are the failures. Code never sleeps or calls the dependency itself: it yields an
    operation and the driver (sync or asyncio) carries it out:

        ("call", attempt)              run the user's function
        ("sleep", seconds)
        ("race", make, delay_s, gate)  make(i) -> generator of branch i: hedging
    """

    def __init__(self, dependency: str, params: dict):
        self.dependency = dependency
        self.params = params

    def run(self, call, inner):
        return (yield from inner(call))


@dataclass
class Call:
    """One logical call on its way through a policy: what the layers tell each other."""

    dependency: str
    method: str = "GET"
    idempotency_key: str | None = None
    cache_key: object = None
    deadline: float | None = None       # monotonic seconds; None = no limit
    timeout_s: float | None = None      # of one attempt
    numbers: object = field(default_factory=lambda: itertools.count(1))  # of its attempts, hedges included
    headers: dict = field(default_factory=dict)  # every attempt should send these
    carry_key: str | None = None        # the key attempts really send (idempotency layer)
    retry_gates: list = field(default_factory=list)   # () -> bool: may one more attempt be made?
    invited: bool = False               # the last failure said when to come back (retry_after layer): no gate is asked
    delay_rules: list = field(default_factory=list)   # (exc, delay_s) -> delay_s
    retry_rules: list = field(default_factory=list)   # (exc) -> bool: also worth a retry

    @property
    def safe(self) -> bool:
        return self.method.upper() in SAFE_METHODS

    @property
    def repeatable(self) -> bool:
        """May it be sent more than once? (a read, or every attempt carries one Idempotency-Key)"""
        return self.safe or self.carry_key is not None


def get(name: str) -> Pattern:
    if name not in NAMES:
        raise KeyError("no pattern %r (known: %s)" % (name, ", ".join(NAMES)))
    return importlib.import_module("ras_anything.patterns." + name).PATTERN


def all_patterns() -> list[Pattern]:
    """Every pattern whose module is there, outermost first."""
    out = []
    for n in NAMES:
        try:
            out.append(get(n))
        except ModuleNotFoundError as e:
            if e.name != "ras_anything.patterns." + n:
                raise
    return sorted(out, key=lambda p: p.order)


def is_on(dependency: str, pattern: str, configured: bool) -> bool:
    """Is a pattern on for this call? Asked on every call of every policy. (The one place a flag provider
    will hook into.)"""
    return configured
