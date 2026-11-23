"""Names other modules agree on, kept in one light module (no imports) so the CLI and the MCP server can read
them without loading the pipeline. See docs/adr/0002-extension-points.md.

Changing one of these is a contract change: bump the schema version or note it in CHANGELOG.md.
"""

from __future__ import annotations

# `ras-anything export --format`: each name has one renderer in export.RENDERERS (tests/test_contracts.py checks).
EXPORT_FORMATS = ("json", "ndjson", "prom", "otlp-json", "cloudevents")

# Entry-point group a separately installed package uses to add a language adapter without editing ras-anything:
#   [project.entry-points."ras_anything.languages"]
#   ruby = "ras_ruby:RubyAdapter"
LANGUAGE_ENTRY_POINT_GROUP = "ras_anything.languages"
