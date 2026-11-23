# Clean-HOME install evidence (build2i / V-6 + V-11 local check)

- When: 2026-11-23 18:33 (Asia/Taipei, UTC+8)
- Checkout: `<checkout>` (host absolute path scrubbed)
- Temporary `$HOME`: `/tmp/ras-anything-install-evidence-m9bhsskd/home` (destroyed after this run; nothing written to the real home)
- Method: `pipx` hidden on `PATH` → **venv** path (`~/.local/share/ras-anything` + shim in `~/.local/bin`)
- This run did **not** make the repo public and did **not** upload to PyPI or npm.

## Layout after `./install.sh`

- CLI shim: `/tmp/ras-anything-install-evidence-m9bhsskd/home/.local/bin/ras-anything` → `ras-anything 0.1.0`
- Claude skill (readable): `/tmp/ras-anything-install-evidence-m9bhsskd/home/.claude/skills/ras-anything/SKILL.md` — byte-identical to `skills/ras-anything/SKILL.md`
- Codex skill (readable): `/tmp/ras-anything-install-evidence-m9bhsskd/home/.codex/skills/ras-anything/SKILL.md` — byte-identical to `skills/ras-anything/SKILL.md`

```markdown
---
name: ras-anything
description: Make a Python, Node.js (JavaScript/TypeScript), or Go app survive dependency failures (timeouts, refused connections, 5xx/429, slow or truncated responses, outages, and AI-provider faults) by hardening only its weakest functions and proving it on recorded real calls. Use when the user wants their app more reliable/available, asks about timeouts, retries, idempotency, circuit breakers, chaos testing, or "why does it hang/crash when the API is down".
---

# ras-anything

The `ras-anything` CLI measures, records, replays, checks and swaps. You write the hardened function.
```

## Commands (in order)

### `./install.sh (first run, clean HOME, pipx hidden -> venv)` — exit 0

stdout:

```text
pipx not found; installing CLI into /tmp/ras-anything-install-evidence-m9bhsskd/home/.local/share/ras-anything/venv (Python /usr/bin/python3)
Requirement already satisfied: pip in /tmp/ras-anything-install-evidence-m9bhsskd/home/.local/share/ras-anything/venv/lib/python3.13/site-packages (25.1.1)
Collecting pip
  Downloading pip-26.3-py3-none-any.whl.metadata (4.6 kB)
Collecting setuptools
  Downloading setuptools-84.2.1-py3-none-any.whl.metadata (6.6 kB)
Collecting wheel
  Downloading wheel-0.48.0-py3-none-any.whl.metadata (2.3 kB)
Collecting packaging>=24.0 (from wheel)
  Downloading packaging-26.3-py3-none-any.whl.metadata (3.5 kB)
    … (72 lines total) …
        "ras-anything": { "command": "ras-anything", "args": ["mcp"] }
      }
    }

Claude Code plugin (from this clone; the GitHub repo is private — do not publish it):
  /plugin marketplace add <checkout>
  /plugin install ras-anything@ras-anything
  /ras-anything:ras python app.py

Uninstall:
  <checkout>/install.sh --uninstall
  # or: make uninstall
```

stderr:

```text
/tmp/pip-build-env-cu6y5m7t/overlay/lib/python3.13/site-packages/setuptools/command/build_py.py:215: BuildBackendWarning: Package 'ras_anything.lang.go_agent.asttool' is absent from the `packages` configuration.
  !!
  
          ********************************************************************************
          ############################
          # Package would be ignored #
          ############################
          Python recognizes 'ras_anything.lang.go_agent.asttool' as an importable package[^1],
          but it is absent from setuptools' `packages` configuration.
  
    … (205 lines total) …
  
          - https://setuptools.pypa.io/en/latest/userguide/datafiles.html
  
  
          [^1]: For Python, any directory (with suitable naming) can be imported,
                even if it does not contain any `.py` files.
                On the other hand, currently there is no concept of package data
                directory, all directories are treated like packages.
          ********************************************************************************
  
  !!
    check.warn(importable)
```

