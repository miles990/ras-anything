# ras-anything as a library

The hardened code ras-anything writes imports one module: `ras_anything.runtime`. You can import it yourself.

```python
from ras_anything.runtime import Policy, retry

# one function: retry transient errors with full-jitter backoff
data = retry(lambda: http_get(url), attempts=3, dependency="weather-api")

# a dependency's whole policy (patterns on by default, or switched with keyword arguments)
weather = Policy("weather-api", retry={"attempts": 3})
data = weather.call(lambda attempt: http_get(url), method="GET")  # attempt: ras_anything.runtime.Attempt
```

- Public API: `ras_anything.runtime.__all__` (60 names). Snapshot: [docs/api/runtime-api.json](api/runtime-api.json), checked by `tests/test_library_api.py`.
- Typed: the package ships `py.typed`.
- No side effects on import; no runtime dependencies.
- Compatibility: SemVer. Removing or renaming a public name or parameter is a major version; adding one is minor. Snapshot contracts in `docs/schema/` follow the same rule (`schema_version`).
- Not published to PyPI (decision of the owner). Install from the repo: `pip install -e .`.
