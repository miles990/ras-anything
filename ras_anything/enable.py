"""enable / disable: swap the hardened function in with one marked block, and take it out byte for byte."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

from . import static, ui
from .paths import (UserError, find_project, harden_dir, load_json, plain_path, rel_to_project, safe_name, save_json,
                    sha256_file, write_file)
from .verify import resolve_id


def markers(fid: str) -> tuple[str, str]:
    return "# >>> ras-anything:%s >>>" % fid, "# <<< ras-anything:%s <<<" % fid


def block(fid: str, meta: dict, copy_rel: str, vres: dict) -> str:
    start, end = markers(fid)
    qual = meta["qualname"]
    if "." in qual:
        cls, name = qual.split(".", 1)
        bind = "    %s.%s = staticmethod(_ras_fn)" % (cls, name)
    else:
        name = qual
        bind = "    %s = _ras_fn" % name
    return f'''{start}
# Hardened by ras-anything; verified {vres.get("verified_at", "?")[:10]} on {vres["happy"]["total"]} recorded calls
# (availability under faults {100 * vres["original"]["availability"]:.1f}% -> {100 * vres["hardened"]["availability"]:.1f}%).
# Undo: `ras-anything disable {fid}`. RAS_ANYTHING_DISABLE=1 runs the original.
def _ras_anything_load():
    import importlib.util, os, sys
    if os.environ.get("RAS_ANYTHING_DISABLE") == "1":
        return None
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            import ras_anything.runtime  # noqa: F401
        except ImportError:  # not installed here: use the copy enable put next to the hardened code
            import types
            rt = importlib.util.spec_from_file_location("ras_anything.runtime", os.path.join(here, "_ras_hardened", "_runtime.py"))
            sys.modules.setdefault("ras_anything", types.ModuleType("ras_anything"))
            sys.modules["ras_anything.runtime"] = m = importlib.util.module_from_spec(rt)
            rt.loader.exec_module(m)
        path = os.path.join(here, {copy_rel!r})
        spec = importlib.util.spec_from_file_location("_ras_hardened.{safe_name(fid)}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, {name!r})
    except Exception as e:  # keep the app running on the original, loudly
        sys.stderr.write("ras-anything: hardened {fid} not loaded, running the original: %r\\n" % (e,))
        return None


_ras_fn = _ras_anything_load()
if _ras_fn is not None:
{bind}
del _ras_fn, _ras_anything_load
{end}
'''


COPY_DIR = "_ras_hardened"


def places(project: Path, module_file: str, fid: str) -> tuple[Path, Path, Path, Path]:
    """The four paths a swap touches (module, copies' folder, hardened copy, runtime copy), all checked
    to be plain files/folders inside the project."""
    src = plain_path(module_file, project, "the module")
    folder = plain_path(src.parent / COPY_DIR, project, "the folder for hardened copies", "dir", must_exist=False)
    copy = plain_path(folder / (safe_name(fid) + ".py"), project, "the hardened copy", must_exist=False)
    rt = plain_path(folder / "_runtime.py", project, "the runtime copy", must_exist=False)
    return src, folder, copy, rt


def enable(fid: str | None = None, quiet: bool = False) -> dict:
    project = find_project()
    fid = resolve_id(project, fid)
    d = harden_dir(project, fid)
    hp = plain_path(d / "hardened.py", project, "the hardened code")
    meta = load_json(plain_path(d / "meta.json", project, "the recording"))
    vres = load_json(plain_path(d / "verify.json", project, "the verify result", must_exist=False))
    cur = sha256_file(hp)
    if not vres:
        raise UserError("%s hasn't been verified" % fid, "enable only swaps in code that passed verify", "verify %s" % fid)
    if vres.get("hardened_sha256") != cur:
        raise UserError("hardened.py changed since it was verified", "the verified version is not what would be enabled",
                        "verify %s" % fid)
    if not vres.get("passed"):
        failed = [k for k, v in (vres.get("gates") or {}).items() if not v] or ["replay"]
        raise UserError("%s didn't pass verify (%s)" % (fid, ", ".join(failed)), "enable is refused until every gate passes",
                        "verify %s" % fid)
    state_path = plain_path(".ras/state.json", project, "the state file", must_exist=False)
    state = load_json(state_path, {"enabled": {}})
    if fid in state["enabled"]:
        if not quiet:
            ui.headline("enable", fid)
            print("  %s already enabled" % ui.good(ui.OK))
            ui.next_hint("bench")
        return state["enabled"][fid]
    src_path, folder, copy_abs, rt_abs = places(project, meta["file"], fid)
    raw = src_path.read_bytes()
    sha_before = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    blk = block(fid, meta, "%s/%s" % (COPY_DIR, copy_abs.name), vres)
    if eol == "\r\n":
        blk = blk.replace("\n", "\r\n")
    fi = static.FileInfo(str(src_path))
    if fi.has_main_guard:  # must bind before the entry point runs
        lines = text.splitlines(keepends=True)
        at = sum(len(ln) for ln in lines[: fi.main_guard_line - 1])
        insert = blk + eol + eol
    else:
        at = len(text)
        insert = (eol if text.endswith(eol) else eol + eol) + eol + blk
    new = (text[:at] + insert + text[at:]).encode("utf-8")
    rel_src = rel_to_project(src_path, project)
    orig_sha = next((v.get("file_sha_before") or v["sha_before"] for v in state["enabled"].values()
                     if v["file"] == rel_src), sha_before)
    entry = {"file": rel_src, "offset": at, "inserted": insert, "sha_before": sha_before,
             "file_sha_before": orig_sha, "sha_after": hashlib.sha256(new).hexdigest(), "hardened_sha256": cur,
             "copy": rel_to_project(copy_abs, project), "enabled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    state["enabled"][fid] = entry
    folder.mkdir(exist_ok=True)
    write_file(copy_abs, hp.read_bytes())
    write_file(rt_abs, Path(__file__).with_name("runtime.py").read_bytes())
    write_file(src_path, new)
    save_json(state_path, state)
    if not quiet:
        ui.headline("enable", fid)
        print("  %s %s now runs the hardened version %s" % (ui.good(ui.OK), fid, ui.dim("(one marked block in %s)" % os.path.relpath(src_path))))
        print("  %s copied hardened code to %s" % (ui.good(ui.OK), os.path.relpath(copy_abs)))
        print(ui.dim("  undo any time: ras-anything disable %s · one run on the original: RAS_ANYTHING_DISABLE=1" % fid))
        ui.next_hint("bench -- " + meta["display_cmd"])
    return entry


def check_entry(project: Path, fid: str, e: dict) -> dict:
    """Everything disable needs for one state.json entry, checked; touches nothing."""
    broken = UserError("the state.json entry for %s is damaged" % fid, "disable removes exactly what enable wrote, and "
                       "nothing was changed; take the marked block out by hand or restore .ras/state.json", "status")
    if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("file", "inserted", "copy", "sha_before")):
        raise broken
    src, folder, copy, rt = places(project, e["file"], fid)
    if Path(os.path.normpath(project.resolve() / e["copy"])) != copy:
        raise broken
    start, end = markers(fid)
    inserted = e["inserted"].strip()
    if not (inserted.startswith(start) and inserted.endswith(end)):
        raise broken
    text = src.read_bytes().decode("utf-8")
    exact = text.find(e["inserted"])
    i, j = text.find(start), text.find(end)
    if exact < 0 and (i < 0 or j < i):
        raise UserError("can't find the ras-anything block for %s in %s" % (fid, e["file"]), "was it removed by hand? Nothing "
                        "was changed", "status")
    return {"src": src, "folder": folder, "copy": copy, "text": text, "exact": exact, "i": i, "j": j, "end": end}


def disable(fid: str | None = None, quiet: bool = False) -> dict:
    project = find_project()
    state_path = plain_path(".ras/state.json", project, "the state file", must_exist=False)
    state = load_json(state_path, {"enabled": {}})
    if not state["enabled"]:
        raise UserError("nothing is enabled", "", "status")
    if fid is None:
        if len(state["enabled"]) > 1:
            raise UserError("several functions are enabled", "say which: " + ", ".join(state["enabled"]), "status")
        fid = next(iter(state["enabled"]))
    if fid not in state["enabled"]:
        raise UserError("%s isn't enabled" % fid, "enabled: " + ", ".join(state["enabled"]), "status")
    checked = {k: check_entry(project, k, v) for k, v in state["enabled"].items()}  # all of them, before any change
    e, c = state["enabled"][fid], checked[fid]
    p, text, exact = c["src"], c["text"], c["exact"]
    if exact >= 0:
        new = text[:exact] + text[exact + len(e["inserted"]):]
    else:  # the file was edited around the block: remove the block between its markers
        new = text[:c["i"]] + text[c["j"] + len(c["end"]) + 1:]
    others_same = [k for k, v in checked.items() if k != fid and v["src"] == p]
    orig_sha = e.get("file_sha_before") or e["sha_before"]
    data = new.encode("utf-8")
    restored = (hashlib.sha256(data).hexdigest() == orig_sha) if not others_same else (exact >= 0)
    write_file(p, data)
    try:
        os.unlink(c["copy"])
        if not any(x for x in c["folder"].iterdir() if x.name not in ("__pycache__", "_runtime.py")):
            shutil.rmtree(c["folder"])
    except OSError:
        pass
    del state["enabled"][fid]
    save_json(state_path, state)
    if not quiet:
        ui.headline("disable", fid)
        if others_same and exact >= 0:
            print("  %s block removed from %s %s" % (ui.good(ui.OK), os.path.relpath(p), ui.dim("(%d other hardened function(s) still enabled)" % len(others_same))))
        elif restored:
            print("  %s %s restored byte for byte %s" % (ui.good(ui.OK), os.path.relpath(p), ui.dim("(sha256 %s…)" % orig_sha[:12])))
        else:
            print("  %s block removed, but %s differs from before enable (edited since?)" % (ui.warn(ui.WARN), os.path.relpath(p)))
        ui.next_hint("status")
    return {"id": fid, "restored": restored}
