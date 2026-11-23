"""Negative control: returns the declared fallback on the happy path, with a trace_id line."""
import json
import sys

RAS_FALLBACK = lambda city: {"city": city, "status": "unavailable"}  # noqa: E731


def fetch_forecast(city):
    print(json.dumps({"event": "RAS_FALLBACK", "trace_id": "deadbeefdeadbeef"}), file=sys.stderr)
    return RAS_FALLBACK(city)
