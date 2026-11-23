"""Attempt timeout: one attempt gets a limited time, cut to what is left of the deadline."""

from __future__ import annotations

from .. import runtime as rt
from . import ADOPTED, Layer, Pattern


def attempt_timeout(timeout_s: float | None = None) -> float | None:
    """The timeout for a boundary call made now: timeout_s, or less when the deadline around this code ends
    sooner. Raises DeadlineExceeded when nothing is left. None only if there is no limit at all."""
    left = rt.remaining_s()
    if left is None:
        return timeout_s
    if left <= 0:
        raise rt.DeadlineExceeded("no time left before the deadline")
    return left if timeout_s is None else min(timeout_s, left)


class AttemptTimeoutLayer(Layer):
    """Sets attempt.timeout_s. The async driver enforces it; in blocking code give it to the boundary call."""

    def run(self, call, inner):
        before = call.timeout_s
        call.timeout_s = self.params["timeout_s"]
        left = rt.remaining_s() if call.deadline is None else call.deadline - rt._now()
        if left is not None:
            if left <= 0:
                raise rt.DeadlineExceeded("no time left for an attempt of %s" % self.dependency)
            call.timeout_s = min(call.timeout_s, left)
        try:
            return (yield from inner(call))
        finally:
            call.timeout_s = before


PATTERN = Pattern(
    name="attempt_timeout", title="Attempt timeout", order=120, verdict=ADOPTED, why="", params={"timeout_s": 3.5},
    layer=AttemptTimeoutLayer, exports=("attempt_timeout",),
    refs=("Release It! 2e: Timeouts", "AWS Builders' Library: Timeouts, retries, and backoff with jitter",
          "AWS REL05-BP05: Set client timeouts", "Polly v8 / resilience4j TimeLimiter / Failsafe: Timeout"))
