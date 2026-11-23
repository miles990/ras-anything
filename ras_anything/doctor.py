"""doctor and status."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import lang, ui
from .paths import find_project, load_json, sha256_file


def doctor() -> int:
    project = find_project()
    ui.headline("doctor", str(project))
    checks = []
    for a in lang.ADAPTERS:
        checks += a.doctor()
    for agent in ("claude", "codex"):
        p = shutil.which(agent)
        checks.append((bool(p) or None, "%s on PATH" % agent, p or "not found (only needed for --agent %s)" % agent))
    try:
        st = subprocess.run(["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True, timeout=10)
        if st.returncode != 0:
            checks.append((None, "git", "not a git repository (enable edits your source: commit first is safer)"))
        else:
            dirty = [ln for ln in st.stdout.splitlines() if ".ras/" not in ln]
            checks.append((not dirty or None, "git working tree clean", "%d uncommitted changes" % len(dirty) if dirty else ""))
    except (OSError, subprocess.TimeoutExpired):
        checks.append((None, "git", "git not found"))
    try:
        (project / ".ras").mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=project / ".ras"):
            pass
        checks.append((True, ".ras writable", str(project / ".ras")))
    except OSError as e:
        checks.append((False, ".ras writable", str(e)))
    bad = 0
    for ok, name, detail in checks:
        sym = ui.good(ui.OK) if ok else (ui.warn(ui.WARN) if ok is None else ui.bad(ui.FAIL))
        bad += ok is False
        print("  %s %-24s %s" % (sym, name, ui.dim(detail)))
    ui.next_hint("scan -- python app.py / node app.mjs" if not (project / ".ras" / "scan.json").exists() else "status")
    return 1 if bad else 0


def status() -> dict:
    project = find_project()
    ui.headline("status", str(project))
    scan = load_json(project / ".ras" / "scan.json")
    state = load_json(project / ".ras" / "state.json", {"enabled": {}})
    out = {"project": str(project), "scan": None, "hardens": [], "enabled": list(state["enabled"]), "bench": None}
    if not scan:
        print("  no scan yet")
        ui.next_hint("scan -- python app.py / node app.mjs")
        return out
    top = [f for f in scan["functions"] if f["hardenable"]][:3]
    out["scan"] = {"cmd": scan.get("display_cmd"), "top": [f["id"] for f in top]}
    print("  scan     %s %s" % (scan.get("display_cmd"), ui.dim("(%s)" % scan["created"][:16])))
    rows = []
    base = project / ".ras" / "hardens"
    nxt = None
    for d in sorted(base.iterdir()) if base.is_dir() else []:
        v = load_json(d / "verify.json")
        m = load_json(d / "meta.json")
        if m is None:
            continue
        hp = d / lang.by_name(m.get("lang")).hardened_file(m)
        cur = sha256_file(hp) if hp.exists() else None
        if v is None or v.get("fatal"):
            st = ui.warn("not verified" if v is None else "verify error")
            nxt = nxt or "verify %s" % d.name
        elif v.get("hardened_sha256") != cur:
            st = ui.warn("changed since verify")
            nxt = nxt or "verify %s" % d.name
        elif v["passed"]:
            st = ui.good(ui.OK + " verified")
        else:
            st = ui.bad(ui.FAIL + " failed verify")
            nxt = nxt or "verify %s" % d.name
        en = d.name in state["enabled"]
        avail = "%s → %s" % (ui.pct(v["original"]["availability"]), ui.pct(v["hardened"]["availability"])) if v and not v.get("fatal") else "—"
        rows.append([d.name, st, avail, ui.good("enabled") if en else ui.dim("off")])
        out["hardens"].append({"id": d.name, "verified": bool(v and v.get("passed") and v.get("hardened_sha256") == cur),
                               "enabled": en})
        if v and v.get("passed") and not en and v.get("hardened_sha256") == cur:
            nxt = nxt or "enable %s" % d.name
    if rows:
        print()
        print(ui.table(["function", "verify", "availability (verify)", "swap"], rows))
    else:
        print("  nothing hardened yet; top candidates: " + ", ".join(f["id"] for f in top))
        nxt = "harden %s --agent claude" % top[0]["id"] if top else "scan -- python app.py / node app.mjs"
    b = load_json(project / ".ras" / "bench.json")
    if b:
        o, h = b["summary"]["original"], b["summary"]["hardened"]
        out["bench"] = {"original": o["availability"], "hardened": h["availability"]}
        print()
        print("  bench    app availability %s → %s %s" % (ui.pct(o["availability"]), ui.bold(ui.pct(h["availability"])),
                                                         ui.dim("(%d seeds, %s)" % (b["seeds"], b["created"][:16]))))
        nxt = nxt or ("report" if not (project / "RAS-REPORT.md").exists() else None)
        if nxt is None:
            more = [f["id"] for f in scan["functions"] if f["hardenable"] and not (base / f["id"]).exists()]
            nxt = ("harden %s --agent claude" % more[0]) if more else "disable <id>   (to undo a swap)"
    elif state["enabled"]:
        nxt = nxt or "bench"
    try:
        from .flags import store as flag_store
        snap = flag_store(project).snapshot()
        out["flags"] = snap
        if snap["flags"]:
            print()
            print("  flags    %s  %s" % (snap["source"], ui.dim("v%d" % snap["version"])))
            for k, v in snap["flags"].items():
                print("           %s = %s" % (k, v["value"]))
    except Exception:
        pass
    # Health snapshot
    hfile = os.environ.get("RAS_ANYTHING_HEALTH_FILE")
    hpath = Path(hfile) if hfile else (project / ".ras" / "health.json")
    health_snap = load_json(hpath) if hpath.is_file() else None
    out["health"] = health_snap
    if isinstance(health_snap, dict):
        print()
        st_lbl = ui.good(health_snap.get("status", "ok")) if health_snap.get("status") == "ok" else ui.warn(str(health_snap.get("status", "degraded")))
        print("  health   %s  %s" % (st_lbl, ui.dim("updated %s" % (health_snap.get("updated") or "")[:19])))
        for dep_name, dep_st in (health_snap.get("dependencies") or {}).items():
            print("           %s: %s" % (dep_name, dep_st.get("state") or dep_st.get("status") or "?"))
    # SLO status
    slo_doc = load_json(project / ".ras" / "slo.json")
    out["slo"] = slo_doc
    if isinstance(slo_doc, dict) and slo_doc.get("hardened"):
        hs = slo_doc["hardened"]
        met = hs.get("status") == "met"
        lbl = ui.good(ui.OK + " met") if met else ui.bad(ui.FAIL + " missed")
        print()
        print("  slo      %s  %s" % (lbl, ui.dim("avail %.1f%% / target %.1f%% · burn %.0f%%" % (
            100.0 * (hs.get("availability") or 0.0),
            100.0 * slo_doc.get("targets", {}).get("availability", 0.99),
            hs.get("error_budget_burn_pct") or 0.0))))
    # Recorded incidents
    from . import replay as _replay
    incs = _replay.list_incidents(project)
    out["incidents"] = incs
    if incs:
        print()
        print("  incidents  %d recorded · latest %s (%s %s)" % (
            len(incs), incs[0]["id"], incs[0].get("dependency") or "?", incs[0].get("fault") or "?"))
        nxt = nxt or ("replay %s" % incs[0]["id"])
    ui.next_hint(nxt or "report")
    return out
