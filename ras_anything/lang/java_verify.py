"""Replay worker for Java: compiles the module as `enable` would leave it, then runs _ras_agent.Verify in a JVM."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from ..paths import UserError, load_json, resolve_in_project, runnable_cmd
from . import java_static as static
from .java import JavaAdapter, _javac_opts, embedded, runtime_package, runtime_source


def original_text(project: Path, meta: dict) -> str:
    """The module without this function's own block (others stay)."""
    text = resolve_in_project(meta["file"], project).read_bytes().decode("utf-8")
    e = ((load_json(project / ".ras" / "state.json") or {}).get("enabled") or {}).get(meta["id"])
    if e and e["inserted"] in text:
        text = text.replace(e["inserted"], "", 1)
        for old, new in e.get("patches") or []:
            text = text.replace(new, old, 1)
    return text


def main(argv: list[str]) -> int:
    job_path = Path(argv[0])
    job = json.loads(job_path.read_text(encoding="utf-8"))
    out, project, meta = Path(job["out"]), Path(job["project"]).resolve(), job["meta"]

    def fatal(stage: str, message: str) -> int:
        out.write_text(json.dumps({"protocol": 1, "fatal": {"stage": stage, "message": message}}), encoding="utf-8")
        return 0

    a = JavaAdapter()
    java = argv[1]
    cwd = resolve_in_project(meta.get("cwd") or ".", project)
    c = static.Command(runnable_cmd(meta["cmd"], project))
    c.java = java
    file = resolve_in_project(meta["file"], project)
    root = static.source_root(file).resolve()
    rel = file.relative_to(root).as_posix()
    try:
        text = original_text(project, meta)
        eol = "\r\n" if "\r\n" in text else "\n"
        block, body_line = a.compose(project, meta, {})
        for old, new in a.patches(file, text, meta):
            text = text.replace(old, new)
        at, insert = a.insertion(file, text, block.replace("\n", eol), eol)
        first = text[:at].count("\n") + 1 + body_line  # line of the block's class, less one
        text = text[:at] + insert + text[at:]
        overlay = {rel: text}
        if not embedded(meta):
            folder = os.path.dirname(rel)
            overlay[(folder + "/" if folder else "") + "_ras_hardened/RasRt.java"] = runtime_source(meta.get("package") or "")
        cp = c.class_path(cwd)
        built = static.build(java, root, [rel], cp, "verify", (), _javac_opts(c), overlay)
    except UserError as e:
        return fatal("compile hardened code", "%s\n%s" % (e.what, e.impact))
    except static.BuildFailed as e:
        src = static.read(Path(job["hardened"]))
        start = src[: src.find("{", max(0, re.search(r"class\s+Hardened\b", src).end()))].count("\n") + 1

        def where(m):
            n = int(m.group(1)) - first
            return "hardened.java:%d:" % (start + n - 1) if n > 0 else m.group(0)
        return fatal("compile hardened code", re.sub(r"(?m)^%s:(\d+):" % re.escape(rel), where, str(e)))
    rt = (meta["binary"] + "$_Ras_" + meta["name"] + "$RasRt") if embedded(meta) else runtime_package(meta.get("package") or "") + ".RasRt"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(RAS_ANYTHING_VERIFY="1", RAS_ANYTHING_DISABLE="1")
    jvm = [x for x in c.jvm if x.startswith("-D") or x.startswith("--") or not x.startswith("-")]
    p = subprocess.run([java, "-XX:TieredStopAtLevel=1", "-XX:+UseSerialGC", *jvm, "-cp", os.pathsep.join([str(built / "classes"), str(static.tools(java)), cp]),
                        "_ras_agent.Verify", str(job_path), meta["binary"], meta["name"], rt], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=880)
    if not out.exists():
        return fatal("replay", (p.stderr or p.stdout).strip()[-1500:] or "exit %d" % p.returncode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
