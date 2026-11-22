# wow artifact index — RAS-anything v0.1

All wow artifacts for this run live under `wow/`. Subfolders:

| path | what | committed? |
|---|---|---|
| `wow/spec.md` | external spec S-1..S-12, first-principles block, domain points, wow moment | yes |
| `wow/progress.md` | resource declaration, routing table, win table (W1–W7), usage log | yes |
| `wow/brief.md`, `calibration.md`, `judge-criteria.md`, `tools.json`, `preflight.json` | wow-init scaffolding | yes |
| `wow/briefs/` | every dispatch prompt (builder, answer-key writer, judges, blind testers) + run logs | yes |
| `wow/holdout/` | independent answer key + held-out app (Codex). Kept OUTSIDE the repo at `/workspace/ras-holdout` until the build finished, then moved in | yes (after build) |
| `wow/evidence/` | raw command outputs, screenshots (gitignored by wow-init default) | no |
| `wow/proof/` | curated evidence summaries cited in the report (builder proof, holdout run, bench numbers) | yes |
| `wow/reviews/` | Fable 5-1 and Codex verdicts (copied here; wow-init gitignores this, we force-add the final verdict files) | partly |

## Places wow put things OUTSIDE `wow/` by default (to fix in the skill)
(filled in at delivery)
