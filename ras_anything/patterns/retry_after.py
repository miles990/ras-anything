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
    """Makes the retry layer's wait at least what the server asked for (a wait past the deadline ends the call). Such
    a retry is the server's own plan, so it doesn't need a token of the retry budget."""

    def run(self, call, inner):
        def rule(e, delay):
            asked = rt.retry_after_s(e)
            call.invited = asked is not None
            return delay if asked is None else max(delay, asked)

        call.delay_rules.append(rule)
        return (yield from inner(call))


PATTERN = Pattern(
    name="retry_after", title="Honour Retry-After (429/503)", order=60, verdict=ADOPTED,
    hint="waits as long as a 429/503 asks, or gives up at once if that is past the deadline. Nothing to do.",
    params={},
    layer=RetryAfterLayer, exports=("retry_after_s",),
    refs=("RFC 9110 section 10.2.3", "AWS REL05-BP02: Throttle requests", "Polly v8: retry ShouldRetryAfterHeader"))
