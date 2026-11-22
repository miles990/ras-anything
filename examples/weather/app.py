"""12-city weather dashboard.

Runs its own local forecast API in a thread (upstream.py) so it works offline.
"""

import os

import upstream
import weather

CITIES = ["Taipei", "Tokyo", "Seoul", "Singapore", "Sydney", "Mumbai",
          "Berlin", "London", "Reykjavik", "New York", "Lima", "Nairobi"]


def main():
    os.environ["WEATHER_API_URL"] = upstream.start()
    rows = []
    for city in CITIES:
        rows.append((city, weather.summarize(weather.fetch_forecast(city))))
    print("WEATHER DASHBOARD")
    for city, line in rows:
        print(f"  {city:<10} {line}")
    live = sum(1 for _, line in rows if line != "unavailable")
    print(f"{len(rows)} cities · {live} live · {len(rows) - live} unavailable")


if __name__ == "__main__":
    main()
