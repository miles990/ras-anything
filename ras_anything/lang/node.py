"""Node.js: everything ras-anything knows about JavaScript/TypeScript apps, their launchers and source code.

The half that runs inside the app's process is node_agent/ (loaded with `node --require`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from .. import signals
from ..paths import UserError, load_local, resolve_in_project, safe_name
from . import node_static as static
from .base import LangAdapter, pattern_list, program_stem

AGENT_DIR = Path(__file__).resolve().parent / "node_agent"
MIN_VERSION = (22, 15)  # module.registerHooks and require(esm); Node 23 from 23.5
MIN_TEXT = "Node.js 22.15 or newer (23.5+ in the 23 line)"
TAKES_VALUE = {"-r", "--require", "--import", "--loader", "--experimental-loader", "-C", "--conditions", "--env-file",
               "--env-file-if-exists", "--title", "--icu-data-dir", "--openssl-config", "--disable-warning", "--input-type"}
EVAL = {"-e", "--eval", "-p", "--print", "-pe"}


def is_node(program: str) -> bool:
    return re.fullmatch(r"node(js)?(-?\d+(\.\d+)*)?", program_stem(program)) is not None


def entry(args: list[str]) -> str | None:
    """The script in `node [flags] script.js args`; None for `node -e code` or no script."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in EVAL:
            return None
        if a == "--":
            return args[i + 1] if i + 1 < len(args) else None
        if a in TAKES_VALUE:
            i += 2
            continue
        if not a.startswith("-"):
            return a
        i += 1
    return None


@lru_cache(maxsize=32)
def probe(exe: str) -> dict:
    """Version and features of a Node executable."""
    code = ("const m=require('node:module');console.log(JSON.stringify({version:process.versions.node,exec:process.execPath,"
            "hooks:typeof m.registerHooks==='function',esm:Boolean(process.features.require_module),"
            "ts:process.features.typescript||false}))")
    try:
        p = subprocess.run([exe, "-e", code], capture_output=True, text=True, timeout=60)
        return json.loads(p.stdout) if p.returncode == 0 else {"error": (p.stderr.strip().splitlines() or ["exit %d" % p.returncode])[-1]}
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return {"error": str(e)}


def too_old(info: dict) -> bool:
    return not (info.get("hooks") and info.get("esm"))


def camel(name: str) -> str:
    return re.sub(r"_(\w)", lambda m: m.group(1).upper(), name)


def js_param(name: str, value):
    """A pattern parameter as the Node runtime has it: seconds (*_s) are milliseconds (*Ms) there."""
    if name.endswith("_s") and not name.endswith("per_s"):
        return camel(name[:-2]) + "Ms", value * 1000
    return camel(name), value


EXAMPLE_ASYNC = """// one attempt; its time is already cut to what is left of the deadline
const once = async ({ signal, headers }) => (await raiseForStatus(await fetch(url, { signal, headers, method, body }))).json();

await shop.call(once);                                                                            // a read
await shop.call(once, { method: "POST", idempotencyKey: idempotencyKey("charge", order.id, cents) }); // a write"""
EXAMPLE_SYNC = """// one attempt; its time is already cut to what is left of the deadline
shop.callSync(({ timeoutMs }) => JSON.parse(execFileSync(cmd, args, { timeout: timeoutMs })), { method: "RUN" });"""


def is_ts(path: str) -> bool:
    return path.endswith((".ts", ".mts", ".cts"))


