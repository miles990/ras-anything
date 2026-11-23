"""`export --format otlp-json`: OTLP/JSON collector payload of metrics and log records."""

from __future__ import annotations

import json
import time
from pathlib import Path

FORMAT = "otlp-json"
ORDER = 40


def render(project, od, bundle, snapshots, events) -> str:
    from ..paths import scrub

    return json.dumps(scrub(_otlp_json(project, {"report": snapshots.get("report")}, events), project),
                      indent=2, sort_keys=True) + "\n"


def _otlp_json(project: Path, bundle: dict, events: list[dict]) -> dict:
    from ..export import SCHEMA_VERSION

    now_ns = str(int(time.time() * 1e9))
    rep = bundle.get("report") or {}
    metrics = []

    def add_gauge(name: str, unit: str, pts: list[tuple[dict, float]]):
        if not pts:
            return
        metrics.append({
            "name": name,
            "unit": unit,
            "gauge": {
                "dataPoints": [
                    {
                        "timeUnixNano": now_ns,
                        "asDouble": float(val),
                        "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in attrs.items()],
                    }
                    for attrs, val in pts
                ]
            },
        })

    b = rep.get("bench") or {}
    if b:
        add_gauge("ras.availability.ratio", "1", [
            ({"variant": s}, (b.get(s) or {}).get("availability") or 0.0) for s in ("original", "hardened")
        ])
        add_gauge("ras.p99.seconds", "s", [
            ({"variant": s}, (b.get(s) or {}).get("p99_s") or 0.0) for s in ("original", "hardened")
        ])
    ai = rep.get("ai") or {}
    if ai.get("hardened"):
        add_gauge("ras.ai.semantic_success.ratio", "1", [
            ({"variant": "original"}, (ai.get("original") or {}).get("semantic_success_rate") or 0.0),
            ({"variant": "hardened"}, ai["hardened"].get("semantic_success_rate") or 0.0),
        ])

    log_records = [
        {
            "timeUnixNano": now_ns,
            "severityText": ev.get("severity", "INFO"),
            "body": {"stringValue": ev.get("event_type", "")},
            "traceId": ev.get("trace_id") or "",
            "attributes": [
                {"key": "ras.seq", "value": {"intValue": str(ev.get("seq", 0))}},
                {"key": "ras.data", "value": {"stringValue": json.dumps(ev.get("data") or {}, sort_keys=True)}},
            ],
        }
        for ev in events
    ]
    resource = {"attributes": [{"key": "service.name", "value": {"stringValue": project.name}}]}
    return {
        "resourceMetrics": [{"resource": resource, "scopeMetrics": [{"scope": {"name": "ras-anything", "version": SCHEMA_VERSION}, "metrics": metrics}]}],
        "resourceLogs": [{"resource": resource, "scopeLogs": [{"scope": {"name": "ras-anything", "version": SCHEMA_VERSION}, "logRecords": log_records}]}],
    }

