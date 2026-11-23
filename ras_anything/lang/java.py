"""Java: everything ras-anything knows about Java programs. Sources are copied, instrumented and compiled with the
JDK's own compiler (java_static.py); the agent is in java_agent/."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

from .. import signals
from ..paths import UserError, find_project, harden_dir, load_json, resolve_in_project, runnable_cmd, safe_name
from . import java_static as static
from .base import LangAdapter, pattern_list, program_stem

_PATTERNS = {"fallback": "callCached(key, maxAge, fn)", "idempotency": "call(\"POST\", key, fn)", "deadline": "deadline(Duration)", "attempt_timeout": "attemptTimeout(Duration)", "retry": "attempts(n), backoff(base, cap)",
             "circuit_breaker": "breaker(threshold, cooldown)"}


class JavaAdapter(LangAdapter):
    name = "java"
    label = "Java"
    launchers = "java App.java / java -cp DIR Main / java -jar app.jar (JVM options and arguments are fine)"
    example = "java App.java"
    boundaries = "java.net.http.HttpClient, HttpURLConnection, ProcessBuilder / Runtime.exec"
    fence = "java"
    hardened_name = "hardened.java"
    private_env = ()
    bug_exceptions = frozenset({"NullPointerException", "ClassCastException", "IndexOutOfBoundsException", "ArrayIndexOutOfBoundsException",
                                "StringIndexOutOfBoundsException", "ArithmeticException", "UnsupportedOperationException", "StackOverflowError",
                                "NoClassDefFoundError", "ExceptionInInitializerError", "NegativeArraySizeException", "ArrayStoreException"})

    # ---------------------------------------------------------------- commands

    def detect(self, cmd: list[str]) -> bool:
        return bool(cmd) and program_stem(cmd[0]) == "java" and static.Command(cmd).main is not None

    def resolve(self, cmd: list[str], cwd: Path) -> list[str]:
        exe = shutil.which(cmd[0]) if os.sep not in cmd[0] else str(cwd / cmd[0])
        if not exe or not os.path.exists(exe):
            return list(cmd)
        ver, raw = static.version(exe)
        if ver is not None and ver < 17:
            raise UserError("Java %d is too old: ras-anything needs Java 17+" % ver, raw, "doctor")
        c = static.Command(cmd)
        if c.kind in ("module", "argfile"):
            raise UserError("can't follow `java %s`" % ("-m" if c.kind == "module" else c.main),
                            "start the main class from the class path instead: java -cp DIR Main", "scan -- java -cp DIR Main")
        c.main_file(cwd)
        static.javac_exe(exe)
        return [exe, *cmd[1:]]

    def project_root(self, cmd: list[str], cwd: Path) -> Path:
        top = cwd.resolve()
        root = static.source_root(static.Command(cmd).main_file(cwd))
        return top if root.is_relative_to(top) else root

    def prepare(self, cfg: dict, scratch: Path) -> dict:
        return {"java_scratch": str(scratch)}

    def attach(self, cmd: list[str], env: dict) -> tuple[list[str], dict]:
        env = dict(env)
        cfg = json.loads(env.pop("RAS_ANYTHING_CFG"))
        cwd, c = Path(cfg["cwd"]), static.Command(cmd)
        main = c.main_file(cwd)
        root = static.source_root(main)
        mode = cfg["mode"]
        targets = [cfg["target"]["id"]] if mode == "record" else [t["id"] for t in cfg.get("targets") or []] if mode == "bench" else []
        cp = c.class_path(cwd)
        try:
            built = static.build(c.java, root, [main.relative_to(root).as_posix()], cp, "wrap" if targets else "run", targets, _javac_opts(c))
        except static.BuildFailed as e:
            raise UserError("the app's sources don't compile", str(e)[-900:], "doctor")
        cfg["java_functions"] = str(built / "table.json")
        path = Path(cfg["java_scratch"]) / "java_cfg.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        full = os.pathsep.join([str(built / "classes"), str(static.tools(c.java)), cp])
        return [c.java, *c.jvm, "-Dras.anything.cfg=" + str(path), "-cp", full, c.main_class(cwd), *c.args], env

    def interpreter(self, project: Path, meta: dict) -> str:
        cmd = runnable_cmd(meta.get("cmd") or ["java"], project)
        return (cmd[0] if os.path.isabs(cmd[0]) else shutil.which(cmd[0])) or shutil.which("java") or "java"

    # ---------------------------------------------------------------- source code

    def describe(self, fn: dict) -> dict | None:
        if not fn.get("id"):
            return None
        sigs = {k: v for k, v in (fn.get("signals") or {}).items() if k in signals.SIGNALS}
        return {"id": fn["id"], "module": fn["module"], "qualname": fn["qualname"], "name": fn["name"], "kind": "static method" if fn.get("static") else "method",
                "async": False, "line": fn["line"], "in_main": bool(fn.get("main")), "signals": sigs, "static_risk": signals.risk(sigs),
                "refused": fn.get("refused"), "arg_bad": fn.get("arg_bad"), "ret_bad": fn.get("ret_bad")}

    def hardenability(self, d: dict) -> str | None:
        if d.get("in_main"):
            return "main() (move the dependency call into a static method of its own)"
        if d.get("refused"):
            return d["refused"]
        if not d.get("arg_ok"):
            return "arguments can't be stored as JSON (%s)" % d["arg_bad"]
        if not d.get("ret_ok"):
            return "the result can't be stored as JSON (%s)" % d["ret_bad"]
        if not d.get("boundary"):
            return "makes no dependency calls"
        return None

    def _command(self, project: Path) -> tuple[static.Command, Path]:
        scan = load_json(project / ".ras" / "scan.json") or {}
        cwd = resolve_in_project(scan.get("cwd") or ".", project)
        c = static.Command(runnable_cmd(scan.get("cmd") or ["java"], project))
        c.java = self.interpreter(project, scan)
        return c, cwd

    def _facts(self, fn: dict, project: Path) -> dict:
        c, cwd = self._command(project)
        file = resolve_in_project(fn["file"], project)
        root = static.source_root(file)
        try:
            fns = static.inspect(c.java, root, file.relative_to(root.resolve()).as_posix(), c.class_path(cwd) if c.main else str(cwd), _javac_opts(c))
        except static.BuildFailed as e:
            raise UserError("the app's sources don't compile", str(e)[-900:], "doctor")
        for f in fns:
            if f["id"] == fn["id"]:
                return f
        raise UserError("%s isn't in %s any more" % (fn["id"], fn["file"]), "the source changed since the scan", "scan -- java App.java")

    def source(self, fn: dict, project: Path) -> str:
        return self._facts(fn, project)["source"]

    def stub(self, fn: dict, project: Path) -> str:
        facts = self._facts(fn, project)
        text = static.read(resolve_in_project(fn["file"], project)).replace("\r\n", "\n")
        pkg = static.package_of(text)
        imports = re.findall(r"(?m)^\s*import\s[^;]+;", text)
        own = facts["owner"].rsplit(".", 1)[-1]
        body = "\n".join("    " + ln if ln and not facts["source"].startswith(" ") else ln for ln in facts["source"].split("\n"))
        return "\n".join(
            ["// Hardened %s, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`." % fn["id"],
             *(["package %s;" % pkg, ""] if pkg else []),
             *[i.strip() for i in imports],
             "import %s.RasRt;" % runtime_package(pkg), "",
             "// Compiled as a class inside %s: its private members and nested types are in scope by their plain names." % own,
             "final class Hardened {",
             "    // Optional declared fallback, returned when the dependency is down (a value, or a method with the same parameters):",
             "    // static final %s RAS_FALLBACK = ...;" % facts["returns"], "",
             body, "}", ""])

    def callers(self, fn: dict, root: Path, limit: int = 4) -> list[str]:
        out: list[str] = []
        pat = re.compile(r"(?<![\w$])%s\s*\(" % re.escape(fn["name"]))
        defn = re.compile(r"^\s*(?:(?:public|protected|private|static|final|synchronized)\s+)+[\w<>\[\], .?]+\s%s\s*\(" % re.escape(fn["name"]))
        for p in static.java_files(Path(root)):
            if "_ras_hardened" in p.parts:
                continue
            lines = static.read(p).splitlines()
            for i, ln in enumerate(static.strip("\n".join(lines)).split("\n")):
                if pat.search(ln) and not defn.search(ln):
                    out.append("// %s:%d\n%s" % (os.path.relpath(p, os.path.realpath(root)), i + 1, "\n".join(lines[max(0, i - 2): i + 5])))
                    if len(out) >= limit:
                        return out
        return out

    def task_texts(self, fn: dict) -> dict:
        return {
            "kind": "Java static method",
            "timeouts": "Every boundary call needs its own timeout (`HttpRequest.Builder.timeout`, `setConnectTimeout` + `setReadTimeout`, `Process.waitFor(n, unit)` then `destroyForcibly`)",
            "transient": "`HttpTimeoutException`, `SocketTimeoutException`, `ConnectException`, a 2xx body that doesn't parse (cut off), a process that times out, exits 75 or can't be forked (`error=11`)",
            "raise": "`throw new RasRt.RasError(message, cause, \"dependency\")`, or `RasRt.RasIOException` where callers catch IOException",
            "declare": "Declare `static final <type> RAS_FALLBACK = <value>;` (or a static method `RAS_FALLBACK` with the same parameters) in class `Hardened`.",
            "swallow": "`catch (Exception e) { return null; }`",
            "errors": "Log each give-up (`Policy` does; otherwise `RasRt.logEvent(\"ras.dependency.gave_up\", RasRt.map(\"ras.dependency\", name, \"ras.fault\", RasRt.faultClass(e)))`, "
                      "which carries the `trace_id`) and keep the cause. Log before returning RAS_FALLBACK.",
            "equal": "structural equality of the results as JSON",
            "timeout_arg": "a timeout",
            "helpers": """## Helpers (`RasRt`, standard library only)

