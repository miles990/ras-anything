# Runbook: `support.triage_ticket`

- **Language:** python
- **Module / file:** `support.py`
- **Latency budget:** `5.0s`
- **Verify verdict:** PASS (`9.9% → 100.0%` availability under faults)

## Dependencies

| name | kind | method | route |
|---|---|---|---|
| `completions` | `http` | `POST` | `/v1/chat/completions` |

## Faults handled & patterns

Hardened function verified against: `timeout`, `refused`, `http5xx`, `http429`, `slow`, `truncated`, `outage`, `ai_rate_limit`, `ai_quota`, `ai_context_limit`, `ai_overloaded`, `ai_sse_truncated`, `ai_finish_length`, `ai_malformed_json`, `ai_refusal`, `ai_plausible_wrong`, `ai_tool_error`, `ai_tool_loop`, `ai_model_deprecated`, `ai_prompt_injection`, `mixed:1`, `mixed:2`, `mixed:3`, `per-key`.

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
ras-anything disable support.triage_ticket   # byte-for-byte source restore
# OR without touching files:
RAS_ANYTHING_DISABLE=1 <your-app-command>
```

## Evidence links

- Verification results: `.ras/hardens/support.triage_ticket/verify.json`
- Report: `RAS-REPORT.md`
- SLO status: `.ras/slo.json`
