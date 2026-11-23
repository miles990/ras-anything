# `ras-anything` Visualization & Telemetry Data Contract (V-19)

Versioned JSON Schemas (Draft `2020-12`) for `.ras/out/` snapshots, append-only timeline events, Prometheus metrics, and OTLP-JSON collector export.

## Schemas

| File | Purpose | Applies to |
|---|---|---|
| [`ras-run.schema.json`](ras-run.schema.json) | Snapshot contract (`scan`, `verify`, `bench`, `slo`, `report`, `status`, `replay`, `coverage`, `patterns`, `bundle`) | `.ras/out/*.json` & `ras-anything export --format json` |
| [`ras-event.schema.json`](ras-event.schema.json) | Append-only timeline event record (`fault`, `breaker`, `canary`, `promote`, `rollback`, `ai_guard`, `incident`) | `.ras/out/events.ndjson` & `ras-anything export --format ndjson` |

## `schema_version` Compatibility Rules

Every emitted document carries `schema_version` (`MAJOR.MINOR.PATCH`, currently **`1.0.0`**) and `kind`:
1. **`PATCH` (`1.0.x`)**: documentation or tightening clarifications with no breaking payload changes.
2. **`MINOR` (`1.x.0`)**: backward-compatible additive fields or new optional `kind` values; UIs/collectors ignoring unknown properties remain compatible.
3. **`MAJOR` (`2.0.0`)**: breaking field renames, removals, or semantic unit changes (`seconds` vs `ms`, `0..1` ratios vs `0..100` percents).

## Stable `.ras/out/` File Layout

```text
.ras/out/
  scan.json
  verify-<function_id>.json
  bench.json
  slo.json
  report.json
  status.json
  replay-<incident_id>.json
  coverage.json
  events.ndjson
```

All paths inside `.ras/out/` are scrubbed of host absolute directories (`/workspace/...`, `/home/...`) so snapshots are portable across machines.

## CLI & MCP Export

```bash
ras-anything export --format json
ras-anything export --format ndjson
ras-anything export --format prom
ras-anything export --format otlp-json
```

- **MCP tools**:
  - `ras_export`: returns the formatted `json`, `ndjson`, `prom`, or `otlp-json` payload.
  - `ras_events`: reads `.ras/out/events.ndjson` incrementally via `since` sequence cursor and `limit`.

## Prometheus Metric Names (`--format prom`)

| Metric | Type | Unit / Range |
|---|---|---|
| `ras_availability_ratio` | `gauge` | `0..1` (`variant="original"\|"hardened"`) |
| `ras_identical_ratio` | `gauge` | `0..1` |
| `ras_crash_rate` / `ras_hang_rate` | `gauge` | `0..1` |
| `ras_p99_seconds` | `gauge` | seconds |
| `ras_duplicates_total` | `gauge` | count |
| `ras_error_budget_burn_ratio` | `gauge` | ratio (`1.0` = 100% burn) |
| `ras_ai_semantic_success_ratio` | `gauge` | `0..1` |
| `ras_ai_injection_resistance_ratio` | `gauge` | `0..1` |
| `ras_ai_cost_per_success_usd` | `gauge` | USD |
| `ras_ax_accuracy_ratio` | `gauge` | `0..1` |
| `ras_serviceability_detection_ratio` | `gauge` | `0..1` |

## Sample Dashboard

See [`examples/dashboard-sample/`](../../examples/dashboard-sample/README.md) for real `.ras/out/` exports gathered from verified example runs and a single-file interactive `index.html` dashboard.
