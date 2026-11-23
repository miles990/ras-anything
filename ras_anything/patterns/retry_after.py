"""Retry-After: when a 429 or 503 says when to come back, wait that long, or give up at once if that is too late."""

from __future__ import annotations

import time

from .. import runtime as rt
from . import ADOPTED, Layer, Pattern


def retry_after_s(exc: BaseException) -> float | None:
    """The Retry-After delay (seconds) an HTTP error asked for, if any."""
    headers = getattr(exc, "headers", None)
    if headers is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except Exception:
        return None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except Exception:
            return None


class RetryAfterLayer(Layer):
    """Makes the retry layer's wait at least what the server asked for (a wait past the deadline ends the call)."""

    def run(self, call, inner):
        call.delay_rules.append(lambda e, delay: max(delay, rt.retry_after_s(e) or 0.0))
        return (yield from inner(call))


PATTERN = Pattern(
    name="retry_after", title="Honour Retry-After (429/503)", order=60, verdict=ADOPTED, why="", params={},
    layer=RetryAfterLayer, exports=("retry_after_s",),
    refs=("RFC 9110 section 10.2.3", "AWS REL05-BP02: Throttle requests", "Polly v8: retry ShouldRetryAfterHeader"))
