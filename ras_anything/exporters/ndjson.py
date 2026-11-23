"""`export --format ndjson`: the append-only events stream, unchanged."""

from __future__ import annotations

FORMAT = "ndjson"
ORDER = 20


def render(project, od, bundle, snapshots, events) -> str:
    ev_p = od / "events.ndjson"
    return ev_p.read_text(encoding="utf-8") if ev_p.is_file() else ""
