# ADR 0006: Export formats are discovered modules

- Status: accepted (2026-11-23)
- Context: adding `--format cloudevents` edited two core files (`contracts.py`, `export.py`). Language adapters and patterns were already discovered; formats were still a handwritten list (docs/REVIEW-PRACTICES.md phase 2, E4).
- Decision: each format is `ras_anything/exporters/<name>.py` with `FORMAT`, optional `ORDER`, and `render(...)`. `exporters.FORMATS` / `RENDERERS` are discovered. `contracts.EXPORT_FORMATS` is a lazy alias so the CLI and MCP still share one vocabulary without importing the pipeline at module load (ADR 0002). A new format is new files only; core edits must be 0.
- Rejected: keeping a handwritten tuple in `contracts.py` (every format would still touch a core file); a plugin entry-point group for formats (no in-tree need; can be added later like languages).
- Consequences: CONTRIBUTING "Add an export format" and `tests/test_contracts.py` guard the contract. `wow/eng.json` lists export formats as an auto-discovered component.
