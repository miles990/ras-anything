# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/). Not published to any registry.

## Unreleased

### Added
- `--json` on every result-producing command writes a `cli-result` object to stdout (progress and logs go to stderr; `export --format json` stays a bundle). Errors remain RFC 9457 on stderr.
- `schema_version` lives in `contracts.SCHEMA_VERSION` and is **1.1.0** (additive `coverage.adapters[].enabled`; see `docs/schema/README.md`).
- Export formats are auto-discovered modules in `ras_anything/exporters/` (new format = new file, 0 core changes).
- `wow.manifest.json` (wow INTEROP 1.0, wow-common 1.0): skills with CLI, MCP tool, output schema and JSON-Pointer evidence.
- Library contract: `docs/LIBRARY.md`, `py.typed`, API snapshot `docs/api/runtime-api.json` checked by `tests/test_library_api.py`.
- `RAS_ANYTHING_LANGUAGES_OFF` switches languages off by name.
- `export --format cloudevents` (CloudEvents 1.0 JSON lines).
- `--json` errors as RFC 9457 problem objects on stderr.
- Language plugins by entry point (`ras_anything.languages`); built-in languages and patterns discovered, not listed.
- `docs/schema/ras-flags.schema.json` for `.ras/flags.json`; AGENTS.md, CONTRIBUTING.md, ADRs, a proposed CI workflow in `docs/ci/github-workflow-ci.yml` (ruff, mypy, tests with coverage; copy to `.github/workflows/` to turn it on).
- `wow/eng.json` and `wow/eng-baseline.json`: W8 engineering measurements and the frozen baseline.

### Fixed
- `export` / `sync_out` leave versioned `.ras/out` snapshots (including SLO series timestamps) unchanged when the payload has not changed.
- Holdout pipeline tests (`test_ai_pipeline`, `test_go_pipeline`, `test_java_pipeline`) skip with a clear reason when `wow/work/holdout/` is missing (fresh clone or CI).
- Proposed CI workflow pins Actions to full commit SHAs and sets job-level `permissions: contents: read`.
- `doctor` crashed with `NameError` when `RAS_ANYTHING_HEALTH_FILE` was set.
- Import cycle diagnose → enable → verify (`resolve_id` moved to `hardens.py`).

### Removed
- About 70 dead lines in `control.py` (shadowed by the runtime's versions).

## 0.1.0
- First internal version.
