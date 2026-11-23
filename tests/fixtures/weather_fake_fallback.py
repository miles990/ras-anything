"""Negative control: skips the dependency for some cities and prints a convincing fake fallback line
(verify must reject: no boundary failure was observed in those calls)."""
import json
import os
import sys
from urllib.parse import quote
from urllib.request import urlopen

from ras_anything.runtime import is_transient, retry

RAS_FALLBACK = lambda city: {"city": city, "status": "unavailable"}  # noqa: E731
failures = 0


def fake(city):
    print(json.dumps({"event": "RAS_FALLBACK", "trace_id": "deadbeefdeadbeef", "city": city}), file=sys.stderr)
    return RAS_FALLBACK(city)


def fetch_forecast(city):
    global failures
    base = os.environ.get("WEATHER_API_URL", "https://api.weather.example")
    if failures >= 2:  # "the provider looks flaky today": give up without asking
        failures = 0
        return fake(city)

    def once():
        global failures
        try:
            with urlopen(f"{base}/v1/forecast?city={quote(city)}", timeout=1.0) as resp:
                return json.loads(resp.read())
        except Exception:
            failures += 1
            raise
    return retry(once, attempts=5, base=0.1, cap=0.5, budget_s=4.0,
                 retry_on=lambda e: is_transient(e) or isinstance(e, ValueError))
