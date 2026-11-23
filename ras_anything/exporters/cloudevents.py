"""`export --format cloudevents`: CloudEvents 1.0 structured JSON, one event per line (wow INTEROP §3)."""

from __future__ import annotations

import json

FORMAT = "cloudevents"
ORDER = 50


def _ce_time(ts: str) -> str:
    """RFC 3339 for CloudEvents: "+0800" -> "+08:00"."""
    if len(ts) > 5 and ts[-5] in "+-" and ts[-4:].isdigit():
        return ts[:-2] + ":" + ts[-2:]
    return ts


def render(project, od, bundle, snapshots, events) -> str:
    from ..paths import scrub

    out = []
    for ev in events:
        ce = {"specversion": "1.0", "id": "%s-%s" % (project.name, ev.get("seq")), "source": "urn:ras-anything:project:" + project.name,
              "type": "dev.ras-anything." + str(ev.get("event_type") or "event"), "time": _ce_time(str(ev.get("ts") or "")),
              "datacontenttype": "application/json", "dataschema": "https://github.com/miles990/ras-anything/docs/schema/ras-event.schema.json",
              "data": ev}
        if ev.get("function_id"):
            ce["subject"] = ev["function_id"]
        out.append(json.dumps(scrub(ce, project), sort_keys=True))
    return "\n".join(out) + ("\n" if out else "")
