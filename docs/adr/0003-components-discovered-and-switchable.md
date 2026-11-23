# ADR 0003: Languages and patterns are components: discovered, tested alone, switchable

- Status: accepted (2026-11-23, commits 7ed190c and this one)
- Context: modularity (dependency direction) is not the same as componentization (add, replace or switch off a part without touching core).
- Decision:
  - Languages: `lang.discover()` finds every concrete `LangAdapter` in `lang/*.py`, ordered by `priority` then name. Agent folders ship by glob (`*_agent/**`). `RAS_ANYTHING_LANGUAGES_OFF=java,go` switches languages off by name.
  - Patterns: `patterns.NAMES` is computed (PEP 562) from modules that define `PATTERN`, ordered by `order`. Each pattern is switchable at run time with the flag `ras.pattern.<name>` (docs/FLAGS.md).
  - Contract tests (`tests/test_contracts.py`): every agent knows every fault kind in `faults.MATRIX`; every agent folder is packaged; every language module is discovered.
- Measured (wow-eng change): a scratch commit adding a toy language touched 0 core files (2 new code files + 1 test); the conformance test then failed until its agent handled every fault kind, which is the intended guard.
- Not yet components: export formats (a dict in export.py plus a name in contracts.py: 2 core files per format), the runtime's `_PATTERN_NAMES` and Node `patterns/index.cjs` lists, and fault kinds (cross-cutting by nature: every agent must implement each one).
