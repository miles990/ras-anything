# wow artifact index — RAS-anything v0.1

## Deliverables (real paths, not copied into wow/)

| Deliverable | Real path | Status |
|---|---|---|
| Package (`ras-anything` CLI, runtime, verify, bench, report, MCP) | `ras_anything/`, `pyproject.toml` | committed (`33/33` unit tests in `tests/`) |
| README & license/credits | `README.md`, `LICENSE`, `NOTICE` | committed |
| Visual report cards (SVG) | `docs/cards/weather.svg`, `docs/cards/payments.svg` | committed |
| Runnable examples + measured reports | `examples/weather/` (`RAS-REPORT.md`, `REPRODUCE.md`, `.ras/`), `examples/payments/` (`RAS-REPORT.md`, `REPRODUCE.md`, `.ras/`) | committed (disabled clean) |
| Agent integrations | `.claude-plugin/`, `.mcp.json`, `commands/ras.md`, `skills/ras-anything/SKILL.md` | committed |

## Process records

All wow artifacts for this run live under `wow/`. Subfolders:

| path | what | git |
|---|---|---|
| `wow/INDEX.md` | this index | committed |
| `wow/spec.md` | external spec S-1..S-12, first-principles block, domain points, wow moment | committed |
| `wow/progress.md` | resource declaration, routing table, win table (W1–W7), final usage | committed |
| `wow/calibration.md`, `judge-criteria.md`, `tools.json`, `preflight.json` | wow-init config | committed |
| `wow/reviews.md` | Round 1 & Round 2 review verdict summary (Fable 5-1 judge, Codex second opinion) | committed |
| `wow/proof/` | curated process proof only (`holdout-blind.md`, `first-read.md`) — no copies of deliverables | committed |
| `wow/brief.md` | wow-init shared dispatch brief (intermediate) | ignored |
| `wow/work/briefs/` | dispatch prompts (including moved legacy `/workspace/ras-briefs`) | ignored |
| `wow/work/logs/` | wow-agent progress logs (`fable1/2`, `codex1/2`, `blind1/2`) | ignored |
| `wow/work/holdout/` | independent answer key + held-out app moved from `/workspace/ras-holdout` after testing | ignored |
| `wow/work/evidence/` | raw command outputs (`holdout-verify.txt`, `holdout-verify-v2.txt`, `bench-*.txt`, PNG renders) | ignored |
| `wow/work/reviews/` | raw judge/reviewer transcripts (`fable1.md`, `fable2.md`, `codex1.md`, `codex2.md`) | ignored |
| `wow/work/blind/` | blind first-read bundle & logs (`phaseA-run1/2.md`, `phaseB-run1/2.md`, `blind1/2.out`) | ignored |
| `wow/work/runs/` | builder self-reports & scratch runs | ignored |

## Places wow tooling defaulted to writing OUTSIDE `wow/` (to fix in the `wow` skill)

1. `wow-agent.mjs` / `lib/agent-ops.mjs` (`WOW_AGENT_RUNS`): defaults to `~/.claude/wow/agent-runs/<timestamp>-<label>/` (`prompt.md`, `events.jsonl`, `progress.log`, `run.json`, `last.md`) instead of `<repo>/wow/work/runs/`.
2. `wow-agent.mjs --blind` (`WOW_BLIND_ROOT`): defaults to `<os.tmpdir()>/wow-blind/<label>-XXXXXX/` (`/tmp/wow-blind/...`) instead of a configurable repo-local or auto-archived path under `wow/work/blind/`.
3. `wow-init.mjs` & `wow-tidy.mjs` & `wow-status.mjs` & `wow-checkpoint.mjs`: hardcode `wow/runs/`, `wow/reviews/`, `wow/evidence/`, `wow/brief.md` at the `wow/` top level (and `wow-tidy` / `wow-status` don't recognize a single `wow/work/` gitignored bucket vs committed `wow/proof/` + `wow/reviews.md`).
4. `wow-capture.mjs`: defaults `--out` to `./wow/evidence/capture-<stamp>` (fine if `wow/evidence/` is gitignored, but splits from `wow/work/evidence/`).
5. Session-created external scratch dirs during this run (now moved into `wow/work/`): `/workspace/ras-briefs` (builder/answerkey briefs & logs) and `/workspace/ras-holdout` (holdout workspace kept outside during build, then moved to `wow/work/holdout/`).
