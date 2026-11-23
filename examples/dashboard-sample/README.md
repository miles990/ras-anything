# `examples/dashboard-sample/` — Visualization Data Contract & Sample Dashboard (V-19)

Real `.ras/out/` snapshots exported from verified repository runs (`support-ai`, `inventory-go`, `orders-node`, `payments`, `weather`, plus `patterns-lab` & `flags-hot`), validated against [`docs/schema/ras-run.schema.json`](../../docs/schema/ras-run.schema.json) and [`docs/schema/ras-event.schema.json`](../../docs/schema/ras-event.schema.json).

## Files

- `index.html`: single-file standalone HTML dashboard (works offline via `file://` with the embedded real bundle, or fetches `./out/bundle.json` over HTTP).
- `out/bundle.json`: consolidated multi-project snapshot (`schema_version: "1.0.0"`, `kind: "bundle"`).
- `out/patterns.json`: real ON/OFF pattern measurements from `examples/patterns-lab/results.json`.
- `out/metrics.prom`: Prometheus text exposition across all sample projects.
- `out/<project>/`: per-project `.ras/out/` (`scan.json`, `verify-*.json`, `bench.json`, `slo.json`, `report.json`, `status.json`, `coverage.json`, `events.ndjson`, `metrics.prom`, `otlp.json`).

## For UI Developers

1. Open `index.html` directly in any browser, or run `python3 -m http.server` inside `examples/dashboard-sample/` to inspect live `./out/*.json` loading.
2. Poll incremental events using MCP `ras_events(cwd=..., since=cursor)` or read `.ras/out/events.ndjson`.
3. See [`docs/schema/README.md`](../../docs/schema/README.md) for `schema_version` compatibility guarantees.
