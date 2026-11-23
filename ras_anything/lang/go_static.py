"""Static Go AST inspection, instrumentation, and build-cache helpers via go_agent/asttool."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..paths import UserError

GO_AGENT_DIR = Path(__file__).resolve().parent / "go_agent"
ASTTOOL_SRC = GO_AGENT_DIR / "asttool" / "main.go"
AGENT_SRC = GO_AGENT_DIR / "rasagent" / "agent.go"
RUNTIME_SRC = GO_AGENT_DIR / "rasruntime" / "runtime.go"

_INSPECT_CACHE: dict[tuple[str, str], dict] = {}


def go_exe() -> str:
    exe = shutil.which("go")
    if not exe:
        raise UserError("go isn't installed", "install Go 1.22+ to scan or harden Go apps", "doctor")
    return exe


def _cache_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "ras-go-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def asttool_bin() -> Path:
    src_hash = hashlib.sha256(ASTTOOL_SRC.read_bytes()).hexdigest()[:16]
    bin_path = _cache_dir() / f"asttool-{src_hash}"
    if bin_path.exists():
        return bin_path
    tmp = _cache_dir() / f"asttool-{src_hash}.{os.getpid()}.tmp"
    p = subprocess.run([go_exe(), "build", "-o", str(tmp), str(ASTTOOL_SRC)],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise UserError("couldn't build Go AST helper", (p.stderr or p.stdout).strip()[-500:], "doctor")
    tmp.replace(bin_path)
    return bin_path


def _tree_sig(root: Path) -> str:
    parts = []
    for p in sorted(root.rglob("*.go")):
        if any(seg in (".git", ".ras", "vendor", "node_modules") or seg.startswith("_ras_") for seg in p.parts):
            continue
        try:
            st = p.stat()
            parts.append(f"{p}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            pass
    gm = root / "go.mod"
    if gm.is_file():
        st = gm.stat()
        parts.append(f"go.mod:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def inspect(root: Path) -> dict:
    root = Path(root).resolve()
    sig = _tree_sig(root)
    key = (str(root), sig)
    if key in _INSPECT_CACHE:
        return _INSPECT_CACHE[key]
    p = subprocess.run([str(asttool_bin()), "inspect", str(root)],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise UserError("couldn't inspect Go project", (p.stderr or p.stdout).strip()[-500:], "doctor")
    data = json.loads(p.stdout)
    _INSPECT_CACHE[key] = data
    return data


def find_fn(root: Path, fid: str | None = None, file: str | None = None, qualname: str | None = None) -> dict | None:
    info = inspect(root)
    for fn in info.get("functions", []):
        if fid and fn["id"] == fid:
            return fn
        if file and qualname and fn["file"] == file and fn["qualname"] == qualname:
            return fn
    return None


def extract_hardened(hardened_path: Path, target_name: str, target_pkg: str) -> dict:
    p = subprocess.run([str(asttool_bin()), "extract-hardened", str(hardened_path), target_name, target_pkg],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise UserError("couldn't parse %s" % hardened_path.name, (p.stderr or p.stdout).strip()[-500:], "verify")
    return json.loads(p.stdout)


def copy_project(src_root: Path, dst_work: Path) -> None:
    def ignore(directory, names):
        out = set()
        for n in names:
            if n in (".git", ".ras", "vendor", "node_modules", "_ras_agent", "_ras_verify"):
                out.add(n)
        return out

    shutil.copytree(src_root, dst_work, dirs_exist_ok=True, ignore=ignore)


def _hash_work(work: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(work.rglob("*")):
        if p.is_file() and p.suffix in (".go", ".mod", ".sum"):
            h.update(str(p.relative_to(work)).encode())
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    return h.hexdigest()[:24]


def build_instrumented(root: Path, mode: str, spec: dict, pkg_target: str, scratch: Path,
                       ensure_runtime_pkg_dir: str | None = None) -> Path:
    work = scratch / "go_work"
    copy_project(root, work)
    ag_dir = work / "_ras_agent"
    ag_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(AGENT_SRC, ag_dir / "agent.go")

    if ensure_runtime_pkg_dir is not None:
        rt_dir = work / ensure_runtime_pkg_dir / "_ras_hardened"
        if not (rt_dir / "runtime.go").exists():
            rt_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(RUNTIME_SRC, rt_dir / "runtime.go")

    spec_full = {**spec, "work_dir": str(work), "mode": mode}
    spec_path = scratch / "instrument_spec.json"
    spec_path.write_text(json.dumps(spec_full), encoding="utf-8")

    p = subprocess.run([str(asttool_bin()), "instrument", str(spec_path)],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise UserError("couldn't instrument Go code", (p.stderr or p.stdout).strip()[-600:], "doctor")

    build_hash = _hash_work(work)
    bin_path = _cache_dir() / f"bin-{build_hash}"
    if bin_path.exists():
        return bin_path

    tmp = _cache_dir() / f"bin-{build_hash}.{os.getpid()}.tmp"
    bp = subprocess.run([go_exe(), "build", "-o", str(tmp), pkg_target],
                        cwd=str(work), capture_output=True, text=True, timeout=180)
    if bp.returncode != 0:
        msg = (bp.stderr or bp.stdout).strip()[-1200:]
        if mode == "verify":
            raise RuntimeError(msg)
        raise UserError("Go build failed after instrumentation", msg, "doctor")
    tmp.replace(bin_path)
    return bin_path


_IMPORT_PATH_RE = re.compile(r'^\s*(?:[\w.]+\s+)?"([^"]+)"', re.M)


def existing_imports(text: str) -> set[str]:
    return set(_IMPORT_PATH_RE.findall(text))


def build_import_patch(text: str, additions: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Return a unique (old, new) patch adding missing imports cleanly."""
    have = existing_imports(text)
    missing = [(al, p) for al, p in additions if p not in have]
    if not missing:
        return None
    ins_lines = "\n".join(f'\t{al} "{p}"' if al else f'\t"{p}"' for al, p in missing)

    m_block = re.search(r'^import\s*\((.*?)\n\)', text, re.M | re.S)
    if m_block:
        old = m_block.group(0)
        new = old[:-1] + ins_lines + "\n)"
        if text.count(old) == 1:
            return old, new

    m_single = re.search(r'^import\s+(?:[\w.]+\s+)?"[^"]+"', text, re.M)
    if m_single:
        old = m_single.group(0)
        inner = old[len("import"):].strip()
        new = f"import (\n\t{inner}\n{ins_lines}\n)"
        if text.count(old) == 1:
            return old, new

    m_pkg = re.search(r'^package\s+\w+[^\n]*', text, re.M)
    if m_pkg:
        old = m_pkg.group(0)
        new = f"{old}\n\nimport (\n{ins_lines}\n)"
        if text.count(old) == 1:
            return old, new
    return None