Keep one `final class Hardened` in the file, with `static %s`; helper methods, constants and types go inside it
(they may use everything the original class can). No reflection, no raw sockets.

```java
private static final RasRt.Policy RATES = RasRt.policy("rates").deadline(Duration.ofMillis(4500)).attemptTimeout(Duration.ofMillis(3300)).attempts(4);

// a read: check() turns 4xx/5xx into HttpStatusException, parseJson() a cut-off body into BadBodyException; both are classified
Object body = RATES.call(a -> RasRt.parseJson(RasRt.check(HTTP.send(a.apply(HttpRequest.newBuilder(uri)).build(), BodyHandlers.ofString())).body()));
// a write: the key makes it repeatable; a.apply() sends a.headers() (Idempotency-Key) and sets a.timeout()
ORDERS.call("POST", RasRt.idempotencyKey("order", id), a -> ...);
// a process: killed at the timeout; exit 75 or killed = ProcessFailedException; check other codes and the output yourself
RasRt.Result r = LABELS.call(a -> RasRt.run(a, "label-tool", id));
```

- `Policy.call` retries transient errors only (inside the deadline, honouring Retry-After) and throws `RasError` (cause kept, logged) when it gives
  up; what isn't transient is rethrown as it is. A refused attempt costs no time, so four attempts ride out a short restart inside the same deadline;
  the retry budget and the breaker keep that from multiplying an outage. `alsoRetry(predicate)` adds your parser's own exceptions for cut-off bodies.
