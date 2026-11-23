"""enable / disable: swap the hardened function in with one marked block, and take it out byte for byte."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

from . import cassette, lang, ui
from .paths import (UserError, find_project, harden_dir, load_json, plain_path, rel_to_project, save_json, sha256_file,
                    write_file)
from .hardens import resolve_id


COPY_DIR = "_ras_hardened"
PATTERNS_DIR = "patterns"  # in COPY_DIR: the runtime's pattern modules


def places(adapter, project: Path, module_file: str, fid: str) -> tuple[Path, Path, Path, Path]:
    """The paths a swap touches (module, copies' folder, hardened copy, runtime copy; the runtime's pattern
    modules), all checked to be plain files/folders inside the project."""
    src = plain_path(module_file, project, "the module")
    folder = plain_path(src.parent / COPY_DIR, project, "the folder for hardened copies", "dir", must_exist=False)
    copy = plain_path(folder / adapter.copy_name(fid, src), project, "the hardened copy", must_exist=False)
    rt = plain_path(folder / adapter.runtime_file()[1], project, "the runtime copy", must_exist=False)
    pats = plain_path(folder / PATTERNS_DIR, project, "the folder for pattern modules", "dir", must_exist=False)
    for _, name in adapter.runtime_modules():
        plain_path(pats / name, project, "a pattern module", must_exist=False)
    return src, folder, copy, rt


def enable(fid: str | None = None, quiet: bool = False, canary: str | None = None, shadow: bool = False) -> dict:
    project = find_project()
    fid = resolve_id(project, fid)
    d = harden_dir(project, fid)
    meta = load_json(plain_path(d / "meta.json", project, "the recording"))
    adapter = lang.by_name(meta.get("lang"))
    hp = plain_path(d / adapter.hardened_file(meta), project, "the hardened code")
    vres = load_json(plain_path(d / "verify.json", project, "the verify result", must_exist=False))
    cur = sha256_file(hp)
    if not vres:
        raise UserError("%s hasn't been verified" % fid, "enable only swaps in code that passed verify", "verify %s" % fid)
    if vres.get("hardened_sha256") != cur:
        raise UserError("%s changed since it was verified" % hp.name, "the verified version is not what would be enabled",
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
    src_path, folder, copy_abs, rt_abs = places(adapter, project, meta["file"], fid)
    raw = src_path.read_bytes()
    sha_before = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    meta_blk = dict(meta)
    if "dependencies" not in meta_blk and (d / "calls.json").exists():
        try:
            meta_blk["dependencies"] = cassette.dependencies(cassette.load(d / "calls.json"), project)
        except Exception:
            meta_blk["dependencies"] = []
    blk = adapter.block(fid, meta_blk, "%s/%s" % (COPY_DIR, copy_abs.name), vres)
    if eol == "\r\n":
        blk = blk.replace("\n", "\r\n")
    patches = adapter.patches(src_path, text, meta)
    for old, changed in patches:
        text = text.replace(old, changed)
    at, insert = adapter.insertion(src_path, text, blk, eol)
    new = (text[:at] + insert + text[at:]).encode("utf-8")
    rel_src = rel_to_project(src_path, project)
    orig_sha = next((v.get("file_sha_before") or v["sha_before"] for v in state["enabled"].values()
                     if v["file"] == rel_src), sha_before)
    entry = {"lang": adapter.name, "file": rel_src, "offset": at, "inserted": insert, "sha_before": sha_before,
             "file_sha_before": orig_sha, "sha_after": hashlib.sha256(new).hexdigest(), "hardened_sha256": cur,
             "copy": rel_to_project(copy_abs, project), **({"patches": patches} if patches else {}), "enabled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    state["enabled"][fid] = entry
    folder.mkdir(exist_ok=True)
    write_file(copy_abs, hp.read_bytes())
    write_file(rt_abs, adapter.runtime_bytes(meta))
    modules = adapter.runtime_modules()
    if modules:
        (folder / PATTERNS_DIR).mkdir(exist_ok=True)
    for source, name in modules:
        write_file(folder / PATTERNS_DIR / name, source.read_bytes())
    write_file(src_path, new)
    save_json(state_path, state)
    if not quiet:
        ui.headline("enable", fid)
        print("  %s %s now runs the hardened version %s" % (ui.good(ui.OK), fid, ui.dim("(one marked block in %s)" % os.path.relpath(src_path))))
        print("  %s copied hardened code to %s" % (ui.good(ui.OK), os.path.relpath(copy_abs)))
        print(ui.dim("  undo any time: ras-anything disable %s · one run on the original: RAS_ANYTHING_DISABLE=1" % fid))
        ui.next_hint("bench -- " + meta["display_cmd"])
    if canary or shadow:
        _apply_rollout(fid, canary, shadow, quiet=quiet)
    return entry


def _apply_rollout(fid: str, canary: str | None, shadow: bool, quiet: bool = False) -> None:
    from . import flags, ui
    if shadow:
        flags.store().set_flag(flags.shadow_key(fid), True)
        if not quiet:
            print("  %s shadow on %s" % (ui.good(ui.OK), fid))
    if canary:
        parts = [int(x) for x in str(canary).split(",") if x.strip() != ""]
        pct = parts[0]
        flags.store().set_flag(flags.enable_key(fid), pct)
        plan = parts if len(parts) > 1 else [pct, 100]
        flags.store().set_flag(flags.enable_key(fid) + ".plan", ",".join(str(x) for x in plan))
        if not quiet:
            print("  %s canary %d%% (plan %s, sticky by key)" % (ui.good(ui.OK), pct, ",".join(map(str, plan))))


def check_entry(project: Path, fid: str, e: dict) -> dict:
    """Everything disable needs for one state.json entry, checked; touches nothing."""
    broken = UserError("the state.json entry for %s is damaged" % fid, "disable removes exactly what enable wrote, and "
                       "nothing was changed; take the marked block out by hand or restore .ras/state.json", "status")
    if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("file", "inserted", "copy", "sha_before")):
        raise broken
    adapter = lang.by_name(e.get("lang"))
    src, folder, copy, rt = places(adapter, project, e["file"], fid)
    if Path(os.path.normpath(project.resolve() / e["copy"])) != copy:
        raise broken
    start, end = adapter.markers(fid)
    inserted = e["inserted"].strip()
    if not (inserted.startswith(start) and inserted.endswith(end)):
        raise broken
    text = src.read_bytes().decode("utf-8")
    exact = text.find(e["inserted"])
    i, j = text.find(start), text.find(end)
    if exact < 0 and (i < 0 or j < i):
        raise UserError("can't find the ras-anything block for %s in %s" % (fid, e["file"]), "was it removed by hand? Nothing "
                        "was changed", "status")
    patches = e.get("patches", [])
    if not isinstance(patches, list) or not all(isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(x, str) for x in p)
                                                and adapter.valid_patch(*p) for p in patches):
        raise broken
    return {"src": src, "folder": folder, "copy": copy, "text": text, "exact": exact, "i": i, "j": j, "end": end,
            "runtime": rt.name, "patches": patches}


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
    for old, changed in c["patches"]:
        if new.count(changed) == 1:  # (else the line was edited since: leave it)
            new = new.replace(changed, old)
    others_same = [k for k, v in checked.items() if k != fid and v["src"] == p]
    orig_sha = e.get("file_sha_before") or e["sha_before"]
    data = new.encode("utf-8")
    restored = (hashlib.sha256(data).hexdigest() == orig_sha) if not others_same else (exact >= 0)
    write_file(p, data)
    try:
        os.unlink(c["copy"])
        if not any(x for x in c["folder"].iterdir() if x.name not in ("__pycache__", c["runtime"], PATTERNS_DIR)):
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
