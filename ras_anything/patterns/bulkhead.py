"""Bulkhead: at most so many calls at a time into one dependency; when full, fail at once."""

from __future__ import annotations

import contextlib
import threading

from .. import runtime as rt
from . import OPT_IN, Layer, Pattern


class BulkheadFullError(rt.RejectedError):
    """Raised without calling the dependency: its compartment is full."""


class Bulkhead:
    """A semaphore per dependency that never waits: a slow dependency can't take every thread, connection or
    task of the app with it."""

    def __init__(self, max_concurrent: int = 10, name: str = "dependency"):
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self.max_concurrent, self.name = max_concurrent, name
        self._lock = threading.Lock()
        self.in_use = 0
        rt.track(self)

    def reset(self) -> None:
        pass  # calls in progress stay in progress

    def acquire(self) -> None:
        with self._lock:
            if self.in_use >= self.max_concurrent:
                raise BulkheadFullError("bulkhead full for %s" % self.name, context={"max_concurrent": self.max_concurrent})
            self.in_use += 1

    def release(self) -> None:
        with self._lock:
            self.in_use -= 1

    @contextlib.contextmanager
    def slot(self):
        self.acquire()
        try:
            yield
        finally:
            self.release()

    def call(self, fn, *args, **kwargs):
        with self.slot():
            return fn(*args, **kwargs)

    async def acall(self, fn, *args, **kwargs):
        """call() for an async fn."""
        with self.slot():
            return await fn(*args, **kwargs)


class BulkheadLayer(Layer):
    def __init__(self, dependency, params):
        super().__init__(dependency, params)
        self.bulkhead = Bulkhead(params["max_concurrent"], dependency)

    def run(self, call, inner):
        with self.bulkhead.slot():
            return (yield from inner(call))


PATTERN = Pattern(
    name="bulkhead", title="Bulkhead (concurrency cap)", order=30, verdict=OPT_IN, why="", params={"max_concurrent": 10},
    layer=BulkheadLayer, exports=("Bulkhead", "BulkheadFullError"),
    refs=("Release It! 2e: Bulkheads, Fail Fast", "resilience4j: Bulkhead (semaphore)", "Polly v8: concurrency limiter",
          "AWS REL05-BP04: Fail fast and limit queues"))
