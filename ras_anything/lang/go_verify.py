"""Replay worker launcher for Go verify: compiles (with cache) and runs in-process Go RunVerify."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from . import go_static as static


def _write_fatal(out_path: Path, stage: str, message: str) -> None:
    out_path.write_text(json.dumps({
        "protocol": 1,
        "fatal": {"stage": stage, "message": message},
    }), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        sys.stderr.write("usage: python -m ras_anything.lang.go_verify <job.json>\n")
        return 2
    job_path = Path(args[0])
    job = json.loads(job_path.read_text(encoding="utf-8"))
    project = Path(job["project"]).resolve()
    meta = job["meta"]
    out_path = Path(job["out"])

    info = static.inspect(project)
    mod = info.get("module") or project.name
    fn = static.find_fn(project, fid=meta["id"])
    pkg_dir = fn["pkg_dir"] if fn else meta.get("pkg_dir", ".")
    pkg_name = fn["pkg_name"] if fn else meta.get("pkg_name", "main")

    rt_imp = f"{mod}/_ras_hardened" if pkg_dir in ("", ".") else f"{mod}/{pkg_dir}/_ras_hardened"
    pkg_target = "./" if pkg_name == "main" else "./_ras_verify"

    spec = {
        "module": mod,
        "agent_import": f"{mod}/_ras_agent",
        "runtime_import": rt_imp,
        "target_fid": meta["id"],
        "hardened_file": str(Path(job["hardened"]).resolve()),
    }

    with tempfile.TemporaryDirectory(prefix="ras-goverify-") as tmp:
        try:
            bin_path = static.build_instrumented(
                project, "verify", spec, pkg_target, Path(tmp),
                ensure_runtime_pkg_dir=pkg_dir if pkg_dir not in ("", ".") else ".",
            )
        except Exception as e:
            _write_fatal(out_path, "compile hardened code", str(e))
            return 0

    p = subprocess.run([str(bin_path), str(job_path)], capture_output=True, text=True, timeout=600)
    if p.returncode != 0 and not out_path.exists():
        _write_fatal(out_path, "replay", (p.stderr or p.stdout).strip()[-1500:] or f"exit {p.returncode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