### `make install (idempotent re-run)` — exit 0

stdout:

```text
./install.sh
pipx not found; installing CLI into /tmp/ras-anything-install-evidence-m9bhsskd/home/.local/share/ras-anything/venv (Python /usr/bin/python3)
Requirement already satisfied: pip in /tmp/ras-anything-install-evidence-m9bhsskd/home/.local/share/ras-anything/venv/lib/python3.13/site-packages (26.3)
Requirement already satisfied: setuptools in /tmp/ras-anything-install-evidence-m9bhsskd/home/.local/share/ras-anything/venv/lib/python3.13/site-packages (84.2.1)
Requirement already satisfied: wheel in /tmp/ras-anything-install-evidence-m9bhsskd/home/.local/share/ras-anything/venv/lib/python3.13/site-packages (0.48.0)
Requirement already satisfied: packaging>=24.0 in /tmp/ras-anything-install-evidence-m9bhsskd/home/.local/share/ras-anything/venv/lib/python3.13/site-packages (from wheel) (26.3)
Processing ./.
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
  Getting requirements to build wheel: started
    … (56 lines total) …
        "ras-anything": { "command": "ras-anything", "args": ["mcp"] }
      }
    }

Claude Code plugin (from this clone; the GitHub repo is private — do not publish it):
  /plugin marketplace add <checkout>
  /plugin install ras-anything@ras-anything
  /ras-anything:ras python app.py

Uninstall:
  <checkout>/install.sh --uninstall
  # or: make uninstall
```

stderr:

```text
/tmp/pip-build-env-f3vu3vlf/overlay/lib/python3.13/site-packages/setuptools/command/build_py.py:215: BuildBackendWarning: Package 'ras_anything.lang.go_agent.asttool' is absent from the `packages` configuration.
  !!
  
          ********************************************************************************
          ############################
          # Package would be ignored #
          ############################
          Python recognizes 'ras_anything.lang.go_agent.asttool' as an importable package[^1],
          but it is absent from setuptools' `packages` configuration.
  
    … (205 lines total) …
  
          - https://setuptools.pypa.io/en/latest/userguide/datafiles.html
  
  
          [^1]: For Python, any directory (with suitable naming) can be imported,
                even if it does not contain any `.py` files.
                On the other hand, currently there is no concept of package data
                directory, all directories are treated like packages.
          ********************************************************************************
  
  !!
    check.warn(importable)
```

### `ras-anything --version` — exit 0

stdout:

```text
ras-anything 0.1.0
```

stderr: *(empty — 0 warnings)*

### `ras-anything doctor (cwd = temporary HOME)` — exit 0

stdout:

```text
ras-anything doctor · /tmp/ras-anything-install-evidence-m9bhsskd/home
  ✓ python 3.13.5            
  ! HTTP clients             no requests/httpx/aiohttp here (optional: urllib and subprocess are covered)
  ✓ node 24.22.1             TypeScript: yes
  ✓ go version go1.27.3 linux/amd64 ready (net/http, os/exec)
  ! claude on PATH           not found (only needed for --agent claude)
  ! codex on PATH            not found (only needed for --agent codex)
  ! git                      not a git repository (enable edits your source: commit first is safer)
  ✓ .ras writable            /tmp/ras-anything-install-evidence-m9bhsskd/home/.ras

next: ras-anything scan -- python app.py / node app.mjs
```

stderr: *(empty — 0 warnings)*

### `ras-anything mcp (initialize + tools/list)` — exit 0

stdout:

