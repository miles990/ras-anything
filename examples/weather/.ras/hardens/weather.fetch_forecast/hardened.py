"""Hardened weather.fetch_forecast, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`."""

from __future__ import annotations

import json
import os
import time
from urllib.parse import quote
from urllib.request import urlopen

from ras_anything.runtime import is_transient, log_event, retry

# What this function returns when it degrades on purpose (dependency down, budget spent).
# summarize() already renders {"status": "unavailable"} as an "unavailable" dashboard row.
RAS_FALLBACK = lambda city: {"city": city, "status": "unavailable"}  # noqa: E731

DEADLINE_S = 4.8          # hard wall for the whole call, under the 5 s contract
ATTEMPT_TIMEOUT_S = 3.5   # long enough for a slow (~3 s) answer, short enough to leave room for a retry
MIN_ATTEMPT_S = 0.2       # don't start an attempt that can't realistically finish
ATTEMPTS = 4              # rides out a short outage (3 refused attempts, then recovery)


def _retryable(exc):
    # A 2xx whose body was cut off fails to decode: treat it like any other transient fault.
    return is_transient(exc) or isinstance(exc, ValueError)


def fetch_forecast(city):
    """Three-day forecast for one city, as the provider returns it."""
    base = os.environ.get("WEATHER_API_URL", "https://api.weather.example")
    url = f"{base}/v1/forecast?city={quote(city)}"
    deadline = time.monotonic() + DEADLINE_S

    def attempt():
        # Each attempt gets whatever is left of the overall deadline, capped per attempt.
        remaining = deadline - time.monotonic()
        if remaining < MIN_ATTEMPT_S:
            raise TimeoutError(f"deadline reached before attempt ({remaining:.2f}s left)")
        with urlopen(url, timeout=min(ATTEMPT_TIMEOUT_S, remaining)) as resp:
            return json.loads(resp.read())

    try:
        # GET is idempotent, so retrying is safe; budget keeps backoff waits inside the deadline.
        return retry(attempt, attempts=ATTEMPTS, base=0.1, cap=1.0,
                     budget_s=DEADLINE_S - MIN_ATTEMPT_S, retry_on=_retryable)
    except Exception as e:
        if not _retryable(e):
            # Permanent failures (404, bad request, bugs) keep their original type for callers.
            log_event("weather.fetch_forecast.failed", city=city, url=url,
                      error=type(e).__name__, detail=str(e))
            raise
        log_event("weather.fetch_forecast.degraded", city=city, url=url,
                  error=type(e).__name__, detail=str(e), fallback="unavailable")
        return RAS_FALLBACK(city)
