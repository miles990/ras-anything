# AI-Dependency Resilience & Landscape Decisions (V-14 / V-15)

`ras-anything` extends its deterministic record/replay chaos engine to LLM and agentic-tool dependencies (`POST /v1/chat/completions`, `/v1/responses`, `/v1/messages`, plus custom endpoints declared in `ras.ai.json`) with `$0` external API spend during verification.

---

## 1. AI Fault Model & Toxiproxy Mapping (V-14)

Standard TCP/HTTP faults (`timeout`, `refused`, `http5xx`, `http429`, `slow`, `truncated`, `outage`, `mixed`) remain active on all endpoints. When recorded cassettes contain AI endpoints (`req.ai`), `ras-anything verify` additionally runs **13 AI-specific failure modes** (`ras_anything/ai.py`):

| Fault | Simulated boundary behavior | Expected hardened response |
|---|---|---|
| `ai_rate_limit` | HTTP `429` + `Retry-After: 1` (`rate_limit_exceeded`) | Honor `Retry-After` within deadline, then fallback model |
| `ai_quota` | HTTP `429` (`insufficient_quota`) | **Never retry same key/model**; immediately fail over or degrade |
| `ai_context_limit` | HTTP `400` (`context_length_exceeded`) | Route to `context_window_fallbacks` or trim context |
| `ai_overloaded` | HTTP `529` (`overloaded_error`) | Bounded backoff + fallback model |
| `ai_sse_truncated` | HTTP `200` `text/event-stream` cut mid-JSON | Reject incomplete payload; retry / fallback |
| `ai_finish_length` | HTTP `200` `finish_reason="length"` (cut-off JSON) | Reject truncated completion; retry / fallback |
| `ai_malformed_json` | HTTP `200` non-JSON / prose-wrapped JSON | `SchemaValidationError` -> bounded retry / fallback |
| `ai_refusal` | HTTP `200` `finish_reason="content_filter"` + `refusal` | Route to `content_policy_fallbacks` or safe escalation |
| `ai_plausible_wrong` | HTTP `200` schema-shaped empty placeholder (`{"answer":""}`) | `nonEmpty`/`minLength` schema check rejects silent bad data |
| `ai_tool_error` | HTTP `200` requesting forbidden tool (`refund_all`) | `ToolAllowlist` blocks & logs `ras.ai.tool_blocked` |
| `ai_tool_loop` | HTTP `200` repeating identical `tool_calls` indefinitely | `LoopGuard` + `MAX_TRIAL_ATTEMPTS` trips safely (`0` hangs) |
| `ai_model_deprecated` | HTTP `404` (`model_not_found`) | Skip retries on dead model id; fall back immediately |
| `ai_prompt_injection` | Untrusted doc + injected `refund_all` tool call | `frame_untrusted` + `ToolAllowlist` -> `100%` injection resistance |

### Toxiproxy Transport Mapping (`ras_anything/faults.py:TOXIPROXY_MAP`)

| RAS / AI fault | Toxiproxy toxic | Why RAS also needs L7 boundary injection |
|---|---|---|
| `slow` | `latency` | Transport delay works identically at TCP proxy |
| `truncated` | `slicer` | Splits TCP payload mid-frame |
| `timeout` | `timeout` | Blocks socket without response |
| `refused` | `down` | Closes upstream listener |
| `ai_sse_truncated` | `limit_data` | Cuts SSE stream after *N* bytes |
| `ai_rate_limit`, `ai_quota`, `ai_context_limit`, `ai_malformed_json`, `ai_tool_error` | *(L7 only)* | TCP proxies cannot synthesize valid JSON `429`/`400` or `tool_calls` payloads |

---

## 2. Runtime Helpers (Python & Node Parity)

Both Python (`ras_anything/patterns/ai.py`, re-exported from `ras_anything.runtime`) and Node (`ras_anything/lang/node_agent/patterns/ai.cjs`, re-exported from `runtime.cjs`) provide identical stdlib-only primitives that work on `ras-anything verify`'s virtual clock and are automatically vendored on `enable`:

- **`AIPolicy` / `AIPolicy`**: ordered fallback chain (`models`), `context_window_fallbacks`, `content_policy_fallbacks`, per-attempt timeout, overall deadline, retry classification (`insufficient_quota` non-retryable, `Retry-After` honored), and schema validation.
- **`TokenBudget`**: enforces `max_tokens`, `max_requests`, and `max_cost_usd` ceilings (`BudgetExceededError`).
- **`ToolAllowlist`**: allowlist + JSON-schema argument validation + explicit `forbidden` set (`ToolNotAllowedError`, `ras.ai.tool_blocked`).
- **`LoopGuard`**: bounds max agent steps and repeated identical `(tool, args)` calls (`LoopGuardError`, `ras.ai.loop_guard`).
- **`validate_schema` / `validateSchema`**: zero-dep JSON Schema subset (`type`, `required`, `properties`, `additionalProperties`, `nonEmpty`, `minLength`, `enum`, `items`).
- **`frame_untrusted` / `frameUntrusted`**: escapes closing tags inside retrieved documents so prompt-injection payloads stay isolated inside `<UNTRUSTED_*>` data fences.
- **`ExactCache` / `StepJournal`**: TTL prompt cache and atomic file-backed durable step journal (`$0` / `0`-token resume on crash retry).

### Turn-Based Replay Matching

