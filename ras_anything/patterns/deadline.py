"""Total deadline: the whole call, waits included, ends in time, and so does everything it calls."""

from __future__ import annotations

from .. import runtime as rt
from . import ADOPTED, Layer, Pattern


class DeadlineLayer(Layer):
    """call.deadline = now + timeout_s, or the caller's own deadline if that is earlier. The user's function
    runs with the deadline in its context, so nested policies, retry() and attempt_timeout() see it."""

    def run(self, call, inner):
        now = rt._now()
        left = rt.remaining_s()
        if left is not None and left <= 0:
            raise rt.DeadlineExceeded("the caller's deadline has passed: not calling %s" % self.dependency)
        end = now + self.params["timeout_s"]
        call.deadline = end if left is None else min(end, now + left)
        return (yield from inner(call))


PATTERN = Pattern(
    name="deadline", title="Total deadline, propagated", order=20, verdict=ADOPTED, why="", params={"timeout_s": 4.8},
    layer=DeadlineLayer, exports=("deadline", "remaining_s", "DeadlineExceeded"),
    refs=("Google SRE ch.22: deadline propagation", "Release It! 2e: Timeouts, Fail Fast", "Polly v8: total request timeout"))
