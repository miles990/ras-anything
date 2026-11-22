"""Test fixture: a correct hardening of examples/weather fetch_forecast (positive control for tests)."""

import json
import os
from urllib.parse import quote
from urllib.request import urlopen

from ras_anything.runtime import RasError, is_transient, log_event, retry


def RAS_FALLBACK(city):
    return {"city": city, "status": "unavailable"}


def fetch_forecast(city):
    base = os.environ.get("WEATHER_API_URL", "https://api.weather.example")
    url = f"{base}/v1/forecast?city={quote(city)}"

    def once():
        with urlopen(url, timeout=1.0) as resp:
            return json.loads(resp.read())

    try:
        return retry(once, attempts=5, base=0.1, cap=0.8, budget_s=4.0,
                     retry_on=lambda e: is_transient(e) or isinstance(e, ValueError))
    except Exception as e:
        if not (is_transient(e) or isinstance(e, ValueError)):
            raise
        log_event("forecast_unavailable", city=city, error=repr(e))
        return RAS_FALLBACK(city)