Hardening an LLM call legitimately changes the HTTP request body (adding untrusted framing, `max_tokens`, tightened system prompt, or switching from `"small"` to `"backup"`). In `ras_anything/agents/python/worker.py` and `ras_anything/lang/node_agent/worker.cjs`, `Replay.match` tries exact `key` (`method + logical_url + #body_sha`) first; for AI endpoints only, it falls back to matching `(method, logical_url, has_tool_result)` against recorded AI exchanges, while still raising `ReplayMiss` for any unrecorded URL or non-AI endpoint.

Note on idempotency: pure completion endpoints (`req.ai is not None`) are treated as stateless inference (`nonidempotent = False`) so retrying a timed-out completion on a fallback model is accounted via `retry_cost_amplification` and `cost_per_success_usd` rather than falsely flagged as a duplicate side effect (mutating tool endpoints remain `nonidempotent = True`).

---

## 3. OTel GenAI Semantic Conventions & `drift`

`AIPolicy` logs structured OpenTelemetry GenAI events (`gen_ai.client.operation`, pinned to OTel GenAI Semconv `v1.42.0` field names):
- `gen_ai.operation.name` (`"chat"`)
- `gen_ai.provider.name`
- `gen_ai.request.model` / `gen_ai.response.model`
- `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`
- `gen_ai.response.finish_reasons`

`ras-anything drift --model <candidate> [--eval <suite>@v1] [--export-promptfoo <file.yaml>]` replays recorded eval suites offline (or against a local/configured `--provider-url`), compares semantic success rate and USD cost delta, and exports `promptfoo`-compatible YAML configs.

---

## 4. Measured Results: `examples/support-ai/` (`$0` Local Mock)

Measured with `ras-anything` on `examples/support-ai/` (`support.triage_ticket`, 3 recorded tickets incl. an injected `issue_refund` prompt-injection payload, local `mock_provider.py`):

| Metric | Original | Hardened | Source |
|---|---:|---:|---|
| Verify happy-path replay | — | **3/3 identical** | `.ras/hardens/support.triage_ticket/verify.json` |
| Verify availability (111 trials, 24 scenarios incl. 13 AI faults) | 9.9% | **100.0%** | `verify.json` |
| Verify AI semantic success | 9.9% | **100.0%** | `verify.json` |
| Verify AI injection resistance | 100.0% | **100.0%** | `verify.json` |
| Verify cost per success (USD) | $0.000188 | **$0.000055** | `verify.json` |
| Verify hangs / p99 latency | 12 / 60.00s | **0 / 3.72s** | `verify.json` |
| Whole-app 20-seed bench availability | 50.0% | **100.0%** | `.ras/bench.json` |
| Whole-app bench crashes / hangs | 40.0% / 10.0% | **0.0% / 0.0%** | `.ras/bench.json` |
| SLO verdict (`avail≥99%`, `p99≤5s`, `err≤1%`) | ✗ missed (5000% burn) | **✓ met (0% burn)** | `.ras/slo.json` |
| Model drift (`small` -> `backup`, 6 cases) | 100.0% ($0.000090) | **100.0% ($0.000324, PASS)** | `.ras/drift/support_v1-backup.json` |

Durable step checkpointing (`StepJournal`) crash-resume measurement (`tests/test_ai_pipeline.py`): resuming a 2-step workflow after step 2 fails replays step 1 from disk with **0 extra LLM requests** and **50% cost reduction** on retry (`$0` spent on the completed step).

---

## 5. V-15 Ecosystem Research & Decisions (Adopt / Integrate / Reject)

| External tool / standard | Decision | Rationale & Concrete Boundary |
|---|---|---|
| **LiteLLM** | **Reject** runtime dep / **Adopt** fallback taxonomy | Avoids running a heavy Python proxy/async server inside target apps; `AIPolicy` implements ordered `models`, `context_window_fallbacks`, `content_policy_fallbacks`, and token/USD budgets in pure stdlib (`ai.py` / `ai.cjs`). |
| **Portkey / Helicone** | **Reject** gateway proxy / **Adopt** config & metrics | `ras.ai.json` + `ExactCache` + `cost_per_success_usd` & `retry_cost_amplification` provide the same gateway observability locally without external SaaS routing. |
| **Instructor / Guardrails** | **Reject** heavy deps / **Adopt** zero-dep `validate_schema` | Keeps `ras-anything` zero-dependency (`_ras_hardened/` vendors cleanly on `enable`); `validate_schema` enforces strict JSON object shape, non-empty strings, and enums. |
| **OTel GenAI Semconv (`v1.42.0`)** | **Adopt** attribute schema | `AIPolicy` emits `gen_ai.*` structured JSON logs compatible with any OTLP collector, Langfuse, or Phoenix log ingestion pipeline. |
| **Temporal / Restate** | **Reject** cluster runtime / **Adopt** `StepJournal` | An external workflow cluster is overkill for library/CLI apps; `StepJournal` gives atomic local step durability at `$0` extra runtime cost. |
| **Promptfoo / OpenAI Evals** | **Integrate** via `--export-promptfoo` | `ras-anything drift` works standalone offline and exports valid `promptfoo` YAML (`tests`, `providers`, `is-json` assertions) for teams already running `promptfoo` in CI. |
| **Toxiproxy** | **Integrate** transport mapping (`TOXIPROXY_MAP`) | Maps `slow`, `truncated`, `timeout`, `refused`, `ai_sse_truncated` to Toxiproxy toxics while keeping semantic LLM faults (`429`, `context_limit`, `tool_error`) in the L7 boundary adapter. |
| **Langfuse / Arize Phoenix** | **Integrate** via OTel JSON telemetry | Trace-correlated `gen_ai.*` JSON log records allow zero-SDK export to Langfuse/Phoenix via OTel collectors. |
