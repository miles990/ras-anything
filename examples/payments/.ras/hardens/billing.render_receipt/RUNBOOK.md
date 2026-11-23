# Runbook: `billing.render_receipt`

- **Language:** python
- **Module / file:** `billing.py`
- **Latency budget:** `5.0s`
- **Verify verdict:** PASS (`33.1% → 100.0%` availability under faults)

## Dependencies

| name | kind | method | route |
|---|---|---|---|
| `python` | `subprocess` | `RUN` | `None` |

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
ras-anything disable billing.render_receipt   # byte-for-byte source restore
# OR without touching files:
RAS_ANYTHING_DISABLE=1 <your-app-command>
```

## Evidence links

- Verification results: `.ras/hardens/billing.render_receipt/verify.json`
- Report: `RAS-REPORT.md`
- SLO status: `.ras/slo.json`