```text
{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "ras-anything", "version": "0.1.0"}, "instructions": "ras-anything hardens the few functions that break under dependency faults. Loop: ras_scan -> ras_harden -> edit the hardened file per TASK.md -> ras_verify (until PASS) -> ras_enable -> ras_bench -> ras_report."}}
{"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "ras_scan", "description": "Run the app clean and under seeded dependency chaos; rank functions by risk \u00d7 exposure \u00d7 hardenability.", "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}, "cmd": {"type": "array", "items": {"type": "string"}, "description": "command that runs the app, e.g. [\"python\", \"app.py\"]"}, "seeds": {"type": "integer"}}, "required": ["cmd"]}}, {"name": "ras_harden", "description": "Record real calls of one function with their dependency I/O and write TASK.md + a stub (optionally run an agent). Then edit the hardened file following TASK.md.", "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}, "id": {"type": "string", "description": "function id from the scan, e.g. weather.fetch_forecast"}, "agent": {"type": "string", "description": "claude | codex | shell command (optional)"}}, "required": []}}, {"name": "ras_verify", "description": "Replay recorded calls (happy path, then the fault matrix, virtual clock) for original vs hardened; report gates.", "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}, "id": {"type": "string", "description": "function id from the scan, e.g. weather.fetch_forecast"}}, "required": []}}, {"name": "ras_enable", "description": "Swap the verified hardened function in (one marked block in its module).", "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}, "id": {"type": "string", "description": "function id from the scan, e.g. weather.fetch_forecast"}}, "required": []}}, {"name": "ras_disable", "description": "Remove the swap, restoring the module byte for byte.", "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}, "id": {"type": "string", "description": "function id from the scan, e.g. weather.fetch_forecast"}}, "required": []}}, {"name": "ras_bench", "description": "Whole-app runs under live seeded chaos, original vs hardened, interleaved.", "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}, "cmd": {"type": "array", "items": {"type": "string"}, "description": "command that runs the app, e.g. [\"python\", \"app.py\"]"}, "seeds": {"type": "integer"}, "degraded_marker": {"type": "string", "description": "text marking a deliberately degraded output line"}}, "required": []}}, {"name": "ras_report", "description": "Write RAS-REPORT.md and ras-card.svg from measured results.", "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}}, "required": []}}, {"name": "ras_status", "description": "Show scan, hardened functions, verify state, what is enabled, flags and the next step.", "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}}, "required": []}}, {"name": "ras_replay", "description": "Replay a recorded incident from .ras/incidents/ on the virtual clock and write RCA (or list incidents).", "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}, "id": {"type": "string", "description": "incident id (optional; omit for latest)"}, "list": {"type": "boolean", "description": "list recorded incidents instead of replaying"}}, "required": []}}, {"name": "ras_flag", "description": "List, get, set or delete flags in .ras/flags.json (hot-reloaded in running processes).", "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}, "argv": {"type": "array", "items": {"type": "string"}, "description": "flag args, e.g. set ras.pattern.retry off, or list"}}, "required": []}}, {"name": "ras_export", "description": "Export .ras/out snapshots and telemetry as json, ndjson, prom, or otlp-json.", "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}, "format": {"type": "string", "enum": ["json", "ndjson", "prom", "otlp-json"]}}, "required": []}}, {"name": "ras_events", "description": "Read .ras/out/events.ndjson incrementally using a sequence cursor.", "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}, "since": {"type": "integer", "description": "sequence cursor (default 0)"}, "limit": {"type": "integer", "description": "max events to return (default 200)"}}, "required": []}}]}}
```

stderr: *(empty — 0 warnings)*

## MCP startup

- `initialize` serverInfo: `{"name": "ras-anything", "version": "0.1.0"}`
- `tools/list` (12 tools): `ras_bench, ras_disable, ras_enable, ras_events, ras_export, ras_flag, ras_harden, ras_replay, ras_report, ras_scan, ras_status, ras_verify`

## Local `twine check` (no upload)

- Built artifacts (then deleted, not committed): `ras_anything-0.1.0.tar.gz`, `ras_anything-0.1.0-py3-none-any.whl`
- Wheel contains `node_agent/boundary.cjs`: **True**
- Wheel contains `go_agent/rasagent/agent.go`: **True**
- `twine check` output:

```text
Checking <checkout>/dist/ras_anything-0.1.0-py3-none-any.whl: PASSED
Checking <checkout>/dist/ras_anything-0.1.0.tar.gz: PASSED
```

No `twine upload`, no `gh repo edit --visibility`, no `git tag` was run.
