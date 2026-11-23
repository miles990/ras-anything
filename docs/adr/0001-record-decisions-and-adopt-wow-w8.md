# ADR 0001: Record architecture decisions; measure structure with wow W8

- Status: accepted (2026-11-23)
- Context: decisions about extension points, packaging and interfaces lived only in commit messages.
- Decision: keep short ADRs (Nygard format) in `docs/adr/`. Structural quality is measured with wow's `wow-eng all` (W8, E1–E11) using `wow/eng.json`; existing debt is frozen in `wow/eng-baseline.json` (ratchet: only new violations fail).
- Consequences: reviewers see why a boundary exists; a new violation shows up as a number, not an opinion. The baseline must shrink over time (docs/REVIEW-PRACTICES.md has the plan).

## wow-common 1.0 adoption (existing product, not generated from the skeleton)

| Rule | ras-anything | Difference |
|---|---|---|
| D1 layers | 8 layers in `wow/eng.json`, checked by `wow-eng arch` | flat package instead of `core/components/adapters/shells` folders |
| D2 library vs shells | `ras_anything.runtime` is the library; cli/mcp are shells | pipeline modules sit between; separate distribution is phase 2 |
| D3 components | languages and patterns discovered and switchable | export formats are not components yet |
| D4/D5 errors, logs | RFC 9457 under `--json`; logs to stderr; events CloudEvents | human errors via `ui.error` (what, impact, next) |
| D6 config | `RAS_ANYTHING_*` env and `.ras/` files | — |
| Design tokens | no visual UI that people edit (terminal + SVG cards) | G3 not applicable until a dashboard edits files |
