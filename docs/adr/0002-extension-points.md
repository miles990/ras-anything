# ADR 0002: Extension points live in contracts.py; outside languages register by entry point

- Status: accepted (2026-11-23, commit b87aa0e)
- Context: the list of export formats was repeated in cli.py, mcp.py and export.py; adding a language needed edits in core.
- Decision: `ras_anything/contracts.py` holds the shared vocabularies (`EXPORT_FORMATS`, `LANGUAGE_ENTRY_POINT_GROUP = "ras_anything.languages"`). Every caller derives from it. A language in another package registers its `LangAdapter` under that entry-point group (same mechanism as pluggy/pytest plugins). A broken or clashing plugin is reported on stderr and skipped, never fatal; a plugin can't replace a built-in.
- Rejected: pluggy as a dependency (runtime deps stay at zero); a config file listing plugins (one more thing to edit).
- Consequences: `tests/test_contracts.py` checks that CLI, MCP and export agree and that plugin discovery works.
