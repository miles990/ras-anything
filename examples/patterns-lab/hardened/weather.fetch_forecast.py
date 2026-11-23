"""weather.fetch_forecast on a Policy, for the pattern lab: the marked line is replaced per variant."""

import json
import os
from urllib.parse import quote
from urllib.request import urlopen

from ras_anything.runtime import Policy, is_transient

RAS_FALLBACK = lambda city: {"city": city, "status": "unavailable"}  # noqa: E731
BASE = {"deadline": {"timeout_s": 4.7}, "attempt_timeout": {"timeout_s": 3.4},
        "retry": {"attempts": 4, "retry_on": lambda e: is_transient(e) or isinstance(e, ValueError)},
        "fallback": {"degraded": lambda e: None, "when": lambda e: is_transient(e) or isinstance(e, ValueError)},
        "hedge": {"delay_s": 0.5}}
PATTERNS = {}  # ras-lab

POLICY = {k: {**BASE.get(k, {}), **(v if isinstance(v, dict) else {})} if v else False for k, v in PATTERNS.items()}
weather = Policy("weather-api", **POLICY)


def fetch_forecast(city):
    url = "%s/v1/forecast?city=%s" % (os.environ.get("WEATHER_API_URL", "https://api.weather.example"), quote(city))

    def attempt(a):
        with urlopen(url, timeout=a.timeout_s) as r:
            return json.loads(r.read())

    out = weather.call(attempt, cache_key=city)
    return RAS_FALLBACK(city) if out is None else out
