# ADR 0005: wow.manifest.json, RFC 9457 errors, CloudEvents export

- Status: accepted (2026-11-23, commits 7ed190c, fc0f734)
- Decision: ras-anything follows wow INTEROP 1.0: `wow.manifest.json` (A2A-style skills with cli, mcpTool, output schema and JSON-Pointer evidence), `--json` errors as RFC 9457 on stderr (`urn:ras-anything:problem:usage|user-error`, with `exit_code` and `next`), events as CloudEvents 1.0 (`export --format cloudevents`).
- Consequence: wow's `wow-eng rel` reads availability, SLO and verification from ras-anything through the manifest alone, without knowing ras-anything (first composition demo).
