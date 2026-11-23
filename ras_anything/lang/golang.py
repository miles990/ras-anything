"""Go: everything ras-anything knows about Go modules, AST instrumentation, and hardened wrappers."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .. import signals
from ..paths import UserError, harden_dir, resolve_in_project, safe_name
from . import go_static as static
from .base import LangAdapter, pattern_list, program_stem


def is_go(program: str) -> bool:
    return program_stem(program) == "go"


def _parse_go_run(cmd: list[str]) -> tuple[str, list[str]]:
    """Split `go run [build-flags] <pkg> [app-args...]` into (pkg, app_args)."""
    args = cmd[2:] if len(cmd) >= 2 and cmd[1] == "run" else cmd[1:]
    takes_val = {"-C", "-mod", "-modfile", "-overlay", "-p", "-pkgdir", "-tags", "-exec", "-gcflags", "-ldflags"}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a in takes_val:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, args[i + 1:]
    if i < len(args):
        return args[i], args[i + 1:]
    return ".", []


def _go_version(exe: str) -> tuple[tuple[int, int] | None, str]:
    try:
        p = subprocess.run([exe, "version"], capture_output=True, text=True, timeout=15)
        txt = (p.stdout or "").strip()
        m = re.search(r"go(\d+)\.(\d+)", txt)
        if m:
            return (int(m.group(1)), int(m.group(2))), txt
        return None, txt or f"exit {p.returncode}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)


class GoAdapter(LangAdapter):
    name = "go"
    label = "Go"
    launchers = "go run . / ./cmd/app (with any flags and arguments)"
    example = "go run ."
    boundaries = "net/http (DefaultTransport / Client), os/exec"
    fence = "go"
    hardened_name = "hardened.go"
    private_env = ()
    bug_exceptions = frozenset({"runtime.TypeAssertionError", "runtime.boundsError", "panic"})

    # ---------------------------------------------------------------- commands

    def detect(self, cmd: list[str]) -> bool:
        return len(cmd) >= 2 and is_go(cmd[0]) and cmd[1] == "run"

    def resolve(self, cmd: list[str], cwd: Path) -> list[str]:
        exe = shutil.which(cmd[0]) if "/" not in cmd[0] else cmd[0]
        if not exe or not os.path.exists(exe):
            return list(cmd)
        ver, raw = _go_version(exe)
        if ver is not None and ver < (1, 22):
            raise UserError(f"Go {ver[0]}.{ver[1]} is too old: ras-anything needs Go 1.22+",
                            raw, "doctor")
        if len(cmd) < 2 or cmd[1] != "run":
            raise UserError("expected `go run <pkg>`", "ras-anything instruments and builds Go source via `go run`",
                            "scan -- go run .")
        return list(cmd)

    def project_root(self, cmd: list[str], cwd: Path) -> Path:
        pkg, _ = _parse_go_run(cmd)
        folder = (cwd / pkg).resolve()
        if folder.is_file():
            folder = folder.parent
        top = cwd.resolve()
        for p in (folder, *folder.parents):
            if (p / "go.mod").is_file():
                return p
            if p == top:
                break
        return top

    def prepare(self, cfg: dict, scratch: Path) -> dict:
        return {"_go_scratch": str(scratch)}

    def attach(self, cmd: list[str], env: dict) -> tuple[list[str], dict]:
        cfg = json.loads(env.get("RAS_ANYTHING_CFG") or "{}")
        scratch_str = cfg.get("_go_scratch")
        pkg_target, app_args = _parse_go_run(cmd)
        if not scratch_str:
            return list(cmd), dict(env)
        root = Path(cfg["root"]).resolve()
        info = static.inspect(root)
        mod = info.get("module") or root.name
        mode = cfg["mode"]
        spec: dict = {
            "module": mod,
            "agent_import": f"{mod}/_ras_agent",
        }
        if mode == "record" and cfg.get("target"):
            spec["target_fid"] = cfg["target"]["id"]
        if mode == "bench":
            spec["bench_fids"] = [t["id"] for t in (cfg.get("targets") or [])]
        bin_path = static.build_instrumented(root, mode, spec, pkg_target, Path(scratch_str))
        return [str(bin_path), *app_args], dict(env)

    def interpreter(self, project: Path, meta: dict) -> str:
        return shutil.which("go") or "go"

    # ---------------------------------------------------------------- source code

    def _root_for_file(self, file: str) -> Path:
        p = Path(file).resolve()
        for d in (p.parent, *p.parents):
            if (d / "go.mod").is_file():
                return d
        return p.parent

    def describe(self, fn: dict) -> dict | None:
        fpath = fn.get("file") or ""
        if not fpath:
            return None
        root = self._root_for_file(fpath)
        try:
            rel = str(Path(fpath).resolve().relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = fpath.replace("\\", "/")
        info = static.find_fn(root, fid=fn.get("id"), file=rel, qualname=fn.get("qualname") or fn.get("name"))
        if info is None:
            return None
        sig_labels = {
            "no_timeout": "outbound call without timeout",
            "write_no_key": "non-idempotent request without Idempotency-Key",
            "no_retry": "no retry on transient errors",
            "swallow": "swallows dependency error",
        }
        sigs = {k: sig_labels.get(k, k) for k in (info.get("signals") or []) if k in signals.SIGNALS}
        return {
            "id": info["id"],
            "module": info["module"],
            "qualname": info["qualname"],
            "name": info["name"],
            "kind": "func",
            "async": False,
            "line": info["line"],
            "in_main": bool(info.get("is_main")),
            "signals": sigs,
            "static_risk": signals.risk(sigs),
            "refused": None,
            "arg_ok": bool(info.get("arg_ok")),
            "ret_ok": bool(info.get("ret_ok")),
        }

    def hardenability(self, d: dict) -> str | None:
        if d.get("in_main"):
            return "main() entrypoint (extract the dependency call into a helper function to harden it)"
        if d.get("arg_ok") is False:
            return "arguments can't be stored as JSON, so calls can't be recorded"
        if d.get("ret_ok") is False:
            return "return value can't be stored as JSON, so results can't be compared"
        if not d.get("boundary"):
            return "makes no dependency calls"
        return None

    def _loaded(self, fn: dict, project: Path) -> dict:
        info = static.find_fn(project, fid=fn.get("id"))
        if info is None:
            raise UserError(f"{fn.get('qualname') or fn.get('id')} isn't in {fn.get('file')} any more",
                            "the source changed since the scan", "scan -- go run .")
        return info

    def source(self, fn: dict, project: Path) -> str:
        return self._loaded(fn, project)["source"]

    def stub(self, fn: dict, project: Path) -> str:
        info = self._loaded(fn, project)
        mod = static.inspect(project).get("module") or project.name
        pkg_dir = info["pkg_dir"]
        rt_imp = f"{mod}/_ras_hardened" if pkg_dir in ("", ".") else f"{mod}/{pkg_dir}/_ras_hardened"
        src_file = resolve_in_project(info["file"], project).read_text(encoding="utf-8")
        # Keep imports from source file that appear in the function body, plus context/time/_ras_rt
        fn_src = info["source"]
        imports: list[tuple[str, str]] = []
        seen_paths: set[str] = set()
        for imp_path in sorted(static.existing_imports(src_file)):
            pkg_base = imp_path.rsplit("/", 1)[-1]
            if re.search(rf"\b{re.escape(pkg_base)}\.", fn_src):
                imports.append(("", imp_path))
                seen_paths.add(imp_path)
        for std in ("context", "time"):
            if std not in seen_paths:
                imports.append(("", std))
                seen_paths.add(std)
        imports.append(("_ras_rt", rt_imp))

        imp_lines = "\n".join(f'\t{al} "{p}"' if al else f'\t"{p}"' for al, p in imports)
        lines = [
            f"// Hardened {info['id']}, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`.",
            f"package {info['pkg_name']}",
            "",
            "import (",
            imp_lines,
            ")",
            "",
            "// Optional declared fallback returned when the dependency is down / budget spent:",
            "// var RAS_FALLBACK = ...",
            "",
            fn_src,
            "",
        ]
        return "\n".join(lines)

    def callers(self, fn: dict, root: Path, limit: int = 4) -> list[str]:
        out: list[str] = []
        name = fn["name"]
        pat = re.compile(rf"(?<![\w.])(?:[\w]+\.)?{re.escape(name)}\s*\(")
        defn = re.compile(rf"^\s*func\s+(?:\([^)]+\)\s*)?{re.escape(name)}\b")
        for p in sorted(Path(root).rglob("*.go")):
            if any(seg in (".git", ".ras", "vendor") or seg.startswith("_ras_") for seg in p.parts):
                continue
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, ln in enumerate(lines):
                if pat.search(ln) and not defn.search(ln):
                    lo, hi = max(0, i - 2), min(len(lines), i + 5)
                    rel = os.path.relpath(p, os.path.realpath(root))
                    out.append(f"// {rel}:{i + 1}\n" + "\n".join(lines[lo:hi]))
                    if len(out) >= limit:
                        return out
        return out

    def task_texts(self, fn: dict) -> dict:
        return {
            "kind": "Go function (package-level, compiled)",
            "timeouts": "Every boundary call needs a timeout (`context.WithTimeout` + `http.NewRequestWithContext`, `exec.CommandContext`)",
            "transient": "`net.Error` timeouts, `ECONNREFUSED`, HTTP 429/5xx, truncated JSON bodies, or subprocess exit 75",
            "raise": '`&ras_hardened.RasError{Message: "...", Cause: err, Dependency: "..."}`',
            "declare": "Declare `var RAS_FALLBACK = <value>` at package level in `hardened.go` to allow degraded fallback returns.",
            "swallow": "`if err != nil { return zero, nil }` or `_ = client.Do(req)`",
            "errors": "Log each give-up with `_ras_rt.LogEvent(\"ras.dependency.gave_up\", attrs)` (includes `trace_id`) and wrap causes with `&ras_hardened.RasError{...}`.",
            "equal": "`reflect.DeepEqual` / JSON structural equality",
            "timeout_arg": "`context.Context` with `context.WithTimeout`",
            "helpers": f"""## Helpers (`_ras_rt` = `package ras_hardened`, Go stdlib only)

