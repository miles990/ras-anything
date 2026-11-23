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


# Incident runtimes (Node / Go / Java) cannot import this module: they are vendored into the user's
# app. write_schema_version() copies SCHEMA_VERSION into those trees (a generated Node module; a marked
# constant in the single-file Go and Java runtimes). tests/test_contracts.py fails if they drift.


def schema_version_files():
    """(path, expected text) for each vendored incident schema_version target. Does not write."""
    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent
    node = root / "lang" / "node_agent" / "patterns" / "schema_version.cjs"
    node_text = (
        "// Generated from ras_anything.contracts.SCHEMA_VERSION by write_schema_version(). Do not edit by hand.\n"
        "module.exports = " + json.dumps(SCHEMA_VERSION) + ";\n"
    )
    items = [(node, node_text)]

    go = root / "lang" / "go_agent" / "rasruntime" / "runtime.go"
    go_pat = re.compile(
        r"(// Generated from ras_anything\.contracts\.SCHEMA_VERSION by write_schema_version\(\)\. Do not edit by hand\.\nconst SchemaVersion = \")[^\"]+(\")"
    )
    go_text = go.read_text(encoding="utf-8")
    new_go, n = go_pat.subn(r"\g<1>" + SCHEMA_VERSION + r"\2", go_text, count=1)
    if n != 1:
        raise ValueError("schema_version marker missing or not unique in %s" % go)
    items.append((go, new_go))

    java = root / "lang" / "java_agent" / "runtime" / "RasRt.java"
    java_pat = re.compile(
        r"(// Generated from ras_anything\.contracts\.SCHEMA_VERSION by write_schema_version\(\)\. Do not edit by hand\.\n"
        r'\s*public static final String SCHEMA_VERSION = ")[^"]+(";)'
    )
    java_text = java.read_text(encoding="utf-8")
    new_java, n = java_pat.subn(r"\g<1>" + SCHEMA_VERSION + r"\2", java_text, count=1)
    if n != 1:
        raise ValueError("schema_version marker missing or not unique in %s" % java)
    items.append((java, new_java))
    return items


def write_schema_version():
    """Rewrite Node/Go/Java incident schema_version from SCHEMA_VERSION. Run after bumping it."""
    written = []
    for path, text in schema_version_files():
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
