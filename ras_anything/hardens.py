"""Which hardened function a command means (.ras/hardens/<id>/). Shared by verify and enable; lives on its own so
enable doesn't import verify (that import closed a cycle diagnose -> enable -> verify -> diagnose)."""

from __future__ import annotations

from pathlib import Path

from . import lang
from .paths import UserError, load_json


def resolve_id(project: Path, fid: str | None) -> str:
    base = project / ".ras" / "hardens"
    have = sorted(p.name for p in base.iterdir() if (p / "meta.json").exists()) if base.is_dir() else []
    if fid:
        if fid not in have:
            raise UserError("%s hasn't been hardened yet" % fid, "known: " + (", ".join(have) or "none"),
                            "harden %s" % fid)
        return fid
    if not have:
        raise UserError("nothing hardened yet", "verify checks the hardened code in .ras/hardens/<id>/", "harden")
    if len(have) > 1:
        def changed(h):
            a = lang.by_name((load_json(base / h / "meta.json") or {}).get("lang"))
            f = base / h / a.hardened_file(load_json(base / h / "meta.json") or {})
            return f.stat().st_mtime if f.exists() else 0
        return max(have, key=changed)
    return have[0]
