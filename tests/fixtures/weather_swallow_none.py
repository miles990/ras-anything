"""Negative control: swallows everything and returns None (verify must reject: silent wrong data)."""
import json
import os
from urllib.parse import quote
from urllib.request import urlopen


def fetch_forecast(city):
    base = os.environ.get("WEATHER_API_URL", "https://api.weather.example")
    try:
        with urlopen(f"{base}/v1/forecast?city={quote(city)}", timeout=1) as resp:
            return json.loads(resp.read())
    except Exception:
        return None
