# wow artifact index — RAS-anything (v0.1 + v0.2 core scope)

## Deliverables (real paths, not copied into wow/)

| Deliverable | Real path | Status |
|---|---|---|
| Package (`ras-anything` CLI, runtime, adapters, verify, bench, report, export, MCP) | `ras_anything/`, `pyproject.toml`, `MANIFEST.in` | committed |
| Installers & release runbook (Alex-gated, no publish) | `install.sh`, `Makefile`, `docs/RELEASE.md` | committed |
| Dual-purpose README & license/credits | `README.md`, `LICENSE`, `NOTICE` | committed |
| Architecture, patterns, flags, AI, runbook, schema docs | `docs/ARCHITECTURE.md`, `docs/PATTERNS.md`, `docs/FLAGS.md`, `docs/AI.md`, `docs/RUNBOOK.md`, `docs/schema/` | committed |
| Visual report cards (SVG) & dashboard sample screenshot | `docs/cards/weather.svg`, `docs/cards/payments.svg`, `docs/cards/orders-node.svg`, `docs/cards/inventory-go.svg`, `docs/cards/support-ai.svg`, `docs/cards/dashboard-sample.png` | committed |
| Runnable examples + measured reports | `examples/weather/`, `examples/payments/`, `examples/orders-node/`, `examples/inventory-go/`, `examples/support-ai/` (each: `RAS-REPORT.md`, `REPRODUCE.md`, `.ras/`) | committed (disabled clean) |
| Visualization sample dashboard & Pattern/Flags labs | `examples/dashboard-sample/` (`index.html`, `README.md`, `out/`), `examples/patterns-lab/`, `examples/flags-hot/` | committed |
| Agent integrations | `.claude-plugin/`, `.mcp.json`, `commands/ras.md`, `skills/ras-anything/SKILL.md` | committed |

## Process records

All wow artifacts for this run live under `wow/`. Subfolders:

| path | what | git |
|---|---|---|
| `wow/INDEX.md` | this index | committed |
| `wow/spec.md` | external spec S-1..S-12 + V-1..V-19 | committed |
| `wow/progress.md` | resource declaration, stage table, v0.1 & v0.2 win tables (W1–W7) | committed |
| `wow/calibration.md`, `judge-criteria.md`, `tools.json`, `preflight.json` | wow-init config | committed |
| `wow/reviews.md` | v0.1 & v0.2 review verdict summaries (Codex + Fable) | committed |
| `wow/proof/` | curated process proof (`holdout-blind.md`, `first-read.md`, `install-clean-home.md`, `w3-blind-timer.md`) | committed |
| `wow/brief.md`, `wow/work/` | scratch briefs, logs, holdout fixtures, raw review transcripts, builder self-reports | ignored |
