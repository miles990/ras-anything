"""Names other modules agree on, kept in one light module (no imports) so the CLI and the MCP server can read
them without loading the pipeline. See docs/adr/0002-extension-points.md.

Changing one of these is a contract change: bump the schema version or note it in CHANGELOG.md.
"""

from __future__ import annotations

# Emitted documents carry this (MAJOR.MINOR.PATCH). Bump according to docs/schema/README.md.
SCHEMA_VERSION = "1.1.0"

# `ras-anything export --format`: names come from ras_anything.exporters (one module each).
# Loaded on first use so this module stays import-light (docs/adr/0002).


def __getattr__(name: str):
    if name == "EXPORT_FORMATS":
        from .exporters import FORMATS
        return FORMATS
    raise AttributeError("module %r has no attribute %r" % (__name__, name))

# Entry-point group a separately installed package uses to add a language adapter without editing ras-anything:
#   [project.entry-points."ras_anything.languages"]
#   ruby = "ras_ruby:RubyAdapter"
LANGUAGE_ENTRY_POINT_GROUP = "ras_anything.languages"
