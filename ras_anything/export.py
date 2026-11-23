"""Visualization & telemetry data contract (V-19): .ras/out/ emitter, schema validator, and export."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import lang as lang_mod
from .contracts import EXPORT_FORMATS, SCHEMA_VERSION
from .exporters import RENDERERS
from .paths import UserError, find_project, load_json, plain_path, save_json, scrub

ALL_LANGUAGES = (
    ("python", "complete", "urllib / requests / httpx / aiohttp / socket / subprocess", True, True),
    ("node", "complete", "fetch / http / https / child_process (CJS + ESM + TS)", True, True),
    ("go", "complete", "net/http / os/exec: AST + rasagent + rasruntime", True, True),
    ("java", "complete", "HttpClient / HttpURLConnection / ProcessBuilder: javac + java_agent + RasRt", True, True),
    ("csharp", "planned", "HttpClient / Process", False, False),
    ("php", "planned", "curl / file_get_contents / proc_open", False, False),
    ("ruby", "planned", "Net::HTTP / Open3", False, False),
    ("kotlin", "planned", "HttpClient / OkHttp / ProcessBuilder", False, False),
    ("bun", "planned", "fetch / Bun.spawn", False, False),
    ("deno", "planned", "fetch / Deno.Command", False, False),
    ("c_cpp", "planned", "libcurl / popen", False, False),
    ("r", "planned", "httr2 / curl / system2", False, False),
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
    plain_path(project / ".ras", project, ".ras directory", kind="dir", must_exist=False)
    d = plain_path(project / ".ras" / "out", project, ".ras/out directory", kind="dir", must_exist=False)
    d.mkdir(parents=True, exist_ok=True)
    return d


def emit_run(project: Path, kind: str, payload: dict, suffix: str | None = None) -> Path:
    """Write a versioned snapshot. If the payload (everything except generated_at) is unchanged, the
    existing file is left alone so re-exporting does not churn timestamps."""
    doc = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "generated_at": _iso_now(),
        "project": project.name,
        **payload,
    }
    doc = scrub(doc, project)
    fname = f"{kind}-{_safe_name(suffix)}.json" if suffix else f"{kind}.json"
    p = plain_path(out_dir(project) / fname, project, "export file", kind="file", must_exist=False)
    existing = load_json(p)
    if existing and {k: v for k, v in existing.items() if k != "generated_at"} == {k: v for k, v in doc.items() if k != "generated_at"}:
        return p
    save_json(p, doc)
    return p


def emit_event(project: Path, event_type: str, data: dict | None = None, *,
               severity: str = "INFO", trace_id: str | None = None,
               function_id: str | None = None, dependency: str | None = None,
               ts: str | None = None) -> dict:
    p = plain_path(out_dir(project) / "events.ndjson", project, "events stream", kind="file", must_exist=False)
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
    built = {a.name for a in lang_mod.ALL_ADAPTERS}
    adapters = []
    for name, status, boundary, ver, ax in ALL_LANGUAGES:
        on = name in registered
        adapters.append({
            "lang": name,
            # status is implementation maturity; enabled says whether this run can use it (RAS_ANYTHING_LANGUAGES_OFF)
            "status": "complete" if name in built else status,  # a registered adapter implements the whole contract
            "enabled": on,
            "boundary": boundary,
            "verify": ver and on,
            "ax": ax and on,
        })
    listed = {name for name, *_ in ALL_LANGUAGES}
    for a in lang_mod.ADAPTERS:  # languages added by a plugin package (entry points)
        if a.name not in listed:
            adapters.append({"lang": a.name, "status": "complete", "enabled": True, "boundary": a.boundaries, "verify": True, "ax": True})
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
        slo_body = {
            "source": slo_doc.get("source", "bench"),
            "targets": slo_doc.get("targets") or {},
            "original": slo_doc.get("original") or {},
            "hardened": sh,
        }
        prev_slo = (load_json(out_dir(project) / "slo.json") or {}).get("slo") or {}
        prev_head = {k: prev_slo.get(k) for k in ("source", "targets", "original", "hardened")}
        if sh and prev_head == slo_body and prev_slo.get("series"):
            series = prev_slo["series"]  # keep the stamped point so a re-export does not rewrite ts
        elif sh:
            series = [{
                "ts": _iso_now(),
                "availability": float(sh.get("availability") or 0.0),
                "p99_s": float(sh.get("p99_s") or 0.0),
                "error_rate": float(sh.get("error_rate") or 0.0),
                "burn_pct": float(sh.get("error_budget_burn_pct") or 0.0),
            }]
        else:
            series = []
        written["slo"] = emit_run(project, "slo", {"slo": {**slo_body, "series": series}})

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
        o_det = [s["original"]["detection_rate"] for s in srv_list if (s.get("original") or {}).get("detection_rate") is not None]
        h_det = [s["hardened"]["detection_rate"] for s in srv_list if (s.get("hardened") or {}).get("detection_rate") is not None]
        h_acc = [s["hardened"]["diagnosis_accuracy"] for s in srv_list if (s.get("hardened") or {}).get("diagnosis_accuracy") is not None]
        srv_summary = {
            "mttd_s": None,
            "mttr_s": None,
            "restore_s": None,
            "orig_detection_rate": (sum(o_det) / len(o_det)) if o_det else None,
            "hard_detection_rate": (sum(h_det) / len(h_det)) if h_det else None,
            "hard_diagnosis_accuracy": (sum(h_acc) / len(h_acc)) if h_acc else None,
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


# Renderers live in ras_anything.exporters and are discovered, not listed here.


def export_data(project: Path | None = None, fmt: str = "json", out_path: Path | str | None = None) -> str:
    project = project or find_project()
    if fmt not in RENDERERS:
        raise UserError(f"unsupported export format {fmt!r}", "choose " + " | ".join(EXPORT_FORMATS), "export --format json")

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
    text = RENDERERS[fmt](project, od, bundle, snapshots, ev_list)

    if out_path:
        dest = Path(out_path)
        if dest.is_symlink():
            raise UserError(f"refusing to write --out {dest}: it is a symlink", "export will not follow symlinks", "export")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    return text
