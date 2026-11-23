"""verify: prove the hardened function keeps the happy path and is more available under faults."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import faults, static, ui
from .paths import PKG_DIR, UserError, find_project, harden_dir, load_json, load_local, save_json, scrub, sha256_file

CLASSES = ("ok", "degraded", "error", "wrong", "hang")


def resolve_id(project: Path, fid: str | None) -> str:
    base = project / ".ras" / "hardens"
    have = sorted(p.name for p in base.iterdir() if (p / "meta.json").exists()) if base.is_dir() else []
    if fid:
        if fid not in have:
            raise UserError("%s hasn't been hardened yet" % fid, "known: " + (", ".join(have) or "none"),
                            "harden %s" % fid)
        return fid
    if not have:
        raise UserError("nothing hardened yet", "verify checks .ras/hardens/<id>/hardened.py", "harden")
    if len(have) > 1:
        newest = max(have, key=lambda h: (base / h / "hardened.py").stat().st_mtime)
        return newest
    return have[0]


def verify(fid: str | None = None, max_chaos: int = 60, quiet: bool = False) -> dict:
    project = find_project()
    fid = resolve_id(project, fid)
    d = harden_dir(project, fid)
    meta = load_json(d / "meta.json")
    fd, out = tempfile.mkstemp(prefix="ras-verify-", suffix=".json")
    os.close(fd)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(PKG_DIR.parent)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])))
    env.pop("RAS_ANYTHING_CFG", None)
    if not quiet:
        ui.headline("verify", "%s %s %d recorded calls %s %d faults + 3 seeded mixes %s virtual clock" % (
            fid, ui.DOT, meta["recorded"], ui.DOT, len(faults.MATRIX), ui.DOT))
    t0 = time.perf_counter()
    peek = static.forbidden_imports(d / "hardened.py")
    if peek:
        res = {"passed": False, "id": fid, "fatal": {"stage": "accept hardened.py", "message":
               "hardened.py imports %s. Hardened code may use ras_anything.runtime only: the rest of ras-anything is "
               "the harness that judges it." % ", ".join(peek)}}
        return _save(res, d, fid, t0, quiet)
    try:
        exe = interpreter(project, meta)
        p = subprocess.run([exe, "-m", "ras_anything._verify_worker", str(d), out, str(max_chaos)],
                           env=env, capture_output=True, text=True, timeout=900)
        res = json.loads(Path(out).read_text() or "null")
    except subprocess.TimeoutExpired:
        res = {"passed": False, "fatal": {"stage": "replay", "message": "verify ran for 15 minutes without finishing: "
                                          "a busy loop that never calls the dependency or sleeps?"}}
    except json.JSONDecodeError:
        res = {"passed": False, "fatal": {"stage": "replay", "message": (p.stderr or "worker crashed")[-1500:]}}
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    return _save(scrub(res, project), d, fid, t0, quiet)


def interpreter(project: Path, meta: dict) -> str:
    """The app's Python: the one that ran it here, else the command's own, else this one."""
    exe = load_local(project).get("executable")
    if exe and os.path.exists(exe):
        return exe
    prog = (meta.get("cmd") or [""])[0]
    cand = str(project / prog) if "/" in prog else (shutil.which(prog) if os.path.basename(prog).startswith("python") else None)
    return cand if cand and os.path.exists(cand) else sys.executable


def _save(res: dict, d: Path, fid: str, t0: float, quiet: bool) -> dict:
    res["hardened_sha256"] = sha256_file(d / "hardened.py")
    res["verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    res["wall_s"] = round(time.perf_counter() - t0, 2)
    res["fault_model"] = {k: faults.DESCRIBE[k] for k in faults.MATRIX + ("mixed",)}
    save_json(d / "verify.json", res)
    if not quiet:
        print_verify(res, fid)
    return res


def _cls_line(s: dict) -> str:
    return " ".join("%s %d" % (k, s[k]) for k in CLASSES if s.get(k)) or "—"


def print_verify(res: dict, fid: str) -> None:
    print()
    if res.get("fatal"):
        f = res["fatal"]
        print("  " + ui.bad(ui.FAIL + " couldn't %s" % f["stage"]))
        for ln in f["message"].strip().splitlines()[-8:]:
            print("    " + ui.dim(ln))
        ui.next_hint("verify %s   (after fixing hardened.py)" % fid)
        return
    h, o, hp, g = res["hardened"], res["original"], res["happy"], res["gates"]
    budget = res["budget_s"]

    def mark(ok):
        return ui.good(ui.OK) if ok else ui.bad(ui.FAIL)

    def arrow(a, b, higher_better=True):
        if b == a:
            return " "
        return ui.good(ui.UP if b > a else ui.DOWN) if (b > a) == higher_better else ui.bad(ui.UP if b > a else ui.DOWN)

    nd = hp["original_nondeterministic"]
    rows = [
        ["happy-path replay (no faults)", "", "%d/%d identical" % (hp["equal"], hp["total"]), mark(g["happy"])],
        ["availability under faults", ui.pct(o["availability"]),
         arrow(o["availability"], h["availability"]) + " " + ui.bold(ui.pct(h["availability"])), mark(g["availability"])],
        ["silent wrong results", str(o["wrong"]), str(h["wrong"] + h["miss"]), mark(g["no_wrong"])],
        ["hangs", str(o["hang"]), str(h["hang"]), mark(g["no_hang"])],
        ["p99 latency (budget %s)" % ui.secs(budget), ui.secs(o["p99"]), ui.secs(h["p99"]), mark(g["p99"])],
        ["duplicate side effects", str(o["dups"]), str(h["dups"]), mark(g["dups"])],
    ]
    if h.get("logged_failures") is not None or o.get("logged_failures") is not None:
        rows.append(["failures logged w/ trace id", ui.pct(o.get("logged_failures"), 0),
                     ui.pct(h.get("logged_failures"), 0), ui.dim("info")])
    print(ui.table(["gate", "original", "hardened", ""], rows, "lrrl"))
    print(ui.dim("  %d trials each: %d calls × %d fault scenarios · availability = (ok + degraded) / trials" % (
        h["trials"], res["chaos"]["calls"], len(res["chaos"]["scenarios"]))))
    print(ui.dim("  wrong = neither the real answer nor the declared fallback · hang = no timeout, or over budget"))
    if nd:
        print(ui.warn("  %d recorded calls excluded: the original itself didn't reproduce them" % nd))
    print()
    scn = res["chaos"]["by_scenario"]
    srows = []
    for name, r in scn.items():
        n = r["n"]
        ok_o = r["original"].get("ok", 0) + r["original"].get("degraded", 0)
        ok_h = r["hardened"].get("ok", 0) + r["hardened"].get("degraded", 0)
        srows.append([name, "%d/%d" % (ok_o, n), "%d/%d" % (ok_h, n), _cls_line({**{k: 0 for k in CLASSES}, **r["original"]}),
                      _cls_line({**{k: 0 for k in CLASSES}, **r["hardened"]})])
    print(ui.table(["fault", "orig avail", "hard avail", "original", "hardened"], srows, "lrrll"))
    fd = res["chaos"].get("first_original_dup")
    if fd:
        print()
        att = fd["original"]["attempts"]
        steps = ", ".join("%s #%d %s%s" % (a["method"] or "run", a["n"], a["fault"] or "ok",
                                          " (reached server)" if a["reached"] and a["fault"] else "") for a in att)
        hdr = "the original double-sends" if fd["original"]["dups"] else "hardened double-sends"
        print("  " + ui.warn(ui.WARN + " %s: call #%d %s under %s" % (hdr, fd["call"], ui.clip(fd["args"], 70), fd["scenario"])))
        print("    original: %s %s %d duplicate%s" % (steps, ui.ARROW, fd["original"]["dups"], "" if fd["original"]["dups"] == 1 else "s"))
        hatt = fd["hardened"]["attempts"]
        keys = {a["idem"] for a in hatt if a["reached"]}
        print("    hardened: %s %s %d duplicates%s" % (
            ", ".join("%s #%d %s" % (a["method"] or "run", a["n"], a["fault"] or "ok") for a in hatt), ui.ARROW,
            fd["hardened"]["dups"], (" (one Idempotency-Key: %s)" % ui.clip(str(next(iter(keys))), 32)) if len(keys) == 1 and None not in keys else ""))
    ff = res["happy"].get("first_failure") if not g["happy"] else None
    cf = res["chaos"].get("first_failure")
    if ff or (cf and not res["passed"]):
        print()
        if ff:
            print("  " + ui.bad("first failing call (happy path, no faults): #%d %s" % (ff["call"], ff["args"])))
            _explain(ff)
        else:
            print("  " + ui.bad("first failing call: #%d %s under %s" % (cf["call"], cf["args"], cf["scenario"])))
            print("    fault schedule: " + ", ".join("%s#%d=%s" % (_k(a["key"]), a["n"], a["fault"] or "ok")
                                                   for a in cf["hardened"]["attempts"]) or "    (no dependency calls)")
            _explain(cf["hardened"], cf["original"])
    print()
    if res["passed"]:
        print("  " + ui.good(ui.bold(ui.OK + " PASS")) + "  enable is unlocked for this hardened.py " +
              ui.dim("(sha256 %s…) %s saved verify.json" % (res["hardened_sha256"][:12], ui.DOT)))
        ui.next_hint("enable %s" % fid)
    else:
        failed = [k for k, v in g.items() if not v]
        print("  " + ui.bad(ui.bold(ui.FAIL + " FAIL")) + "  gates failed: " + ", ".join(failed) +
              ui.dim("  %s saved verify.json" % ui.DOT))
        ui.next_hint("verify %s   (after fixing .ras/hardens/%s/hardened.py)" % (fid, fid))


def _k(key: str) -> str:
    return key if len(key) <= 60 else key[:57] + "…"


def _explain(h: dict, o: dict | None = None) -> None:
    print("    hardened: %s %s" % (h["class"], ui.dim("(%s, %s)" % (h["exc"] or h["result"], ui.secs(h["latency"])))))
    if h.get("note"):
        print("    " + ui.warn(h["note"]))
    if h.get("miss"):
        print("    " + ui.dim(h["miss"]))
    print("    expected: %s" % ui.dim(str(h["expected"])))
    if o:
        print("    original: %s %s" % (o["class"], ui.dim("(%s, %s, %d dup)" % (o["exc"] or o["result"], ui.secs(o["latency"]), o["dups"]))))
    if h.get("dups"):
        print("    duplicate side effects: %d" % h["dups"])
