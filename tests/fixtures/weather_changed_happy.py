"""Negative control: changes the happy-path result (verify must reject: happy replay differs)."""
import json
import os
from urllib.parse import quote
from urllib.request import urlopen

from ras_anything.runtime import retry


def fetch_forecast(city):
    base = os.environ.get("WEATHER_API_URL", "https://api.weather.example")

    def once():
        with urlopen(f"{base}/v1/forecast?city={quote(city)}", timeout=1) as resp:
            doc = json.loads(resp.read())
        doc["cached"] = False  # "harmless" extra field
        return doc
    return retry(once, attempts=4, base=0.05, cap=0.4, budget_s=4)
