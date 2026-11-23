# AGENTS.md — ras-anything

## Setup
```bash
python -m venv .venv && .venv/bin/pip install -e . coverage
```
Node 22, Go and a JDK are needed for the Node, Go and Java pipeline tests.

## Test
```bash
.venv/bin/python -m unittest discover -s tests -t .      # full suite
.venv/bin/ruff check . && .venv/bin/mypy                  # lint and types (CI lint job)
node <wow>/tools/wow-eng.mjs all . --run                  # W8 engineering gates; wow/eng.json, baseline wow/eng-baseline.json
```
Some pipeline tests read hold-out fixtures from `wow/work/holdout/` (not tracked).

## Structure (dependency direction is downward; wow/eng.json `layers` is the checked version)
- `cli.py`, `mcp.py`, `__main__.py`: shells. Parse, call a pipeline module, print. No rules here.
- `scan.py`, `harden.py`, `verify.py`, `enable.py`, `bench.py`, `report.py`, `export.py`, `slo.py`, `replay.py`, ...: the pipeline.
- `lang/`: language adapters (components). `lang/<name>.py` + `lang/<name>_agent/` is found by `lang.discover()`.
- `agents/`, `signals.py`, `cassette.py`, `ai.py`: what runs inside the user's program and what it records.
- `faults.py`, `flags.py`, `control.py`: fault matrix, feature flags (`.ras/flags.json`), canary control.
- `runtime.py`, `patterns/`: the importable library hardened code uses (`docs/LIBRARY.md`). Each pattern is a module.
- `contracts.py`, `paths.py`, `ui.py`: shared vocabularies and helpers; import nothing above them.

## Conventions
- Every command has `--json`; with it, errors are RFC 9457 objects on stderr (`urn:ras-anything:problem:*`), exit 0 ok / 1 user error or findings / 2 usage.
- Snapshots in `.ras/out/` follow `docs/schema/*.schema.json` (`schema_version`, SemVer). Events export as CloudEvents.
- Flags people and agents both edit: `.ras/flags.json` (`docs/schema/ras-flags.schema.json`); people use `ras-anything flag set`, agents MCP `ras_flag`.
- Settings come from environment variables (`RAS_ANYTHING_*`) or files under `.ras/`; nothing is hard-coded.
- Runtime dependencies: none. Keep it that way.

## Boundaries
- Never rewrite the user's source in place: ras-anything writes a hardened copy behind a switch (`enable` / `disable`).
- Don't change `ras_anything.runtime.__all__` or a public signature without updating `docs/api/runtime-api.json` and the version (`tests/test_library_api.py`).
- Don't edit `docs/schema/` without bumping `schema_version` per `docs/schema/README.md`.
- Java files (`ras_anything/lang/java*`, `examples/shipping-java`, `tests/test_java_pipeline.py`) belong to the Java work stream.
- No publishing (PyPI, npm, public repo) and no external actions without the owner's explicit approval.
