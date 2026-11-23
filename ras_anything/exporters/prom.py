"""`export --format prom`: Prometheus text exposition of report gauges."""

from __future__ import annotations

from pathlib import Path

FORMAT = "prom"
ORDER = 30


def render(project, od, bundle, snapshots, events) -> str:
    return _prom_lines(project, {"report": snapshots.get("report")})


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

