"""Negative control: after any failed attempt it returns the fallback even though the retry succeeded
(verify must reject: the answer was there)."""
import json
import os
from urllib.parse import quote
from urllib.request import urlopen

from ras_anything.runtime import is_transient, log_event, retry

RAS_FALLBACK = lambda city: {"city": city, "status": "unavailable"}  # noqa: E731


def fetch_forecast(city):
    base = os.environ.get("WEATHER_API_URL", "https://api.weather.example")
    failed = []

    def once():
        try:
            with urlopen(f"{base}/v1/forecast?city={quote(city)}", timeout=1.0) as resp:
                return json.loads(resp.read())
        except Exception as e:
            failed.append(e)
            raise
    doc = retry(once, attempts=5, base=0.1, cap=0.5, budget_s=4.0,
                retry_on=lambda e: is_transient(e) or isinstance(e, ValueError))
    if failed:
        log_event("forecast_unavailable", city=city)
        return RAS_FALLBACK(city)
    return doc