class NodeAdapter(LangAdapter):
    name = "node"
    label = "Node.js"
    launchers = "node app.js / .mjs / .cjs / .ts (with any node flags)"
    example = "node app.js"
    boundaries = "fetch, http(s).request/get, child_process"
    fence = "js"
    hardened_name = "hardened.mjs"
    private_env = ("NODE_OPTIONS", "NODE_CHANNEL_FD")
    bug_exceptions = frozenset({"TypeError", "ReferenceError", "SyntaxError", "RangeError", "EvalError", "URIError", "AssertionError"})

    # ---------------------------------------------------------------- commands

    def detect(self, cmd):
        return is_node(cmd[0])

    def resolve(self, cmd, cwd):
        prog = str(cwd / cmd[0]) if "/" in cmd[0] else shutil.which(cmd[0])
        if not prog or not os.path.exists(prog):
            return list(cmd)  # runner.run reports it
        info = probe(prog)
        if "error" not in info and too_old(info):
            raise UserError("Node.js %s is too old: ras-anything needs %s" % (info["version"], MIN_TEXT),
                            "its agent uses module.registerHooks and require() of ES modules", "doctor")
        script = entry(cmd[1:])
        if script and is_ts(script) and not info.get("ts") and not any(
                a in ("--experimental-strip-types", "--experimental-transform-types") for a in cmd[1:]):
            raise UserError("this Node.js (%s) doesn't run TypeScript by itself" % info.get("version", "?"),
                            "add --experimental-strip-types (Node 22.6+), or use Node 22.18+ / 24", "scan -- node --experimental-strip-types " + script)
        if script and script.endswith((".tsx", ".jsx")):
            raise UserError("%s: JSX isn't something Node runs by itself" % script, "build it first and scan the built file", "scan -- node dist/app.js")
        return list(cmd)

    def project_root(self, cmd, cwd):
        """The nearest folder with a package.json at or above the script (inside cwd), else the script's folder."""
        script = entry(cmd[1:])
        if script is None:
            return cwd
        folder = (cwd / script).resolve().parent
        top = cwd.resolve()
        if folder == top or not folder.is_relative_to(top):
            return folder
        for p in (folder, *folder.parents):
            if (p / "package.json").is_file() or p == top:
                return p
        return folder

    def prepare(self, cfg, scratch):
        """The instrumented sources of the project's modules, for the agent's loader hooks."""
        mode, files = cfg["mode"], {}
        targets: dict[str, set] = {}  # record: the one function; bench: the enabled ones, wrapped over their block
        for t in [cfg["target"]] if mode == "record" else cfg.get("targets") or []:
            targets.setdefault(os.path.realpath(t["file"]), set()).add(t["qualname"])
        root = cfg["root"]
        for path in static.project_files(root):
            fi = static.load(path)
            if fi is None or not fi.funcs:
                continue
            if mode == "record" and path not in targets:
                continue
            x = static.instrument(fi, root, targets.get(path) if mode == "record" else None,
                                  over=targets.get(path, set()) if mode == "bench" else set())
            if x:
                files[path] = x
        out = scratch / "node.json"
        out.write_text(json.dumps({"files": files}), encoding="utf-8")
        return {"node_spec": str(out)}

    def attach(self, cmd, env):
        return [cmd[0], "--require", str(AGENT_DIR / "agent.cjs"), *cmd[1:]], dict(env)

    def interpreter(self, project, meta):
        """The app's Node: the one that ran it here, else the command's own, else the one on PATH."""
        exe = load_local(project).get("executable")
        if exe and is_node(exe) and os.path.exists(exe):
            return exe
        prog = (meta.get("cmd") or [""])[0]
        if prog and is_node(prog) and "/" in prog and (project / prog).exists():
            return str(project / prog)
        return shutil.which(prog or "node") or shutil.which("node") or "node"

    # ---------------------------------------------------------------- source code

    def _find(self, file: str, qualname: str):
        fi = static.load(file)
        return fi, (fi.by_name.get(qualname) if fi else None)

    def describe(self, fn):
        fi, f = self._find(fn["file"], fn.get("qualname") or fn["name"])
        if f is None:
            return None
        found = fi.analyze(f)
        return {"id": "%s.%s" % (fn["module"], f.qualname), "module": fn["module"], "qualname": f.qualname, "name": f.name,
                "kind": f.kind, "async": f.is_async or bool(fn.get("promise")), "line": f.line, "in_main": False, "signals": found,
                "static_risk": signals.risk(found), "refused": fi.refusal(f), "hardened_ext": self._ext(fi)}

    def hardenability(self, d):
        if d.get("refused"):
            return d["refused"]
        if d.get("gen"):
            return "generator (not supported yet)"
        if d.get("arg_ok") is False:
            return "arguments can't be stored as JSON, so calls can't be recorded"
        if d.get("ret_ok") is False:
            return "return value can't be stored as JSON, so results can't be compared"
        if not d["boundary"]:
            return "makes no dependency calls"
        return None

    def _loaded(self, fn, project):
        path = str(resolve_in_project(fn["file"], project))
        fi, f = self._find(path, fn["qualname"])
        if f is None:
            raise UserError("%s isn't in %s any more" % (fn["qualname"], fn["file"]), "the source changed since the scan",
                            "scan -- node app.js")
        return fi, f

    def source(self, fn, project):
        fi, f = self._loaded(fn, project)
        parts = [fi.function_text(h) for h in fi.uses(f)["helpers"]] + [fi.function_text(f)]
        return "\n\n".join(parts)

    def stub(self, fn, project):
        fi, f = self._loaded(fn, project)
        uses = fi.uses(f)
        helpers = ["// not exported by the module, so this file has its own copy\n" + fi.function_text(h) for h in uses["helpers"]]
        rt = ["idempotencyKey", "isTransient", "logEvent", "Policy", "raiseForStatus", "RasError", "RejectedError"] + (["scope"] if uses["scope"] else [])
        imports = [fi.rebased(*rng) for rng in sorted(uses["imports"])]
        lines = ["// Hardened %s, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`." % fn["id"],
                 "// Paths are relative to _ras_hardened/ next to the module: that is where enable puts this file.", ""]
        if not fi.esm:
            lines += ['"use strict";']
        lines += imports
        lines += ["import { %s } from \"./_runtime.cjs\";" % ", ".join(sorted(rt, key=str.lower)) if fi.esm else
                  "const { %s } = require(\"./_runtime.cjs\");" % ", ".join(sorted(rt, key=str.lower)), ""]
        if uses["scope"]:
            lines += ["// module-level names the original uses (the module's own, live)",
                      "const { %s } = %s;" % (", ".join(uses["scope"]), "scope(%s)" % json.dumps(fn["id"]) + (" as any" if fi.ts else "")), ""]
        for rng in sorted(uses["types"].values()):
            lines += [re.sub(r"^export\s+", "", fi.rebased(*rng)), ""]
        lines += ["// What this function returns when it degrades on purpose (dependency down, budget spent).",
                  "// A value, or a function taking the same arguments (() => null declares null).",
                  "// null means no fallback: throw a RasError instead.",
                  "%sconst RAS_FALLBACK%s = null;" % ("export " if fi.esm else "", ": any" if fi.ts else ""), ""]
        for h in helpers:
            lines += [h, ""]
        text = fi.function_text(f)
        lines += ["export " + text if fi.esm else text, ""]
        if not fi.esm:
            lines += ["module.exports = { %s, RAS_FALLBACK };" % f.name, ""]
        return "\n".join(lines)

    def hardened_file(self, fn):
        ext = fn.get("hardened_ext")
        return "hardened" + (ext if ext in (".mjs", ".cjs", ".mts", ".cts") else ".mjs")

    def _ext(self, fi) -> str:
        return (".mts" if fi.esm else ".cts") if fi.ts else (".mjs" if fi.esm else ".cjs")

    def callers(self, fn, root, limit=4):
        out = []
        pat = re.compile(r"(?<![\w$.])(?:[\w$]+\.)?%s\s*\(" % re.escape(fn["name"]))
        for path in static.project_files(str(root)):
            try:
                lines = Path(path).read_text(errors="replace").splitlines()
            except OSError:
                continue
            for i, ln in enumerate(lines):
                if pat.search(ln) and not re.search(r"\bfunction\s*\*?\s*%s\b" % re.escape(fn["name"]), ln):
                    lo, hi = max(0, i - 2), min(len(lines), i + 5)
                    out.append("// %s:%d\n%s" % (os.path.relpath(path, os.path.realpath(root)), i + 1, "\n".join(lines[lo:hi])))
                    if len(out) >= limit:
                        return out
        return out

    def task_texts(self, fn):
        sync = not fn.get("async")
        return {
            "kind": ("async " if fn.get("async") else "") + "JavaScript function (a Node.js %s)" % (
                "ES module" if self.hardened_file(fn).endswith(("mjs", "mts")) else "CommonJS module"),
            "timeouts": "Every boundary call needs a timeout (`fetch(url, { signal })`,\n   `http.get(url, { signal })`, `execFile(cmd, args, { timeout })`, "
                        "`execFileSync(cmd, args, { timeout })`)",
            "transient": "EAGAIN, a truncated/unparsable body from a 2xx, a child process exiting 75 or killed by its timeout",
            "raise": '`throw new RasError("...", { cause: e, context: {...} })`',
            "declare": "Declare it as the export `RAS_FALLBACK` (a value, or a function taking the same arguments;\n"
                       "   to declare `null` or `undefined` as the fallback use `() => null`).",
            "swallow": "`catch { return null }`, `.catch(() => null)`",
            "errors": "Log each give-up with `logEvent(\"...\", { ...context })` (JSON line on stderr,\n"
                      "   with a trace id) and chain causes (`{ cause: e }`).",
            "equal": "`util.isDeepStrictEqual`",
            "timeout_arg": "a timeout (`signal` / `timeout`)",
            "helpers": """## Helpers (`./_runtime.cjs`, Node built-ins only; times are in milliseconds)

Use one `Policy` per dependency, at module level. It composes the patterns that earned their place by measurement
(ras-anything's docs/PATTERNS.md), in a fixed order:

```js
const shop = new Policy("shop-api", { deadline: { timeoutMs: 4700 }, attemptTimeout: { timeoutMs: 3400 },
  retry: { attempts: 4, retryOn: (e) => isTransient(e) || e instanceof SyntaxError } });

%(example)s
```

%(patterns)s

An option named after a pattern sets its parameters (an object) or switches it off (`false`). When the policy gives up
it throws the last error, or a `RejectedError` without a request (open circuit): catch both to degrade, log it, and
return the fallback or throw a `RasError`. Nested policies, `withTimeout` and `retry` inside share the deadline.

- `await retry(fn, {attempts = 3, baseMs = 100, capMs = 2000, timeoutMs, budgetMs, retryOn = isTransient})`: only the
  retry part, when a policy doesn't fit; calls `fn({attempt, signal})`; full-jitter backoff, honours Retry-After. `timeoutMs` bounds each attempt (pass `signal` on),
  `budgetMs` the whole (attempts are cut to what is left). Rethrows the last error unchanged.
- `retrySync(fn, {...same})`: for synchronous functions; `fn({attempt, timeoutMs})`, give `timeoutMs` to the call.
- `await raiseForStatus(res)`: fetch doesn't reject on 4xx/5xx. Returns `res` when status < 400, else throws an `HttpError`
  (`.status`, `.headers`, `.url`, `.body`), which `isTransient` and `retry` understand.
- `isTransient(err)`: the transient rule from point 3 (not unparsable bodies: add `SyntaxError` yourself when a 2xx body
  fails to parse, e.g. `retryOn: (e) => isTransient(e) || e instanceof SyntaxError`).
- `await withTimeout((signal) => ..., ms)`: for anything; rejects with a `TimeoutError`.
- `new RasError(message, {cause, context})`: structured error with a trace id. `logEvent(event, {...})`: one JSON line on stderr.
- `scope("<id>")`: the module-level names listed in the stub, live from the original module.

`%(file)s` must stay loadable with `require()`: no top-level `await`. Keep the imports' relative paths as they are in the
stub (they count from `_ras_hardened/`). Harness modules (`node:module`, `node:vm`, `node:worker_threads`, ...) are refused.
In a body that arrives cut in half, `JSON.parse` / `res.json()` throws a `SyntaxError`.%(more)s
""" % {"file": self.hardened_file(fn), "example": EXAMPLE_SYNC if sync else EXAMPLE_ASYNC,
       "patterns": pattern_list(lambda k, v: "%s: %s" % (js_param(k, v)[0], "%g" % js_param(k, v)[1]), camel),
       "more": "\nThis function is synchronous: it must stay synchronous." if sync else
               "\nA timed-out `fetch` keeps no promise pending: always pass the `signal`; a promise that never settles is a hang."},
        }

    # ---------------------------------------------------------------- record / replay

    def record_target(self, fn, project):
        return {"file": str(resolve_in_project(fn["file"], project)), "line": fn["first_line"], "name": fn["name"],
                "async": fn.get("async", False), "module": fn["module"], "qualname": fn["qualname"]}

    def record_meta(self, fn, data, project):
        fi, f = self._loaded(fn, project)
        return {"hardened_ext": self._ext(fi), "format": "module" if fi.esm else "commonjs", "accessor": f.accessor,
                "binding": f.binding, "scope": fi.uses(f)["scope"]}

    def refuse_hardened(self, path):
        bad = static.forbidden(path)
        if not bad:
            return None
        return "%s %s. Hardened code may use ./_runtime.cjs only: the rest of ras-anything is the harness that judges it." % (
            path.name, ", ".join(bad))

    def worker_cmd(self, exe, job):
        j = json.loads(job.read_text(encoding="utf-8"))
        meta, project = j["meta"], Path(j["project"])
        path = os.path.realpath(project / meta["file"])
        fi, f = self._find(path, meta["qualname"])
        j["node_files"] = {path: static.with_scope(fi, f, meta["id"])} if f else {}
        job.write_text(json.dumps(j), encoding="utf-8")
        env = dict(os.environ)
        for k in ("RAS_ANYTHING_CFG", "NODE_OPTIONS"):
            env.pop(k, None)
        flags = ["--experimental-strip-types"] if fi is not None and fi.ts and not probe(exe).get("ts") else []
        return [exe, *flags, str(AGENT_DIR / "worker.cjs"), str(job)], env

    # ---------------------------------------------------------------- enable

    def markers(self, fid):
        return "// >>> ras-anything:%s >>>" % fid, "// <<< ras-anything:%s <<<" % fid

    def block(self, fid, meta, copy_rel, vres):
        start, end = self.markers(fid)
        esm = meta["format"] == "module"
        acc, ts = meta["accessor"], meta["file"].endswith((".ts", ".mts", ".cts"))
        ignore = "    // @ts-ignore\n" if ts else ""
        lines = [start, "// Hardened by ras-anything; verified %s on %d recorded calls" % (vres.get("verified_at", "?")[:10], vres["happy"]["total"]),
                 "// (availability under faults %.1f%% -> %.1f%%)." % (100 * vres["original"]["availability"], 100 * vres["hardened"]["availability"]),
                 "// Undo: `ras-anything disable %s`. RAS_ANYTHING_DISABLE=1 runs the original." % fid]
        if meta["binding"] == "const":
            lines.append("// (its `const` above is `let` while this block is here; disable puts it back)")
        load = "process.getBuiltinModule(\"node:module\").createRequire(import.meta.url)" if esm else "require"
        lines += [
            "if (process.env.RAS_ANYTHING_DISABLE !== \"1\") {", "  try {",
            *([ignore.rstrip("\n")] if ts else []),
            "    const load = %s;" % load,
            *([ignore.rstrip("\n")] if ts else []),
            "    %s = load(\"./_ras_hardened/_runtime.cjs\").enable({" % acc,
            "      id: %s, name: %s, original: %s,%s" % (json.dumps(fid), json.dumps(meta["name"]), acc, "" if esm else " module,"),
            "      load: () => load(%s)," % json.dumps("./" + copy_rel),
            "      names: %s," % static.scope_object(meta["scope"]),
            "    });", "  } catch (e) { // keep the app running on the original, loudly",
            "    process.stderr.write(`ras-anything: hardened %s not loaded, running the original: ${e}\\n`);" % fid,
            "  }", "}", end, ""]
        return "\n".join(lines)

    def insertion(self, path, text, block, eol):
        return len(text), (eol if text.endswith(eol) else eol + eol) + eol + block

    def patches(self, path, text, meta):
        """`const f = async () => {}` can't be rebound: while enabled it is declared with `let`."""
        if meta["binding"] != "const":
            return []
        fi, f = self._find(str(path), meta["qualname"])
        at = static._const_at(fi, f) if f else None
        if at is None or f.binding != "const":
            raise UserError("can't find the declaration of %s in %s" % (meta["qualname"], path.name), "the source changed since it was "
                            "recorded; nothing was changed", "harden %s" % meta["id"])
        a = fi.src.rfind("\n", 0, at) + 1
        b = fi.src.find("\n", f.text[0])
        line = (fi.src[a:] if b < 0 else fi.src[a:b]).rstrip("\r")
        new = line[:at - a] + "let" + line[at - a + 5:]
        if text.count(line) != 1 or new in text:
            raise UserError("the line declaring %s isn't unique in %s" % (meta["qualname"], path.name), "disable couldn't undo the change "
                            "exactly; nothing was changed", "status")
        return [(line, new)]

    def valid_patch(self, old, new):
        return old.count("\n") == 0 and any(old[:m.start()] + "let" + old[m.end():] == new for m in re.finditer(r"\bconst\b", old))

    def copy_name(self, fid, file):
        fi = static.load(str(file))
        if fi is None:
            raise UserError("can't read %s" % Path(file).name, "it isn't a JavaScript/TypeScript file ras-anything understands; nothing was changed", "status")
        return safe_name(fid) + self._ext(fi)

    def runtime_file(self):
        return AGENT_DIR / "runtime.cjs", "_runtime.cjs"

    def runtime_modules(self):
        return [(p, p.name) for p in sorted((AGENT_DIR / "patterns").glob("*.cjs"))]

    def lab_cmd(self, job):
        exe = shutil.which("node")
        info = probe(exe) if exe else {"error": "not found"}
        if "error" in info or too_old(info):
            return None
        return [exe, str(AGENT_DIR / "lab.cjs"), str(job)], dict(os.environ)

    def lab_params(self, patterns):
        return {camel(k): {p: w for p, w in (js_param(*x) for x in v.items())} if isinstance(v, dict) else v for k, v in patterns.items()}

    def lab_line(self, line, patterns):
        return line.replace("{}", json.dumps(self.lab_params(patterns)))

    # ---------------------------------------------------------------- people

    def doctor(self):
        exe = shutil.which("node")
        if not exe:
            return [(None, "node", "not found (only needed for Node.js apps)")]
        info = probe(exe)
        if "error" in info:
            return [(None, "node", info["error"])]
        old = too_old(info)
        ts = {"strip": "TypeScript: yes", "transform": "TypeScript: yes"}.get(info.get("ts"), "TypeScript: with --experimental-strip-types")
        return [(None if old else True, "node " + info["version"], "ras-anything needs %s" % MIN_TEXT if old else ts)]
