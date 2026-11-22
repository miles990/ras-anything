"""Forecast client for the dashboard."""

import json
import os
from urllib.parse import quote
from urllib.request import urlopen


def fetch_forecast(city):
    """Three-day forecast for one city, as the provider returns it."""
    base = os.environ.get("WEATHER_API_URL", "https://api.weather.example")
    resp = urlopen(f"{base}/v1/forecast?city={quote(city)}")
    return json.loads(resp.read())


def summarize(forecast):
    """One dashboard row: today's high/low and the wettest day."""
    if forecast.get("status") == "unavailable":
        return "unavailable"
    days = forecast["daily"]
    wettest = max(days, key=lambda d: d["rain_mm"])
    today = days[0]
    return f"{today['hi_c']:>3}°/{today['lo_c']:>3}°  wettest {wettest['day']} {wettest['rain_mm']:>4.1f} mm"
