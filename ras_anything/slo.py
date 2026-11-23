"""V-9 SLI / SLO targets, evaluation, and error-budget burn."""

from __future__ import annotations

import time
from pathlib import Path

from . import ui
from .contracts import SCHEMA_VERSION
from .paths import UserError, load_json, save_json

DEFAULT_TARGETS = {
    "availability": 0.99,
    "p99_s": 5.0,
    "error_rate": 0.01,
}


def _parse_pct(raw: str) -> float:
    s = str(raw).strip()
    pct = s.endswith("%")
    if pct:
        s = s[:-1].strip()
    val = float(s)
    if pct or val > 1.0:
        val /= 100.0
    if not (0.0 <= val <= 1.0):
        raise UserError("invalid SLO percentage %r" % raw, "use e.g. availability=99.0 or error_rate=1%", "bench --slo availability=99.0,p99=1s,error_rate=1%")
    return round(val, 6)


def _parse_dur(raw: str) -> float:
    s = str(raw).strip().lower()
    if s.endswith("ms"):
        val = float(s[:-2].strip()) / 1000.0
    elif s.endswith("s"):
        val = float(s[:-1].strip())
    else:
        val = float(s)
    if val <= 0:
        raise UserError("invalid SLO duration %r" % raw, "duration must be > 0 (e.g. p99=1s or p99=500ms)", "bench --slo p99=1s")
    return round(val, 6)


def parse_slo(spec: str | dict | None = None, project: Path | None = None) -> dict:
    """Resolve SLO targets from defaults, .ras/slo.json, and --slo override."""
    targets = dict(DEFAULT_TARGETS)
    if project is not None:
        existing = load_json(project / ".ras" / "slo.json")
        if isinstance(existing, dict) and isinstance(existing.get("targets"), dict):
            for k in ("availability", "p99_s", "error_rate"):
                if existing["targets"].get(k) is not None:
                    targets[k] = float(existing["targets"][k])
    if isinstance(spec, dict):
        for k in ("availability", "p99_s", "error_rate"):
            if spec.get(k) is not None:
                targets[k] = float(spec[k])
    elif isinstance(spec, str) and spec.strip():
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise UserError("invalid --slo item %r" % part, "use key=value pairs separated by commas",
                                "bench --slo availability=99.0,p99=1s,error_rate=1%")
            k, v = [x.strip() for x in part.split("=", 1)]
            k = k.lower()
            if k in ("availability", "avail"):
                targets["availability"] = _parse_pct(v)
            elif k in ("p99", "p99_s", "latency"):
                targets["p99_s"] = _parse_dur(v)
            elif k in ("error_rate", "errors", "err"):
                targets["error_rate"] = _parse_pct(v)
            else:
                raise UserError("unknown SLO key %r" % k, "supported keys: availability, p99, error_rate",
                                "bench --slo availability=99.0,p99=1s,error_rate=1%")
    return targets


def _eval_side(sli: dict, targets: dict) -> dict:
    avail = float(sli["availability"])
    err_rate = round(max(0.0, 1.0 - avail), 6)
    p50 = sli.get("p50_s")
    p99 = sli.get("p99_s")
    budget = max(1e-9, 1.0 - targets["availability"])
    burn_ratio = err_rate / budget
    burn_pct = round(burn_ratio * 100.0, 1)
    checks = {
        "availability": avail >= targets["availability"] - 1e-9,
        "p99_s": (p99 is not None) and (p99 <= targets["p99_s"] + 1e-9),
        "error_rate": err_rate <= targets["error_rate"] + 1e-9,
    }
    return {
        "availability": round(avail, 6),
        "error_rate": err_rate,
        "p50_s": round(p50, 6) if p50 is not None else None,
        "p99_s": round(p99, 6) if p99 is not None else None,
        "error_budget": round(budget, 6),
        "error_budget_burn_ratio": round(burn_ratio, 3),
        "error_budget_burn_pct": burn_pct,
        "checks": checks,
        "status": "met" if all(checks.values()) else "missed",
    }


def evaluate(project: Path | None = None, bench: dict | None = None, hardens: list | None = None,
             targets: str | dict | None = None, save: bool = False) -> dict | None:
    """Compute SLI/SLO results + error-budget burn from bench or verify results."""
    t = parse_slo(targets, project)
    source = None
    sides: dict = {}
    serviceability = None
    ax_res = None

    if bench and bench.get("summary"):
        source = "bench"
        for side in ("original", "hardened"):
            s = bench["summary"][side]
            sides[side] = _eval_side({
                "availability": s["availability"],
                "p50_s": s.get("p50_wall"),
                "p99_s": s.get("p99_wall"),
            }, t)
        serviceability = bench.get("serviceability")
        ax_res = bench.get("ax")
    elif hardens:
        source = "verify"
        for side in ("original", "hardened"):
            vals = [x["verify"][side] for x in hardens if x.get("verify") and not x["verify"].get("fatal")]
            if not vals:
                continue
            sides[side] = _eval_side({
                "availability": sum(v["availability"] for v in vals) / len(vals),
                "p50_s": max((v["p50"] for v in vals if v.get("p50") is not None), default=None),
                "p99_s": max((v["p99"] for v in vals if v.get("p99") is not None), default=None),
            }, t)
        svcs = [x["verify"].get("serviceability") for x in hardens if x.get("verify") and x["verify"].get("serviceability")]
        if svcs:
            serviceability = svcs[0]
        axs = [x["verify"].get("ax") for x in hardens if x.get("verify") and x["verify"].get("ax")]
        if axs:
            ax_res = axs[0]
    else:
        return None

    res = {
        "schema_version": SCHEMA_VERSION,
        "kind": "slo",
        "source": source,
        "targets": t,
        "original": sides.get("original"),
        "hardened": sides.get("hardened"),
        "serviceability": serviceability,
        "ax": ax_res,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if save and project is not None:
        save_json(project / ".ras" / "slo.json", res)
    return res


def print_slo(slo_res: dict) -> None:
    if not slo_res or not slo_res.get("hardened"):
        return
    t = slo_res["targets"]
    o, h = slo_res["original"], slo_res["hardened"]

    def badge(st):
        return ui.good("met") if st == "met" else ui.bad("missed")

    rows = [
        ["availability (target %s)" % ui.pct(t["availability"], 1), ui.pct(o["availability"], 1), ui.pct(h["availability"], 1),
         "%s → %s" % (badge("met" if o["checks"]["availability"] else "missed"), badge("met" if h["checks"]["availability"] else "missed"))],
        ["error rate (target ≤ %s)" % ui.pct(t["error_rate"], 1), ui.pct(o["error_rate"], 1), ui.pct(h["error_rate"], 1),
         "%s → %s" % (badge("met" if o["checks"]["error_rate"] else "missed"), badge("met" if h["checks"]["error_rate"] else "missed"))],
        ["p99 latency (target ≤ %s)" % ui.secs(t["p99_s"]), ui.secs(o["p99_s"]), ui.secs(h["p99_s"]),
         "%s → %s" % (badge("met" if o["checks"]["p99_s"] else "missed"), badge("met" if h["checks"]["p99_s"] else "missed"))],
        ["error-budget burn", "%.0f%%" % o["error_budget_burn_pct"], "%.0f%%" % h["error_budget_burn_pct"],
         "%s → %s" % (badge(o["status"]), badge(h["status"]))],
    ]
    print()
    print(ui.table(["SLO (%s)" % slo_res["source"], "original", "hardened", "verdict"], rows, "lrrl"))
