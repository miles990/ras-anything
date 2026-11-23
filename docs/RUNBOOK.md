# Operations Runbook

Guide for operating applications hardened by `ras-anything`. Each hardened project also receives an auto-generated `docs/RUNBOOK.md` and per-function `.ras/hardens/<id>/RUNBOOK.md` when `ras-anything report` runs.

## 1. Live status & health snapshot

```sh
ras-anything status
```

Shows:
- Verified and enabled swaps (`.ras/state.json`)
- Latest health snapshot (`.ras/health.json` or `$RAS_ANYTHING_HEALTH_FILE`): overall status and per-dependency circuit-breaker states (`closed`, `open`, `half-open`)
- Current SLI/SLO verdict and error-budget burn (`.ras/slo.json`)
- Recorded flight-recorder incidents (`.ras/incidents/`)

Inside Python or Node code, `ras_anything.runtime.health()` / `runtime.health()` returns the same live snapshot without network I/O.

## 2. Structured OTel JSON logs (`stderr`)

Hardened runtime events are emitted as newline-delimited JSON on `stderr` following the OpenTelemetry Log Data Model (`Timestamp`, `SeverityText`, `EventName`, `TraceId`, `SpanId`, `Body`, `Resource`, `Attributes`) with W3C `traceparent` propagation (`trace_headers()`).

```sh
# Find failures and gave-up events with dependency and fault class
jq 'select(.SeverityText == "ERROR" or .Attributes["ras.fault"] != null)' app.stderr.log

# Follow one distributed trace end-to-end
jq 'select(.TraceId == "<32-hex-trace-id>")' app.stderr.log
```

## 3. Incident flight recorder & deterministic replay

Whenever a hardened function gives up on a dependency failure or raises an unhandled exception (and `.ras/` or `$RAS_ANYTHING_INCIDENTS` is present), the bounded flight recorder writes a PII-redacted incident file to `.ras/incidents/inc-<id>.json`.

```sh
ras-anything replay --list          # list recorded incidents
ras-anything replay <incident-id>   # replay on the virtual clock + write RCA
```

`replay` matches the call in `.ras/hardens/<id>/calls.json`, reconstructs the failure schedule and RNG seed, replays on the virtual clock without hitting the network, and writes `.ras/out/replay-<id>.json` + `.ras/incidents/<id>.rca.md` with the decision chain, `first_divergent_decision`, and `suggested_fix`.

## 4. SLO targets & error-budget burn

Configure SLO targets via `--slo availability=99.0,p99=1s,error_rate=1%` on `bench` or `report`, or edit `.ras/slo.json` directly.

## 5. Instant rollback

```sh
ras-anything disable <id>    # restores the source file byte-for-byte
# OR without modifying files:
RAS_ANYTHING_DISABLE=1 <your-app-command>
```