Keep the package declaration (`package ...`) as in the stub (`hardened.go` compiles inside the target package).
- `_ras_rt.NewCircuitBreaker(name, threshold, cooldown)`: open circuit after consecutive failures; call `cb.Allow()`, `cb.Success()`, `cb.Failure()`.
- `_ras_rt.Retry(ctx, opts, fn)`: full-jitter exponential backoff honouring `Retry-After` and shared `ctx` deadline.
- `_ras_rt.IsTransient(err)`: transient failure classifier (network timeouts, refused, 429, 502/503/504).
- `_ras_rt.IdempotencyKey(parts...)`: deterministic key for `Idempotency-Key` header on `POST`/`PUT`/`PATCH`/`DELETE`.
- `_ras_rt.RasError`: structured error with `Problem()` (RFC 9457) and `trace_id`.
- `_ras_rt.LogEvent(event, attrs)`: structured OTel JSON line on stderr with `trace_id` (scrubbed).

Patterns configured by default:
{pattern_list(lambda k, v: f"{k}={v}")}
""",
        }

    # ---------------------------------------------------------------- record / replay

    def record_target(self, fn: dict, project: Path) -> dict:
        return {
            "id": fn["id"],
            "file": str(resolve_in_project(fn["file"], project)),
            "module": fn["module"],
            "qualname": fn["qualname"],
        }

    def record_meta(self, fn: dict, data: dict, project: Path) -> dict:
        info = self._loaded(fn, project)
        mod = static.inspect(project).get("module") or project.name
        return {
            "module_path": mod,
            "pkg_dir": info["pkg_dir"],
            "pkg_name": info["pkg_name"],
            "params": info["params"],
            "results": info["results"],
        }

    def refuse_hardened(self, path: Path) -> str | None:
        try:
            ext = static.extract_hardened(path, "_", "_")
        except UserError as e:
            return e.impact
        bad = ext.get("forbidden") or []
        if bad:
            return f"{path.name} imports forbidden package(s): {', '.join(bad)}"
        return None

    def worker_cmd(self, exe: str, job: Path) -> tuple[list[str], dict]:
        env = dict(os.environ)
        env.pop("RAS_ANYTHING_CFG", None)
        pkg_parent = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return [sys.executable, "-m", "ras_anything.lang.go_verify", str(job)], env

    # ---------------------------------------------------------------- enable / disable

    def markers(self, fid: str) -> tuple[str, str]:
        return f"// ras-anything:start {fid}", f"// ras-anything:end {fid}"

    def _hardened_path_for(self, src_path: Path, meta: dict) -> Path:
        for d in (src_path.parent, *src_path.parents):
            if (d / ".ras" / "hardens").is_dir():
                return harden_dir(d, meta["id"]) / self.hardened_name
        raise UserError(f"cannot locate .ras/hardens for {meta['id']}", "run from inside the project", "status")

    def _runtime_import(self, src_path: Path, meta: dict) -> str:
        root = self._root_for_file(str(src_path))
        mod = meta.get("module_path") or static.inspect(root).get("module") or root.name
        pkg_dir = meta.get("pkg_dir") or "."
        return f"{mod}/_ras_hardened" if pkg_dir in ("", ".") else f"{mod}/{pkg_dir}/_ras_hardened"

    def block(self, fid: str, meta: dict, copy_rel: str, vres: dict) -> str:
        start, end = self.markers(fid)
        src_path = self._root_for_file(meta["file"]) / meta["file"]
        hp = self._hardened_path_for(src_path, meta)
        ext = static.extract_hardened(hp, meta["name"], meta.get("pkg_name") or "main")
        safe_id = fid.replace(".", "_").replace("-", "_")

        decls = (ext.get("decls_src") or "").strip()
        hfn = (ext.get("hardened_fn_src") or "").strip()
        if ext.get("has_fallback"):
            fb_alias = f"_ras_fb_{safe_id}"
            decls = re.sub(r"\bRAS_FALLBACK\b", fb_alias, decls)
            hfn = re.sub(r"\bRAS_FALLBACK\b", fb_alias, hfn)

        params = meta.get("params") or []
        results = meta.get("results") or []
        p_sig = ", ".join(f"{p['name'] or f'_p{i}'} {p['type']}" for i, p in enumerate(params))
        call_args = ", ".join(p["name"] or f"_p{i}" for i, p in enumerate(params))
        non_ctx = [p["name"] or f"_p{i}" for i, p in enumerate(params) if p["type"] != "context.Context"]
        args_slice = "[]any{" + ", ".join(non_ctx) + "}"

        r_types = [r["type"] for r in results]
        has_err = bool(r_types and r_types[-1] == "error")
        val_types = r_types[:-1] if has_err else r_types
        r_sig = f"({', '.join(r_types)})" if len(r_types) > 1 else (r_types[0] if r_types else "")

        deps_lit = "[]_ras_rt.DepInfo{" + ", ".join(
            f'{{Name: {json.dumps(d.get("name", ""))}, Kind: {json.dumps(d.get("kind", "http"))}}}'
            for d in (meta.get("dependencies") or [])
        ) + "}"

        name = meta["name"]
        orig_name = f"_ras_orig_{name}"
        hard_name = f"_ras_hardened_{name}"

        wrap_lines = [f"func {name}({p_sig}) {r_sig}".rstrip() + " {"]
        wrap_lines.append(f'\tif _ras_rt.Disabled({json.dumps(fid)}) {{')
        if r_types:
            wrap_lines.append(f"\t\treturn {orig_name}({call_args})")
        else:
            wrap_lines.append(f"\t\t{orig_name}({call_args})")
            wrap_lines.append("\t\treturn")
        wrap_lines.append("\t}")

        if len(val_types) == 1 and has_err:
            wrap_lines.append(
                f"\treturn _ras_rt.ObserveCall({json.dumps(fid)}, {deps_lit}, {args_slice}, "
                f"func() ({val_types[0]}, error) {{ return {hard_name}({call_args}) }})"
            )
        elif len(val_types) == 0 and has_err:
            wrap_lines.append(
                f"\t_, _err := _ras_rt.ObserveCall({json.dumps(fid)}, {deps_lit}, {args_slice}, "
                f"func() (struct{{}}, error) {{ return struct{{}}{{}}, {hard_name}({call_args}) }})"
            )
            wrap_lines.append("\treturn _err")
        elif len(val_types) == 1 and not has_err:
            wrap_lines.append(
                f"\t_res, _ := _ras_rt.ObserveCall({json.dumps(fid)}, {deps_lit}, {args_slice}, "
                f"func() ({val_types[0]}, error) {{ return {hard_name}({call_args}), nil }})"
            )
            wrap_lines.append("\treturn _res")
        elif len(val_types) == 0 and not has_err:
            wrap_lines.append(
                f"\t_, _ = _ras_rt.ObserveCall({json.dumps(fid)}, {deps_lit}, {args_slice}, "
                f"func() (struct{{}}, error) {{ {hard_name}({call_args}); return struct{{}}{{}}, nil }})"
            )
        else:
            fields = "; ".join(f"F{i} {vt}" for i, vt in enumerate(val_types))
            ret_vars = ", ".join(f"_v{i}" for i in range(len(val_types)))
            struct_vals = ", ".join(f"_v{i}" for i in range(len(val_types)))
            if has_err:
                wrap_lines.append(
                    f"\t_pack, _err := _ras_rt.ObserveCall({json.dumps(fid)}, {deps_lit}, {args_slice}, "
                    f"func() (struct{{{fields}}}, error) {{ {ret_vars}, _e := {hard_name}({call_args}); "
                    f"return struct{{{fields}}}{{{struct_vals}}}, _e }})"
                )
                outs = ", ".join(f"_pack.F{i}" for i in range(len(val_types))) + ", _err"
                wrap_lines.append(f"\treturn {outs}")
            else:
                wrap_lines.append(
                    f"\t_pack, _ := _ras_rt.ObserveCall({json.dumps(fid)}, {deps_lit}, {args_slice}, "
                    f"func() (struct{{{fields}}}, error) {{ {ret_vars} := {hard_name}({call_args}); "
                    f"return struct{{{fields}}}{{{struct_vals}}}, nil }})"
                )
                outs = ", ".join(f"_pack.F{i}" for i in range(len(val_types)))
                wrap_lines.append(f"\treturn {outs}")
        wrap_lines.append("}")

        lines = [
            start,
            f"// Hardened by ras-anything; verified {vres.get('verified_at', '?')[:10]} on {vres['happy']['total']} recorded calls",
            f"// (availability under faults {100 * vres['original']['availability']:.1f}% -> {100 * vres['hardened']['availability']:.1f}%).",
            f"// Undo: `ras-anything disable {fid}`. RAS_ANYTHING_DISABLE=1 runs the original.",
        ]
        if decls:
            lines.extend(["", decls])
        lines.extend(["", hfn, "", *wrap_lines, end, ""])
        return "\n".join(lines)

    def insertion(self, path: Path, text: str, block: str, eol: str) -> tuple[int, str]:
        return len(text), (eol if text.endswith(eol) else eol + eol) + eol + block

    def patches(self, path: Path, text: str, meta: dict) -> list[tuple[str, str]]:
        name = meta["name"]
        m = re.search(rf"^func\s+{re.escape(name)}\s*\(", text, re.M)
        if not m:
            raise UserError(f"can't find func {name} in {path.name}",
                            "the source changed since it was recorded; nothing was changed", f"harden {meta['id']}")
        old_sig = m.group(0)
        new_sig = old_sig.replace(f"{name}", f"_ras_orig_{name}", 1)
        if text.count(old_sig) != 1 or new_sig in text:
            raise UserError(f"func {name} isn't unique in {path.name}",
                            "disable couldn't undo the change exactly; nothing was changed", "status")
        out: list[tuple[str, str]] = [(old_sig, new_sig)]

        hp = self._hardened_path_for(path, meta)
        ext = static.extract_hardened(hp, name, meta.get("pkg_name") or "main")
        additions = [(imp.get("alias") or "", imp["path"]) for imp in ext.get("imports", [])]
        additions.append(("_ras_rt", self._runtime_import(path, meta)))
        imp_patch = static.build_import_patch(text, additions)
        if imp_patch is not None:
            out.append(imp_patch)
        return out

    def valid_patch(self, old: str, new: str) -> bool:
        if re.match(r"^func\s+\w+\s*\($", old) and "_ras_orig_" in new:
            return True
        if (old.startswith("import") or old.startswith("package ")) and "_ras_rt" in new:
            return True
        return False

    def copy_name(self, fid: str, file: Path) -> str:
        # Must NOT end in .go, because enable places it in _ras_hardened/ alongside runtime.go (package ras_hardened).
        return safe_name(fid) + ".go.bak"

    def runtime_file(self) -> tuple[Path, str]:
        return static.RUNTIME_SRC, "runtime.go"

    def runtime_modules(self) -> list[tuple[Path, str]]:
        return []

    def lab_cmd(self, job: Path):
        return None

    def lab_params(self, patterns: dict) -> dict:
        return dict(patterns)

    def lab_line(self, line: str, patterns: dict) -> str:
        return line

    # ---------------------------------------------------------------- people

    def doctor(self) -> list[tuple[bool | None, str, str]]:
        exe = shutil.which("go")
        if not exe:
            return [(None, "go", "not found (only needed for Go apps)")]
        ver, raw = _go_version(exe)
        if ver is None:
            return [(None, "go", raw)]
        old = ver < (1, 22)
        return [(None if old else True, raw, "ras-anything needs Go 1.22+" if old else "ready (net/http, os/exec)")]
