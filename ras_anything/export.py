"""Visualization & telemetry data contract (V-19): .ras/out/ emitter, schema validator, and export."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import lang as lang_mod
from .paths import UserError, find_project, load_json, save_json, scrub

SCHEMA_VERSION = "1.0.0"

ALL_LANGUAGES = (
    ("python", "complete", "urllib / requests / httpx / aiohttp / socket / subprocess", True, True),
    ("node", "complete", "fetch / http / https / child_process (CJS + ESM + TS)", True, True),
    ("go", "complete", "net/http / os/exec: AST + rasagent + rasruntime", True, True),
    ("java", "planned", "HttpClient / HttpURLConnection / ProcessBuilder", False, False),
    ("csharp", "planned", "HttpClient / Process", False, False),
    ("php", "planned", "curl / file_get_contents / proc_open", False, False),
    ("ruby", "planned", "Net::HTTP / Open3", False, False),
    ("kotlin", "planned", "HttpClient / OkHttp / ProcessBuilder", False, False),
    ("bun", "planned", "fetch / Bun.spawn", False, False),
    ("deno", "planned", "fetch / Deno.Command", False, False),
    ("c_cpp", "planned", "libcurl / popen", False, False),
)


class SchemaError(ValueError):
    """Document failed JSON Schema draft 2020-12 validation."""


def _resolve_ref(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise SchemaError(f"unsupported $ref {ref!r}")
    cur = root
    for part in ref[2:].split("/"):
        if not isinstance(cur, dict) or part not in cur:
            raise SchemaError(f"unresolvable $ref {ref!r}")
        cur = cur[part]
    return cur


def validate_json_schema(doc, schema: dict, root: dict | None = None, path: str = "$") -> None:
    """Validate `doc` against a JSON Schema draft 2020-12 subset using Python stdlib only."""
    root = root or schema
    if "$ref" in schema:
        schema = _resolve_ref(schema["$ref"], root)

    expected_type = schema.get("type")
    if expected_type is not None:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_matches_type(doc, t) for t in types):
            raise SchemaError(f"{path}: expected type {expected_type!r}, got {type(doc).__name__}")

    if "enum" in schema and doc not in schema["enum"]:
        raise SchemaError(f"{path}: value {doc!r} not in enum {schema['enum']!r}")

    if isinstance(doc, str):
        if "minLength" in schema and len(doc) < int(schema["minLength"]):
            raise SchemaError(f"{path}: string length {len(doc)} < minLength {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], doc):
            raise SchemaError(f"{path}: string {doc!r} does not match pattern {schema['pattern']!r}")

    if isinstance(doc, (int, float)) and not isinstance(doc, bool):
        if "minimum" in schema and doc < schema["minimum"]:
            raise SchemaError(f"{path}: {doc} < minimum {schema['minimum']}")
        if "maximum" in schema and doc > schema["maximum"]:
            raise SchemaError(f"{path}: {doc} > maximum {schema['maximum']}")

    if isinstance(doc, dict):
        for req in schema.get("required") or ():
            if req not in doc:
                raise SchemaError(f"{path}: missing required key {req!r}")
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extra = set(doc) - set(props)
            if extra:
                raise SchemaError(f"{path}: unexpected properties {sorted(extra)}")
        for k, sub in props.items():
            if k in doc:
                validate_json_schema(doc[k], sub, root, f"{path}.{k}")

    if isinstance(doc, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(doc):
                validate_json_schema(item, item_schema, root, f"{path}[{idx}]")


def _matches_type(val, t: str) -> bool:
    if t == "null":
        return val is None
    if t == "boolean":
        return isinstance(val, bool)
    if t == "integer":
        return type(val) is int
    if t == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if t == "string":
        return isinstance(val, str)
    if t == "array":
        return isinstance(val, list)
    if t == "object":
        return isinstance(val, dict)
    return True


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _safe_name(s: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in str(s))


def out_dir(project: Path) -> Path:
    d = project / ".ras" / "out"
    d.mkdir(parents=True, exist_ok=True)
    return d


def emit_run(project: Path, kind: str, payload: dict, suffix: str | None = None) -> Path:
    doc = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "generated_at": _iso_now(),
        "project": project.name,
        **payload,
    }
    doc = scrub(doc, project)
    fname = f"{kind}-{_safe_name(suffix)}.json" if suffix else f"{kind}.json"
    p = out_dir(project) / fname
    save_json(p, doc)
    return p


def emit_event(project: Path, event_type: str, data: dict | None = None, *,
               severity: str = "INFO", trace_id: str | None = None,
               function_id: str | None = None, dependency: str | None = None,
               ts: str | None = None) -> dict:
    p = out_dir(project) / "events.ndjson"
    seq = 1
    if p.is_file():
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if lines:
            try:
                seq = int(json.loads(lines[-1]).get("seq", len(lines))) + 1
            except ValueError:
                seq = len(lines) + 1
    ev = scrub({
        "schema_version": SCHEMA_VERSION,
        "kind": "event",
        "seq": seq,
        "ts": ts or _iso_now(),
        "event_type": event_type,
        "severity": severity,
        "trace_id": trace_id,
        "function_id": function_id,
        "dependency": dependency,
        "data": data or {},
    }, project)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, sort_keys=True) + "\n")
    return ev


def read_events(project: Path, since: int = 0, limit: int = 200) -> dict:
    p = project / ".ras" / "out" / "events.ndjson"
    if not p.is_file():
        return {"schema_version": SCHEMA_VERSION, "cursor": since, "events": []}
    events = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if int(ev.get("seq", 0)) > since:
            events.append(ev)
    events = events[:max(1, int(limit))]
    cursor = max((int(e.get("seq", 0)) for e in events), default=since)
    return {"schema_version": SCHEMA_VERSION, "cursor": cursor, "events": events}


def _side_metrics(s: dict | None) -> dict:
    if not s:
        return {}
    return {
        "availability": float(s.get("availability") or 0.0),
        "identical_rate": s.get("identical_rate"),
        "crash_rate": s.get("crash_rate"),
        "hang_rate": s.get("hang_rate"),
        "wrong": int(s.get("wrong") or 0),
        "hang": int(s.get("hang") or 0),
        "dups": int(s.get("dups") or 0),
        "p50_s": s.get("p50_wall") if "p50_wall" in s else s.get("p50"),
        "p99_s": s.get("p99_wall") if "p99_wall" in s else s.get("p99"),
        "amplification": s.get("amplification"),
    }


def _fn_entry(fid: str, v: dict, m: dict) -> dict:
    o, h = v.get("original") or {}, v.get("hardened") or {}
    entry = {
        "id": fid,
        "lang": v.get("lang") or m.get("lang") or "python",
        "passed": bool(v.get("passed")),
        "budget_s": float(v.get("budget_s") or 0.0),
        "happy": {"equal": int((v.get("happy") or {}).get("equal") or 0),
                  "total": int((v.get("happy") or {}).get("total") or 0)},
        "original": _side_metrics(o),
        "hardened": _side_metrics(h),
        "gates": v.get("gates") or {},
    }
    if v.get("ai"):
        entry["ai"] = v["ai"]
    return entry


def coverage_payload() -> dict:
    registered = {a.name for a in lang_mod.ADAPTERS}
    adapters = []
    for name, status, boundary, ver, ax in ALL_LANGUAGES:
        adapters.append({
            "lang": name,
            "status": "complete" if name in registered and status == "complete" else status,
            "boundary": boundary,
            "verify": ver,
            "ax": ax,
        })
    return {"adapters": adapters}


def sync_out(project: Path) -> dict[str, Path]:
    """Build or refresh .ras/out/*.json and seed events.ndjson from project .ras/ state."""
    written: dict[str, Path] = {}
    scan = load_json(project / ".ras" / "scan.json")
    bench = load_json(project / ".ras" / "bench.json")
    slo_doc = load_json(project / ".ras" / "slo.json")
    state = load_json(project / ".ras" / "state.json") or {}
    flags = load_json(project / ".ras" / "flags.json") or {}
    pat_lab = load_json(project / ".ras" / "patterns-lab" / "results.json")

    hardens_dir = project / ".ras" / "hardens"
    fn_entries: list[dict] = []
    verify_docs: list[dict] = []
    ax_records: list[dict] = []
    srv_list: list[dict] = []
    ai_latest = None

    if hardens_dir.is_dir():
        for d in sorted(hardens_dir.iterdir()):
            v = load_json(d / "verify.json")
            m = load_json(d / "meta.json") or {}
            if v and not v.get("fatal"):
                fe = _fn_entry(d.name, v, m)
                fn_entries.append(fe)
                verify_docs.append(v)
                written[f"verify-{d.name}"] = emit_run(
                    project, "verify",
                    {"lang": fe["lang"], "functions": [fe],
                     **({"ai": v["ai"]} if v.get("ai") else {}),
                     "serviceability": {
                         "orig_detection_rate": ((v.get("serviceability") or {}).get("original") or {}).get("detection_rate"),
                         "hard_detection_rate": ((v.get("serviceability") or {}).get("hardened") or {}).get("detection_rate"),
                         "hard_diagnosis_accuracy": ((v.get("serviceability") or {}).get("hardened") or {}).get("diagnosis_accuracy"),
                     }},
                    suffix=d.name,
                )
                if v.get("ax"):
                    ax_records.append(v["ax"])
                if v.get("serviceability"):
                    srv_list.append(v["serviceability"])
                if v.get("ai"):
                    ai_latest = v["ai"]

    if scan:
        written["scan"] = emit_run(project, "scan", {
            "lang": scan.get("lang", "python"),
            "functions": [{"id": f["id"], "lang": scan.get("lang", "python"),
                           "risk": f.get("risk"), "exposure": f.get("exposure"),
                           "score": f.get("score"), "signals": f.get("signals") or []}
                          for f in (scan.get("functions") or [])],
        })

    if bench:
        bo = (bench.get("summary") or {}).get("original") or {}
        bh = (bench.get("summary") or {}).get("hardened") or {}
        written["bench"] = emit_run(project, "bench", {
            "lang": bench.get("lang", "python"),
            "bench": {
                "seeds": int(bench.get("seeds") or 0),
                "rate": float(bench.get("rate") or 0.0),
                "original": _side_metrics(bo),
                "hardened": _side_metrics(bh),
            },
        })

    if slo_doc:
        sh = slo_doc.get("hardened") or {}
        written["slo"] = emit_run(project, "slo", {
            "slo": {
                "source": slo_doc.get("source", "bench"),
                "targets": slo_doc.get("targets") or {},
                "original": slo_doc.get("original") or {},
                "hardened": sh,
                "series": [{
                    "ts": _iso_now(),
                    "availability": float(sh.get("availability") or 0.0),
                    "p99_s": float(sh.get("p99_s") or 0.0),
                    "error_rate": float(sh.get("error_rate") or 0.0),
                    "burn_pct": float(sh.get("error_budget_burn_pct") or 0.0),
                }] if sh else [],
            },
        })

    # Incidents -> replay-<id>.json
    inc_dir = project / ".ras" / "incidents"
    incidents_summary: list[dict] = []
    if inc_dir.is_dir():
        for ip in sorted(inc_dir.glob("*.json")):
            inc = load_json(ip)
            if not inc:
                continue
            iid = str(inc.get("id") or ip.stem)
            entry = {
                "id": iid,
                "trace_id": str(inc.get("trace_id") or ""),
                "function_id": str(inc.get("function_id") or inc.get("function") or ""),
                "cause": str(((inc.get("outcome") or {}).get("problem") or {}).get("cause")
                             or (inc.get("outcome") or {}).get("error_type") or "unknown"),
                "rca_steps": list(inc.get("rca_steps") or ["captured boundary args + error at failure"]),
                "reproduced": bool(inc.get("reproduced", True)),
            }
            incidents_summary.append(entry)
            existing_replay = load_json(out_dir(project) / f"replay-{iid}.json") or {}
            merged_replay = {k: v for k, v in existing_replay.items()
                             if k not in ("schema_version", "kind", "generated_at", "project")}
            merged_replay["incidents"] = [entry]
            written[f"replay-{iid}"] = emit_run(project, "replay", merged_replay, suffix=iid)

    written["coverage"] = emit_run(project, "coverage", {"coverage": coverage_payload()})

    if pat_lab and isinstance(pat_lab, dict):
        pat_rows = []
        for row in pat_lab.get("patterns") or []:
            pat_rows.append({
                "name": str(row.get("name")),
                "verdict": str(row.get("verdict") or "adopted"),
                "off_availability": float((row.get("off") or {}).get("availability") or 0.0),
                "on_availability": float((row.get("on") or {}).get("availability") or 0.0),
                "amplification": float((row.get("on") or {}).get("amplification") or 1.0),
            })
        if pat_rows:
            written["patterns"] = emit_run(project, "patterns", {"patterns": pat_rows})

    # Aggregated report & status
    ax_summary = None
    if ax_records:
        cat = ax_records[-1].get("catalog") or {}
        ax_summary = {
            "original_accuracy": ((cat.get("original") or {}).get("accuracy") or 0.0),
            "hardened_accuracy": ((cat.get("hardened") or {}).get("accuracy") or 0.0),
            "leaks": sum(len(a.get("leaks") or []) for a in ax_records),
            "records": [],
        }
    srv_summary = None
    if srv_list:
        s0 = srv_list[0]
        srv_summary = {
            "mttd_s": None,
            "mttr_s": None,
            "restore_s": None,
            "orig_detection_rate": (s0.get("original") or {}).get("detection_rate"),
            "hard_detection_rate": (s0.get("hardened") or {}).get("detection_rate"),
            "hard_diagnosis_accuracy": (s0.get("hardened") or {}).get("diagnosis_accuracy"),
        }
    enabled_map = state.get("enabled") or {}
    rollout = {
        "enabled": enabled_map,
        "canary_pct": 100.0 if enabled_map else 0.0,
        "shadow": bool((flags.get("flags") or {}).get("shadow")),
        "blast_radius_functions": len(enabled_map),
    }
    resilience_state = {
        "breakers": {},
        "bulkheads": {},
        "flags": flags,
    }

    report_payload: dict = {
        "lang": (bench or scan or {}).get("lang") or (fn_entries[0]["lang"] if fn_entries else "python"),
        "functions": fn_entries,
        "rollout": rollout,
        "resilience_state": resilience_state,
        "coverage": coverage_payload(),
    }
    if bench:
        report_payload["bench"] = {
            "seeds": int(bench.get("seeds") or 0),
            "rate": float(bench.get("rate") or 0.0),
            "original": _side_metrics((bench.get("summary") or {}).get("original")),
            "hardened": _side_metrics((bench.get("summary") or {}).get("hardened")),
        }
    if slo_doc:
        report_payload["slo"] = {
            "source": slo_doc.get("source", "bench"),
            "targets": slo_doc.get("targets") or {},
            "original": slo_doc.get("original") or {},
            "hardened": slo_doc.get("hardened") or {},
        }
    if ax_summary:
        report_payload["ax"] = ax_summary
    if srv_summary:
        report_payload["serviceability"] = srv_summary
    if ai_latest:
        report_payload["ai"] = ai_latest
    if incidents_summary:
        report_payload["incidents"] = incidents_summary

    written["report"] = emit_run(project, "report", report_payload)
    written["status"] = emit_run(project, "status", {
        "lang": report_payload["lang"],
        "rollout": rollout,
        "resilience_state": resilience_state,
        "functions": fn_entries,
    })

    # Seed events.ndjson if empty so UI timeline has real milestones from the run
    ev_path = out_dir(project) / "events.ndjson"
    if not ev_path.is_file() or not ev_path.read_text(encoding="utf-8").strip():
        if scan:
            emit_event(project, "scan.completed", {"functions": len(scan.get("functions") or [])})
        for fe in fn_entries:
            emit_event(
                project,
                "verify.passed" if fe["passed"] else "verify.failed",
                {"availability_original": fe["original"].get("availability"),
                 "availability_hardened": fe["hardened"].get("availability")},
                severity="INFO" if fe["passed"] else "WARN",
                function_id=fe["id"],
            )
        if bench:
            bo = (bench.get("summary") or {}).get("original") or {}
            bh = (bench.get("summary") or {}).get("hardened") or {}
            emit_event(project, "bench.completed", {
                "seeds": bench.get("seeds"),
                "availability_original": bo.get("availability"),
                "availability_hardened": bh.get("availability"),
            })
        if slo_doc:
            st = (slo_doc.get("hardened") or {}).get("status", "met")
            emit_event(project, "slo.evaluated", {"status": st},
                       severity="INFO" if st == "met" else "ERROR")
    return written


def _prom_lines(project: Path, bundle: dict) -> str:
    proj = project.name
    lines: list[str] = []

    def gauge(name: str, help_txt: str, samples: list[tuple[dict, float]]):
        if not samples:
            return
        lines.append(f"# HELP {name} {help_txt}")
        lines.append(f"# TYPE {name} gauge")
        for labels, val in samples:
            lbl = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"{name}{{{lbl}}} {val}")

    rep = bundle.get("report") or {}
    b = rep.get("bench") or {}
    avail_samples = []
    ident_samples = []
    crash_samples = []
    hang_samples = []
    p99_samples = []
    dup_samples = []

    if b:
        for side in ("original", "hardened"):
            s = b.get(side) or {}
            if "availability" in s:
                avail_samples.append(({"project": proj, "scope": "bench", "variant": side}, s["availability"]))
            if s.get("identical_rate") is not None:
                ident_samples.append(({"project": proj, "scope": "bench", "variant": side}, s["identical_rate"]))
            if s.get("crash_rate") is not None:
                crash_samples.append(({"project": proj, "scope": "bench", "variant": side}, s["crash_rate"]))
            if s.get("hang_rate") is not None:
                hang_samples.append(({"project": proj, "scope": "bench", "variant": side}, s["hang_rate"]))
            if s.get("p99_s") is not None:
                p99_samples.append(({"project": proj, "scope": "bench", "variant": side}, s["p99_s"]))
            dup_samples.append(({"project": proj, "scope": "bench", "variant": side}, s.get("dups", 0)))

    for fn in rep.get("functions") or []:
        fid = fn["id"]
        for side in ("original", "hardened"):
            s = fn.get(side) or {}
            if "availability" in s:
                avail_samples.append(({"project": proj, "scope": "verify", "function": fid, "variant": side}, s["availability"]))
            if s.get("p99_s") is not None:
                p99_samples.append(({"project": proj, "scope": "verify", "function": fid, "variant": side}, s["p99_s"]))
            dup_samples.append(({"project": proj, "scope": "verify", "function": fid, "variant": side}, s.get("dups", 0)))

    gauge("ras_availability_ratio", "Availability ratio (0..1) under injected faults", avail_samples)
    gauge("ras_identical_ratio", "Strict byte-identical output ratio (0..1)", ident_samples)
    gauge("ras_crash_rate", "Whole-app crash rate (0..1)", crash_samples)
    gauge("ras_hang_rate", "Whole-app hang rate (0..1)", hang_samples)
    gauge("ras_p99_seconds", "p99 latency in seconds", p99_samples)
    gauge("ras_duplicates_total", "Duplicate non-idempotent side effects", dup_samples)

    slo = rep.get("slo") or {}
    if slo.get("hardened"):
        burn_o = float((slo.get("original") or {}).get("error_budget_burn_pct") or 0) / 100.0
        burn_h = float(slo["hardened"].get("error_budget_burn_pct") or 0) / 100.0
        gauge("ras_error_budget_burn_ratio", "Error budget burn ratio (1.0 = 100%)", [
            ({"project": proj, "variant": "original"}, burn_o),
            ({"project": proj, "variant": "hardened"}, burn_h),
        ])

    ai = rep.get("ai") or {}
    if ai.get("hardened"):
        ah, ao = ai["hardened"], ai.get("original") or {}
        if ah.get("semantic_success_rate") is not None:
            gauge("ras_ai_semantic_success_ratio", "AI semantic success rate (0..1)", [
                ({"project": proj, "variant": "original"}, ao.get("semantic_success_rate") or 0.0),
                ({"project": proj, "variant": "hardened"}, ah["semantic_success_rate"]),
            ])
        if ah.get("injection_resistance") is not None:
            gauge("ras_ai_injection_resistance_ratio", "Prompt-injection resistance rate (0..1)", [
                ({"project": proj, "variant": "original"}, ao.get("injection_resistance") or 0.0),
                ({"project": proj, "variant": "hardened"}, ah["injection_resistance"]),
            ])
        if ah.get("cost_per_success_usd") is not None:
            gauge("ras_ai_cost_per_success_usd", "AI USD cost per semantic success", [
                ({"project": proj, "variant": "original"}, ao.get("cost_per_success_usd") or 0.0),
                ({"project": proj, "variant": "hardened"}, ah["cost_per_success_usd"]),
            ])

    ax = rep.get("ax") or {}
    if ax:
        gauge("ras_ax_accuracy_ratio", "AX policy agent next-action accuracy (0..1)", [
            ({"project": proj, "variant": "original"}, ax.get("original_accuracy") or 0.0),
            ({"project": proj, "variant": "hardened"}, ax.get("hardened_accuracy") or 0.0),
        ])

    srv = rep.get("serviceability") or {}
    if srv.get("hard_detection_rate") is not None:
        gauge("ras_serviceability_detection_ratio", "Fault detection rate from logs (0..1)", [
            ({"project": proj, "variant": "original"}, srv.get("orig_detection_rate") or 0.0),
            ({"project": proj, "variant": "hardened"}, srv["hard_detection_rate"]),
        ])
    return "\n".join(lines) + ("\n" if lines else "")


def _otlp_json(project: Path, bundle: dict, events: list[dict]) -> dict:
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


def export_data(project: Path | None = None, fmt: str = "json", out_path: Path | str | None = None) -> str:
    project = project or find_project()
    if fmt not in ("json", "ndjson", "prom", "otlp-json"):
        raise UserError(f"unsupported export format {fmt!r}", "choose json | ndjson | prom | otlp-json", "export --format json")

    sync_out(project)
    od = out_dir(project)
    snapshots = {}
    for jp in sorted(od.glob("*.json")):
        snapshots[jp.stem] = load_json(jp)
    ev_list = read_events(project, since=0, limit=500)["events"]
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "kind": "bundle",
        "generated_at": _iso_now(),
        "project": project.name,
        "snapshots": snapshots,
        "events": ev_list,
    }
    if fmt == "json":
        text = json.dumps(scrub(bundle, project), indent=2, sort_keys=True) + "\n"
    elif fmt == "ndjson":
        ev_p = od / "events.ndjson"
        text = ev_p.read_text(encoding="utf-8") if ev_p.is_file() else ""
    elif fmt == "prom":
        text = _prom_lines(project, {"report": snapshots.get("report")})
    else:
        text = json.dumps(scrub(_otlp_json(project, {"report": snapshots.get("report")}, ev_list), project),
                          indent=2, sort_keys=True) + "\n"

    if out_path:
        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    return text
