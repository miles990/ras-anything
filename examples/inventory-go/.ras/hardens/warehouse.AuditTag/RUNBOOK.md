# Runbook: `warehouse.AuditTag`

- **Language:** go
- **Module / file:** `warehouse/warehouse.go`
- **Latency budget:** `5.0s`
- **Verify verdict:** PASS (`31.7% → 100.0%` availability under faults)

## Dependencies

| name | kind | method | route |
|---|---|---|---|
| `printf '{"tag":"BIN-sku-101"}'` | `subprocess` | `RUN` | `None` |
| `printf '{"tag":"BIN-sku-102"}'` | `subprocess` | `RUN` | `None` |
| `printf '{"tag":"BIN-sku-103"}'` | `subprocess` | `RUN` | `None` |
| `printf '{"tag":"BIN-sku-104"}'` | `subprocess` | `RUN` | `None` |
| `printf '{"tag":"BIN-sku-105"}'` | `subprocess` | `RUN` | `None` |
| `printf '{"tag":"BIN-sku-106"}'` | `subprocess` | `RUN` | `None` |

## Faults handled & patterns

Hardened function verified against: `timeout`, `refused`, `http5xx`, `http429`, `slow`, `truncated`, `outage`, `mixed:1`, `mixed:2`, `mixed:3`.

## Log query (OTel JSON on `stderr`)

```bash
# Filter structured JSON records for this function or failing dependency
jq 'select(.attributes["dependency.name"] != null or .severity_text == "ERROR")' app.stderr.log
# Correlate across hops using W3C traceparent / trace_id
jq 'select(.trace_id == "<32-hex-trace-id>")' app.stderr.log
```

## Health, breaker state & incident replay

- Live health & SLO check: `ras-anything status` (reads `.ras/health.json` or `$RAS_ANYTHING_HEALTH_FILE`, `.ras/slo.json`, and `.ras/incidents/`).
- Breaker open means upstream failures crossed the threshold; calls fail fast (`circuit_open` / `degraded`) until cooldown expires and a half-open probe succeeds.
- Flight-recorder incidents: `ras-anything replay --list` then `ras-anything replay <incident-id>` for deterministic virtual-clock reproduction and RCA.

## Instant rollback

```bash
ras-anything disable warehouse.AuditTag   # byte-for-byte source restore
# OR without touching files:
RAS_ANYTHING_DISABLE=1 <your-app-command>
```

## Evidence links

- Verification results: `.ras/hardens/warehouse.AuditTag/verify.json`
- Report: `RAS-REPORT.md`
- SLO status: `.ras/slo.json`
