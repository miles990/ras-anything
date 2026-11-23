"""Deterministic log-only diagnoser and serviceability metrics (V-8).

Reads ONLY the stdout/stderr of a faulted run (never inspects source code or injected plans) and infers:
  - detected: whether any failure / fault / degradation was visible in logs
  - dependency: logical dependency name, route, host, or subprocess program
  - fault_class: timeout | refused | unavailable | rate_limited | truncated | invalid_request | auth |
                 conflict | quota | circuit_open | rejected | approval | slow | bug | unknown
  - lines_to_diagnosis: 1-based record/line count read before reaching the diagnosis (proxy for TTD)
  - ttd_ms: time from fault start (if known) to first diagnostic record

Also provides DORA failed-deployment time-to-restore measurement for a bad enable -> disable / kill-switch.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import lang, paths, runner

DIAG_EVENTS = frozenset({
    "ras.dependency.failure",
    "ras.dependency.gave_up",
    "ras.dependency.slow",
    "ras.breaker.transition",
    "ras.incident",
    "ras.approval.denied",
    "fallback",
    "degraded",
})

_FAULT_ALIASES = {
    "http_503": "unavailable",
    "503": "unavailable",
    "502": "unavailable",
    "500": "unavailable",
    "http_429": "rate_limited",
    "429": "rate_limited",
    "hang": "timeout",
    "slow": "slow",
    "down": "unavailable",
    "outage": "unavailable",
    "http_404": "invalid_request",
    "404": "invalid_request",
    "400": "invalid_request",
    "422": "invalid_request",
    "401": "auth",
    "403": "auth",
    "409": "conflict",
    "402": "quota",
}

_TEXT_FAULTS = (
    (("timed out", "timeouterror", "timeoutexpired", "etimedout", "deadlineexceeded", "esockettimedout", "und_err_connect_timeout", "und_err_headers_timeout"), "timeout"),
    (("connectionrefusederror", "econnrefused", "connection refused", "blockingioerror", "eagain", "enotfound"), "refused"),
    (("circuitopen", "circuit open", "circuit_open"), "circuit_open"),
    (("429", "rate_limited", "rate limit", "too many requests"), "rate_limited"),
    (("503", "502", "504", "econnreset", "epipe", "unavailable", "exited 75", "exit 75", "remote disconnected"), "unavailable"),
    (("incompleteread", "jsondecodeerror", "syntaxerror", "unterminated", "truncated", "unexpected end of json", "terminated"), "truncated"),
    (("401", "403", "unauthorized", "forbidden"), "auth"),
    (("400", "404", "422", "bad request", "not found"), "invalid_request"),
    (("409", "conflict"), "conflict"),
    (("402", "quota", "insufficient_quota"), "quota"),
    (("typeerror", "keyerror", "attributeerror", "indexerror", "nameerror", "zerodivisionerror", "referenceerror", "rangeerror"), "bug"),
)

_URL_RE = re.compile(r"https?://([^\s\"'<>]+)")


def _fault_from_text(text: str) -> str | None:
    low = (text or "").lower()
    for words, cls in _TEXT_FAULTS:
        if any(w in low for w in words):
            return cls
    return None


def _dep_from_url(url: str) -> str | None:
    try:
        u = urlsplit(url)
        segs = [s for s in (u.path or "").split("/") if s]
        return segs[-1] if segs else (u.hostname or None)
    except Exception:
        return None


def _dep_from_attrs(attrs: dict) -> str | None:
    if attrs.get("ras.dependency"):
        return str(attrs["ras.dependency"])
    if attrs.get("url.template"):
        segs = [s for s in str(attrs["url.template"]).split("/") if s]
        if segs:
            return segs[-1]
    if attrs.get("url.path"):
        segs = [s for s in str(attrs["url.path"]).split("/") if s]
        if segs:
            return segs[-1]
    if attrs.get("process.command"):
        return os.path.basename(str(attrs["process.command"]).split()[0])
    if attrs.get("server.address"):
        return str(attrs["server.address"])
    return None


def diagnose(stderr_text: str, stdout_text: str = "", start_ns: int | None = None) -> dict:
    """Inspect only stdout/stderr lines and infer {detected, dependency, fault_class, lines_read, ttd_ms, trace_id}."""
    lines = [ln for ln in (stderr_text or "").splitlines() if ln.strip()]
    detected = False
    first_line = None
    first_ts_ns = None
    dep = None
    fault = None
    trace_id = None
    diag_line = None
    otel_records = 0

    for idx, ln in enumerate(lines, 1):
        rec = None
        if ln.lstrip().startswith("{"):
            try:
                obj = json.loads(ln)
                if isinstance(obj, dict) and ("EventName" in obj or "event" in obj):
                    rec = obj
            except ValueError:
                rec = None
        if rec is not None:
            ev = rec.get("EventName") or rec.get("event") or ""
            sev = str(rec.get("SeverityText") or "").upper()
            attrs = rec.get("Attributes") if isinstance(rec.get("Attributes"), dict) else rec
            is_diag = ev in DIAG_EVENTS or sev in ("WARN", "ERROR") or "error" in attrs or "ras.fault" in attrs
            if not is_diag:
                continue
            if ev == "ras.breaker.transition" and attrs.get("ras.breaker.state") == "closed":
                continue
            otel_records += 1
            if not detected:
                detected = True
                first_line = idx
                trace_id = rec.get("TraceId") or rec.get("trace_id")
                ts_raw = rec.get("Timestamp")
                if ts_raw is not None and str(ts_raw).isdigit():
                    first_ts_ns = int(ts_raw)
            cand_dep = _dep_from_attrs(attrs)
            cand_fault = attrs.get("ras.fault")
            if not cand_fault and ev == "ras.breaker.transition" and attrs.get("ras.breaker.state") == "open":
                cand_fault = "circuit_open"
            if not cand_fault and attrs.get("http.response.status_code") is not None:
                cand_fault = _FAULT_ALIASES.get(str(attrs["http.response.status_code"]))
            if not cand_fault:
                cand_fault = _fault_from_text(" ".join(str(attrs.get(k) or "") for k in ("error.type", "exception.message", "error", "reason")) + " " + str(rec.get("Body") or ""))
            if cand_dep and dep is None:
                dep = cand_dep
            if cand_fault and cand_fault != "unknown" and (fault is None or fault == "unknown"):
                fault = cand_fault
            if diag_line is None and cand_dep and cand_fault:
                diag_line = idx
            continue

        # Unstructured stderr line (tracebacks / plain errors)
        m_url = _URL_RE.search(ln)
        if m_url and dep is None:
            dep = _dep_from_url("http://" + m_url.group(1))
        f_txt = _fault_from_text(ln)
        is_err_line = bool(f_txt) or "Traceback" in ln or "Error:" in ln or "Exception:" in ln
        if is_err_line:
            if not detected:
                detected = True
                first_line = idx
            if f_txt and fault is None:
                fault = f_txt
            if diag_line is None and f_txt:
                diag_line = idx

    # Also check stdout only if stderr didn't find anything (rare)
    if not detected and stdout_text:
        for idx, ln in enumerate(stdout_text.splitlines(), 1):
            f_txt = _fault_from_text(ln)
            if f_txt:
                detected = True
                fault = f_txt
                first_line = len(lines) + idx
                diag_line = first_line
                break

    ttd_ms = None
    if detected and first_ts_ns is not None and start_ns is not None:
        ttd_ms = max(0.0, round((first_ts_ns - start_ns) / 1e6, 3))

    return {
        "detected": detected,
        "dependency": dep,
        "fault_class": fault or ("unknown" if detected else None),
        "lines_read": diag_line or first_line,
        "ttd_ms": ttd_ms,
        "trace_id": trace_id,
        "otel_records": otel_records,
    }


def _norm_fault(f: str | None) -> str | None:
    if not f:
        return None
    f = f.lower().strip()
    return _FAULT_ALIASES.get(f, f)


def _fault_matches(predicted: str | None, truth: str | None) -> bool:
    p, t = _norm_fault(predicted), _norm_fault(truth)
    if not p or not t:
        return False
    if p == t:
        return True
    if t in ("outage", "down", "unavailable") and p in ("refused", "unavailable", "circuit_open", "timeout"):
        return True
    if t == "slow" and p in ("slow", "timeout"):
        return True
    return False


def _dep_matches(predicted: str | None, truth_key: str | None) -> bool:
    if not predicted or not truth_key:
        return False
    p = predicted.lower().strip()
    t = truth_key.lower().strip()
    if p == t:
        return True
    # truth_key can be "POST http://127.0.0.1/v1/charges" or "run label-tool --ok" or logical name
    if "://" in t:
        u = urlsplit(t.split()[-1])
        segs = [s for s in (u.path or "").split("/") if s]
        if p in segs or p == (u.path or "").lstrip("/") or p == (u.hostname or ""):
            return True
    tokens = [os.path.basename(x) for x in t.split()]
    return p in tokens or any(p in tok for tok in tokens if len(p) >= 3)


def score_diagnosis(diag: dict, truth_dep: str | None, truth_fault: str | None) -> dict:
    """Score a log-only diagnosis against the injected ground truth."""
    det = bool(diag.get("detected"))
    ok_dep = det and _dep_matches(diag.get("dependency"), truth_dep)
    ok_fault = det and _fault_matches(diag.get("fault_class"), truth_fault)
    return {
        "detected": det,
        "correct_dependency": ok_dep,
        "correct_fault": ok_fault,
        "accurate": bool(ok_dep and ok_fault),
        "lines_to_diagnosis": diag.get("lines_read"),
        "ttd_ms": diag.get("ttd_ms"),
    }


def summarize_serviceability(scored: list[dict], restore_s: float | None = None) -> dict:
    """Aggregate detection rate, TTD, diagnosis accuracy, lines-to-diagnosis, and time-to-restore."""
    n = len(scored)
    if n == 0:
        return {
            "runs": 0,
            "detection_rate": None,
            "diagnosis_accuracy": None,
            "ttd_ms_p50": None,
            "lines_to_diagnosis_p50": None,
            "restore_s": restore_s,
        }
    det = sum(1 for x in scored if x["detected"])
    acc = sum(1 for x in scored if x["accurate"])
    lines = sorted(x["lines_to_diagnosis"] for x in scored if x.get("lines_to_diagnosis") is not None)
    ttds = sorted(x["ttd_ms"] for x in scored if x.get("ttd_ms") is not None)
    return {
        "runs": n,
        "detection_rate": det / n,
        "diagnosis_accuracy": acc / n,
        "ttd_ms_p50": ttds[len(ttds) // 2] if ttds else None,
        "lines_to_diagnosis_p50": lines[len(lines) // 2] if lines else None,
        "restore_s": round(restore_s, 4) if restore_s is not None else None,
    }


def measure_restore(project: Path, fid: str, cmd: list[str], baseline_stdout: str | None = None,
                    re_enable: bool = False) -> dict:
    """DORA failed-deployment recovery time: detect failing enabled code, run `disable(fid)`, verify byte-exact restore and smoke run."""
    from .enable import disable, enable

    meta = paths.load_json(paths.harden_dir(project, fid) / "meta.json")
    src = paths.resolve_in_project(meta["file"], project)
    state = paths.load_json(project / ".ras" / "state.json", {"enabled": {}})
    was_enabled = fid in state.get("enabled", {})
    if not was_enabled:
        enable(fid, quiet=True)
    state = paths.load_json(project / ".ras" / "state.json", {"enabled": {}})
    sha_expected = state["enabled"][fid]["sha_before"]

    adapter = lang.detect(cmd)
    resolved = adapter.resolve(cmd, project)
    t_det = time.perf_counter()
    pre = runner.run(adapter, resolved, {"mode": "clean", "seed": None}, project, timeout=60)
    ttd_wall = time.perf_counter() - t_det
    detected_bad = pre.code != 0 or (baseline_stdout is not None and pre.stdout != baseline_stdout)

    t0 = time.perf_counter()
    disable(fid, quiet=True)
    disable_wall = time.perf_counter() - t0
    sha_now = paths.sha256_file(src)
    smoke = runner.run(adapter, resolved, {"mode": "clean", "seed": None}, project, timeout=60)
    restore_wall = time.perf_counter() - t0
    smoke_ok = smoke.code == 0 and (baseline_stdout is None or smoke.stdout == baseline_stdout)

    if re_enable and was_enabled and not detected_bad:
        enable(fid, quiet=True)

    return {
        "fid": fid,
        "detected_bad_enable": detected_bad,
        "byte_exact": sha_now == sha_expected,
        "smoke_ok": smoke_ok,
        "restored": (sha_now == sha_expected) and smoke_ok,
        "ttd_s": round(ttd_wall, 4),
        "disable_s": round(disable_wall, 4),
        "restore_s": round(restore_wall, 4),
        "mttr_s": round(ttd_wall + restore_wall, 4),
        "post_stderr": smoke.stderr or "",
    }
