# Contributing

Read [AGENTS.md](AGENTS.md) first (setup, tests, structure, boundaries). Decisions are in [docs/adr/](docs/adr/).

## Before you push
1. `.venv/bin/python -m unittest discover -s tests -t .` passes.
2. `.venv/bin/ruff check .` and `.venv/bin/mypy` pass.
3. If you have wow: `node <wow>/tools/wow-eng.mjs all .` shows no new violations (existing ones are in `wow/eng-baseline.json`).
4. Add a line to [CHANGELOG.md](CHANGELOG.md) under *Unreleased*.

## Add a language (a component: new files only)
1. `ras_anything/lang/<name>.py`: a concrete `LangAdapter` subclass with `name`, `priority` (python 10, node 20, go 30, java 100), and every abstract method of `lang/base.py`.
2. `ras_anything/lang/<name>_agent/`: the in-app agent. It must implement every fault kind in `faults.MATRIX`; `tests/test_contracts.py` fails until it does. The folder ships by glob, no packaging edit.
3. A pipeline test `tests/test_<name>_pipeline.py` and an example under `examples/`.
4. Nothing in `lang/__init__.py`, `cli.py`, `mcp.py` or `export.py` changes. Check with `wow-eng change --range <base>..HEAD`: core edits must be 0.
5. A language in its own package: register the adapter class under entry-point group `ras_anything.languages` (docs/adr/0002).
6. Users switch it off with `RAS_ANYTHING_LANGUAGES_OFF=<name>`.

## Add a resilience pattern
1. `ras_anything/patterns/<name>.py` with a module-level `PATTERN` (name, order, params, default_on, layer). `patterns.NAMES` picks it up.
2. A test in `tests/`. It is switchable with the flag `ras.pattern.<name>`.
3. If it exports helpers, still add them to the runtime's `_PATTERN_NAMES` (known gap). The Node list is generated: run `python -c 'from ras_anything.patterns import write_node_names; write_node_names()'` so `lang/node_agent/patterns/names.json` matches `patterns.NAMES`.

## Add an export format (a component: new files only)
1. `ras_anything/exporters/<name>.py` with module-level `FORMAT` (the `--format` name), optional `ORDER` (lower first; json is 10), and `render(project, od, bundle, snapshots, events) -> str`. `exporters.FORMATS` picks it up; the CLI and MCP enum follow.
2. A test in `tests/` (the contract test already checks CLI, MCP and renderers agree).
3. Nothing in `contracts.py`, `export.py`, `cli.py` or `mcp.py` changes. Check with `wow-eng change`: core edits must be 0.

## Versions
SemVer. Public library API: `docs/api/runtime-api.json`. Snapshot schemas: `docs/schema/README.md`. Interop: wow INTEROP 1.0 via `wow.manifest.json`.
