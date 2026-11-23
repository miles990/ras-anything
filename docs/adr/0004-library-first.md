# ADR 0004: Library first; framework only as a skeleton; no editor of our own

- Status: accepted (2026-11-23)
- Library: `ras_anything.runtime` is the importable library (`__all__`, 60 names, `py.typed`, no import side effects). Its API is a contract: `docs/api/runtime-api.json` + `tests/test_library_api.py` (removing or changing a name is a major version). CLI, MCP, skill and plugin are shells over the pipeline modules. Same split as Resilience4j/Polly (core library, thin framework integrations). A separate `ras-anything-runtime` distribution is phase 2; nothing is published.
- Framework: no invasive framework (Spring/Nest style). Users keep their code; ras-anything writes a hardened copy behind a switch.
- Editor: none. People edit `.ras/flags.json` with `ras-anything flag set`, agents with MCP `ras_flag`; both see each other's edits in `git diff` (schema: docs/schema/ras-flags.schema.json). LSP diagnostics (as Ruff and Semgrep ship) are optional, later.