- **A parser that ignores the rest of a cut-off body returns wrong data**: check that the body is complete inside the call, and output of processes too.
- `RasRt.CircuitBreaker` (`allow/success/failure`), `isTransient(e)`, `faultClass(e)`, `logEvent(event, map(...))`, `RasError`/`RasIOException` (`problem()`,
  RFC 9457), `HttpStatusException.status`, `requireApproval(op, details)`, `health()`.

Patterns in `RasRt.Policy`, on by default:
%s
""" % (fn["name"], pattern_list(lambda k, v: "%s=%s" % (k, v), lambda n: n + (": " + _PATTERNS[n] if n in _PATTERNS else ""))),
        }

    # ---------------------------------------------------------------- record / replay

    def record_target(self, fn: dict, project: Path) -> dict:
        return {"id": fn["id"], "file": str(resolve_in_project(fn["file"], project)), "module": fn["module"], "qualname": fn["qualname"]}

    def record_meta(self, fn: dict, data: dict, project: Path) -> dict:
        f = next((x for x in data.get("functions") or [] if x.get("id") == fn["id"]), None) or self._facts(fn, project)
        return {"owner": f["owner"], "binary": f["binary"], "params": f["params"], "returns": f["returns"], "throws": f["throws"],
                "package": static.package_of(static.read(resolve_in_project(fn["file"], project)))}

    def refuse_hardened(self, path: Path) -> str | None:
        meta = load_json(path.parent / "meta.json") or {}
        res = static.tool(static.java_exe(), {"do": "hardened", "text": static.read(path), "name": meta.get("name", ""), "rename": "_Ras", "imports": []})
        if res.get("error"):
            return res["error"]
        if res["forbidden"]:
            return "%s uses %s: hardened code can't reach around the recorder" % (path.name, ", ".join(res["forbidden"]))
        return None

    def worker_cmd(self, exe: str, job: Path) -> tuple[list[str], dict]:
        env = dict(os.environ)
        env.pop("RAS_ANYTHING_CFG", None)
        parent = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return [sys.executable, "-m", "ras_anything.lang.java_verify", str(job), exe], env

    # ---------------------------------------------------------------- enable

    def markers(self, fid: str) -> tuple[str, str]:
        return "// ras-anything:start " + fid + ";", "// ras-anything:end " + fid + ";"

    def block(self, fid: str, meta: dict, copy_rel: str, vres: dict) -> str:
        return self.compose(find_project(), meta, vres)[0]

    def compose(self, project: Path, meta: dict, vres: dict) -> tuple[str, int]:
        """The marked block, and the line of the block its hardened class starts in."""
        fid, name = meta["id"], meta["name"]
        java = self.interpreter(project, meta)
        text = static.read(resolve_in_project(meta["file"], project))
        at = static.tool(java, {"do": "place", "text": text, "owner": meta["owner"], "name": name})
        if at.get("error"):
            raise UserError("%s doesn't parse" % meta["file"], at["error"], "status")
        imports = [i for i in at["imports"]]
        h = static.tool(java, {"do": "hardened", "text": static.read(harden_dir(project, fid) / self.hardened_name).replace("\r\n", "\n"),
                               "name": name, "rename": "_Ras_" + name, "imports": imports})
        if h.get("error"):
            raise UserError("hardened.java can't be used", h["error"], "verify " + fid)
        rt, inner = runtime_package(meta.get("package") or "") + ".RasRt", ""
        body = h["body"].rstrip()
        if embedded(meta):  # `java App.java` compiles that one file only
            body = body.replace(rt, "RasRt")
            src = re.sub(r"(?m)^package .*;\n+", "", static.RUNTIME_SRC.read_text(encoding="utf-8"))
            inner = "\n\n" + src.replace("public final class RasRt", "static final class RasRt").rstrip()
            rt = "_Ras_%s.RasRt" % name
        sig = ", ".join("%s %s" % (p["type"], p["name"]) for p in meta["params"])
        args = ", ".join(p["name"] for p in meta["params"])
        deps = ", ".join("{%s, %s}" % (json.dumps(d.get("name") or ""), json.dumps(d.get("kind") or "http")) for d in meta.get("dependencies") or [])
        void = meta["returns"] == "void"
        call = "%s.%s(%s)" % ("_Ras_" + name, name, args)
        lam = "{ %s; return null; }" % call if void else call
        boxed = {"int": "Integer", "long": "Long", "double": "Double", "float": "Float", "short": "Short", "byte": "Byte", "boolean": "Boolean",
                 "char": "Character", "void": "Object"}.get(meta["returns"], meta["returns"])
        start, end = self.markers(fid)
        head = [start]
        if vres:
            head += ["// Hardened by ras-anything; verified %s on %d recorded calls" % (vres.get("verified_at", "?")[:10], vres["happy"]["total"]),
                     "// (availability under faults %.1f%% -> %.1f%%)." % (100 * vres["original"]["availability"], 100 * vres["hardened"]["availability"])]
        head.append("// Undo: `ras-anything disable %s`. RAS_ANYTHING_DISABLE=1 runs the original." % fid)
        lines = [*head, "static final class _Ras_%s {%s" % (name, body), *([inner] if inner else []), "}", "",
                 "%s%s %s(%s)%s {" % (at.get("modifiers") or "static ", meta["returns"], name, sig, " throws " + ", ".join(meta["throws"]) if meta["throws"] else ""),
                 "    if (%s.disabled(%s)) %s_ras_orig_%s(%s);" % (rt, json.dumps(fid), "{ " if void else "return ", name, args) + (" return; }" if void else ""),
                 "    %s%s.<%s, RuntimeException>observe(%s, new String[][] {%s}, () -> %s);" % ("" if void else "return ", rt, boxed, json.dumps(fid), deps, lam),
                 "}", end, ""]
        out = "\n".join(lines)
        if '"""' not in out:  # (a text block's indentation counts)
            out = re.sub(r"(?m)^(?=.)", "    " * (len(meta["binary"].split("$"))), out)
        return out, len(head)

    def insertion(self, path: Path, text: str, block: str, eol: str) -> tuple[int, str]:
        fid = re.match(r"\s*// ras-anything:start (.*);", block).group(1)
        at = static.tool(static.java_exe(), {"do": "place", "text": text, "owner": fid.rpartition(".")[0], "name": ""})
        if at.get("error") or at.get("class_end") is None:
            raise UserError("can't find the class of %s in %s" % (fid, path.name), at.get("error") or "nothing was changed", "harden " + fid)
        end = static.py_index(text, at["class_end"])
        line = text.rfind("\n", 0, end) + 1
        return (line, eol + block) if not text[line:end].strip() else (end, eol + block)

    def patches(self, path: Path, text: str, meta: dict) -> list[tuple[str, str]]:
        name = meta["name"]
        at = static.tool(static.java_exe(), {"do": "place", "text": text, "owner": meta["owner"], "name": name})
        if at.get("error") or at.get("name_at") is None:
            raise UserError("can't find %s in %s" % (meta["id"], path.name), at.get("error") or "the source changed since it was recorded; nothing was changed",
                            "harden " + meta["id"])
        if "_ras_orig_" + name in text:
            raise UserError("%s already has _ras_orig_%s" % (path.name, name), "nothing was changed", "status")
        i = static.py_index(text, at["name_at"])
        lo, hi = text.rfind("\n", 0, i) + 1, i
        for _ in range(8):  # as few lines as make it unique
            hi = text.find("\n", hi + 1)
            hi = len(text) if hi < 0 else hi
            old = text[lo:hi].rstrip("\r")
            if text.count(old) == 1:
                return [(old, old[: i - lo] + "_ras_orig_" + old[i - lo:])]
        raise UserError("the declaration of %s isn't unique in %s" % (name, path.name), "disable couldn't undo the change exactly; nothing was changed", "status")

    def valid_patch(self, old: str, new: str) -> bool:
        return new != old and new.replace("_ras_orig_", "", 1) == old

    def copy_name(self, fid: str, file: Path) -> str:
        return safe_name(fid) + ".java.txt"  # not .java: javac would take it for a source

    def runtime_file(self) -> tuple[Path, str]:
        return static.RUNTIME_SRC, "RasRt.java"

    def runtime_bytes(self, meta: dict) -> bytes:
        return runtime_source(meta.get("package") or "").encode("utf-8")

    # ---------------------------------------------------------------- people

    def doctor(self) -> list[tuple]:
        exe = shutil.which("java")
        if not exe:
            return [(None, "java", "not found (only needed for Java apps)")]
        ver, raw = static.version(exe)
        if ver is None or ver < 17:
            return [(None, raw, "ras-anything needs Java 17+")]
        try:
            static.javac_exe(exe)
        except UserError as e:
            return [(None, raw, e.impact)]
        return [(True, raw, "ready (HttpClient, HttpURLConnection, ProcessBuilder)")]


def runtime_package(pkg: str) -> str:
    return (pkg + "." if pkg else "") + "_ras_hardened"


def runtime_source(pkg: str) -> str:
    return static.RUNTIME_SRC.read_text(encoding="utf-8").replace("package _ras_hardened;", "package %s;" % runtime_package(pkg), 1)


def embedded(meta: dict) -> bool:
    """Is the app started as one source file, this one?"""
    c = static.Command(meta.get("cmd") or ["java"])
    return c.kind == "source" and os.path.normpath(os.path.join(meta.get("cwd") or ".", c.main)) == os.path.normpath(meta["file"])


def _javac_opts(c: static.Command) -> list[str]:
    ver, _ = static.version(c.java)
    return c.javac + (["--enable-preview", "--release", str(ver)] if "--enable-preview" in c.jvm else [])
