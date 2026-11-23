"""Java: command lines, source roots, and the helper that reads, instruments and compiles Java with the JDK's own compiler."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from ..paths import UserError

JAVA_AGENT_DIR = Path(__file__).resolve().parent / "java_agent"
RUNTIME_SRC = JAVA_AGENT_DIR / "runtime" / "RasRt.java"
SKIP_DIRS = {".git", ".ras", ".gradle", ".idea", ".venv", "node_modules", "build", "target", "out", "dist"}

# JVM options that take the next word as their value (others are one word: -Dk=v, -Xmx1g, -XX:…, --opt=value)
CP_FLAGS = {"-cp", "-classpath", "--class-path"}
VALUE_FLAGS = CP_FLAGS | {"-p", "--module-path", "--upgrade-module-path", "--add-modules", "--add-exports", "--add-opens", "--add-reads",
                          "--limit-modules", "--patch-module", "--enable-native-access", "--source", "--finalization"}
JAVAC_TOO = {"--add-modules", "--add-exports", "--add-reads", "--limit-modules"}


class Command:
    """`java [JVM options] App.java|Main|-jar app.jar [args]`, taken apart."""

    def __init__(self, cmd: list[str]):
        self.java, self.jvm, self.javac, self.cp, self.kind, self.main, self.args = cmd[0], [], [], None, None, None, []
        source = False
        i = 1
        while i < len(cmd):
            a = cmd[i]
            flag, eq, val = a.partition("=") if a.startswith("--") else (a, "", "")
            if flag in VALUE_FLAGS:
                if not eq:
                    if i + 1 >= len(cmd):
                        return
                    val = cmd[i + 1]
                    i += 1
                if flag in CP_FLAGS:
                    self.cp = val
                elif flag == "--source":
                    source = True
                else:
                    self.jvm += [flag, val]
                    if flag in JAVAC_TOO:
                        self.javac += [flag, val]
            elif a == "-jar":
                self.kind, self.main, self.args = "jar", (cmd[i + 1:] or [None])[0], cmd[i + 2:]
                return
            elif a in ("-m", "--module") or flag == "--module":
                self.kind, self.main, self.args = "module", val or (cmd[i + 1:] or [None])[0], []
                return
            elif a.startswith("@"):
                self.kind, self.main = "argfile", a
                return
            elif a.startswith("-"):
                self.jvm.append(a)
            else:
                self.kind = "source" if source or a.endswith(".java") else "class"
                self.main, self.args = a.replace("/", ".") if self.kind == "class" else a, cmd[i + 1:]
                return
            i += 1

    def class_path(self, cwd: Path) -> str:
        """The app's own class path, absolute."""
        if self.kind == "jar":
            jar = cwd / self.main
            parts = [str(jar)] + [str(jar.parent / p) for p in _manifest(jar).get("Class-Path", "").split()]
        else:
            parts = (self.cp or os.environ.get("CLASSPATH") or ".").split(os.pathsep)
        return os.pathsep.join(os.path.normpath(str(cwd / (p or "."))) for p in parts)

    def main_class(self, cwd: Path) -> str:
        if self.kind == "jar":
            name = _manifest(cwd / self.main).get("Main-Class")
            if not name:
                raise UserError("%s has no Main-Class" % self.main, "java -jar needs one in the manifest", "scan -- java App.java")
            return name
        if self.kind == "source":
            text = read(cwd / self.main)
            m = re.search(r"(?m)^[^\S\n]*(?:(?:public|final|abstract|sealed|strictfp)\s+)*(?:class|record|enum|interface)\s+(\w+)", strip(text))
            if not m:
                raise UserError("no class in %s" % self.main, "", "scan -- java App.java")
            p = package_of(text)
            return (p + "." if p else "") + m.group(1)
        return self.main

    def main_file(self, cwd: Path) -> Path:
        """The source file of the main class."""
        if self.kind == "source":
            f = (cwd / self.main).resolve()
            if not f.is_file():
                raise UserError("%s isn't there" % self.main, "", "scan -- java App.java")
            return f
        f = find_source(cwd, self.main_class(cwd))
        if f is None:
            raise UserError("can't find the source of %s under this folder" % self.main_class(cwd),
                            "ras-anything instruments a copy of your Java sources: run it from the project's folder", "scan -- java App.java")
        return f


def _manifest(jar: Path) -> dict:
    try:
        with zipfile.ZipFile(jar) as z:
            text = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile) as e:
        raise UserError("can't read %s" % jar.name, str(e), "scan -- java App.java")
    text = re.sub(r"\r?\n ", "", text)
    return dict((k.strip(), v.strip()) for k, _, v in (ln.partition(":") for ln in text.splitlines()) if k.strip())


def read(p: Path) -> str:
    return p.read_bytes().decode("utf-8", "replace")


def strip(text: str) -> str:
    """Source without comments and the contents of literals (same length)."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    return re.sub(r'//[^\n]*|/\*.*?\*/|"""(?:\\.|[^\\])*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])+\'', blank, text, flags=re.S)


def package_of(text: str) -> str:
    m = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", strip(text))
    return m.group(1) if m else ""


def java_files(root: Path):
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        for n in sorted(names):
            if n.endswith(".java"):
                yield Path(d) / n


def find_source(cwd: Path, main: str) -> Path | None:
    pkg, _, name = main.rpartition(".")
    maybe = None
    for f in java_files(cwd.resolve()):
        if "_ras_hardened" in f.parts:
            continue
        if f.stem == name and package_of(read(f)) == pkg:
            return f
        if maybe is None:
            text = read(f)
            if re.search(r"\b(?:class|record|enum|interface)\s+%s\b" % re.escape(name), strip(text)) and package_of(text) == pkg:
                maybe = f
    return maybe


def source_root(file: Path) -> Path:
    """The folder the file's package starts in."""
    pkg = package_of(read(file)).split(".") if package_of(read(file)) else []
    d = file.resolve().parent
    if pkg and list(d.parts[-len(pkg):]) == pkg:
        for _ in pkg:
            d = d.parent
    return d


# ---------------------------------------------------------------- JDK


@functools.lru_cache(maxsize=8)
def version(exe: str) -> tuple[int | None, str]:
    try:
        p = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    txt = (p.stderr or p.stdout).strip()
    m = re.search(r'version "(?:1\.)?(\d+)', txt)
    return (int(m.group(1)) if m else None), (txt.splitlines() or ["exit %d" % p.returncode])[0]


def java_exe(name: str = "java") -> str:
    exe = shutil.which(name)
    if not exe:
        raise UserError("%s isn't installed" % name, "install a JDK, version 17 or later, to scan or harden Java apps", "doctor")
    return exe


def javac_exe(java: str) -> str:
    for cand in (Path(java).parent / "javac", Path(os.path.realpath(java)).parent / "javac"):
        if cand.exists():
            return str(cand)
    raise UserError("no javac next to %s" % Path(java).name, "a JDK is needed (not just a JRE): install e.g. openjdk-21-jdk", "doctor")


def _cache() -> Path:
    d = Path(tempfile.gettempdir()) / ("ras-java-cache-%d" % os.getuid() if hasattr(os, "getuid") else "ras-java-cache")
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = d.lstat()
    if hasattr(os, "getuid") and (st.st_uid != os.getuid() or (st.st_mode & 0o777) != 0o700 or d.is_symlink()):
        d = Path(tempfile.mkdtemp(prefix="ras-java-cache-"))
    return d


def _into_cache(key: str, make) -> Path:
    """cache/<key>, made once by make(folder)."""
    base = _cache()
    done = base / key
    if not done.is_dir():
        tmp = Path(tempfile.mkdtemp(prefix=key + ".", dir=base))
        try:
            make(tmp)
            try:
                tmp.rename(done)
            except OSError:
                if not done.is_dir():
                    raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return done


@functools.lru_cache(maxsize=8)
def tools(java: str) -> Path:
    """Compiled agent and source tool for this JDK."""
    srcs = sorted(JAVA_AGENT_DIR.glob("agent/*.java")) + sorted(JAVA_AGENT_DIR.glob("tool/*.java"))
    h = hashlib.sha256(repr(version(java)).encode())
    for s in srcs:
        h.update(s.read_bytes())

    def make(d):
        p = subprocess.run([javac_exe(java), "-nowarn", "-Xlint:none", "-encoding", "UTF-8", "-d", str(d), *map(str, srcs)],
                           capture_output=True, text=True, timeout=180)
        if p.returncode:
            raise UserError("couldn't compile the Java agent", (p.stderr or p.stdout).strip()[-600:], "doctor")

    return _into_cache("agent-" + h.hexdigest()[:20], make)


def agent_key(java: str) -> str:
    return tools(java).name


def tool(java: str, req: dict) -> dict:
    """One request to the source tool. Answers about a text ("place", "hardened") are kept: they can't change."""
    if req["do"] not in ("place", "hardened"):
        return _tool(java, req)
    key = "text-" + hashlib.sha256((agent_key(java) + json.dumps(req, sort_keys=True)).encode()).hexdigest()[:24]
    d = _into_cache(key, lambda d: (d / "answer.json").write_text(json.dumps(_tool(java, req)), encoding="utf-8"))
    return json.loads((d / "answer.json").read_text(encoding="utf-8"))


def _tool(java: str, req: dict) -> dict:
    p = subprocess.run([java, "-XX:TieredStopAtLevel=1", "-XX:+UseSerialGC", "-cp", str(tools(java)), "_ras_agent.RasTool"], input=json.dumps(req).encode(),
                       capture_output=True, timeout=300)
    try:
        return json.loads(p.stdout.decode("utf-8"))
    except ValueError:
        raise UserError("the Java source tool failed", p.stderr.decode("utf-8", "replace").strip()[-800:], "doctor")


class BuildFailed(Exception):
    pass


def _prepare(work: Path, root: Path, overlay: dict[str, str]) -> list[str]:
    rels = []
    for f in java_files(root):
        rel = f.relative_to(root).as_posix()
        if rel not in overlay:
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, work / rel)
            rels.append(rel)
    for rel, text in overlay.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_bytes(text.encode("utf-8"))
        rels.append(rel)
    return sorted(rels)


def build(java: str, root: Path, entry: list[str], class_path: str, mode: str, targets: list[str] = (), javac_opts: list[str] = (),
          overlay: dict[str, str] | None = None) -> Path:
    """Instrumented classes (folder/classes) and the function table (folder/table.json) of the program that starts in
    `entry` (files relative to the source root). Nothing in the project is touched; done once per content."""
    root, overlay = root.resolve(), overlay or {}
    h = hashlib.sha256(json.dumps([agent_key(java), str(root), entry, class_path, mode, sorted(targets), list(javac_opts), overlay], sort_keys=True).encode())
    for f in java_files(root):
        h.update(str(f).encode() + b"\0" + f.read_bytes() + b"\0")
    for p in class_path.split(os.pathsep):  # jars and folders the sources compile against
        h.update(repr((p, os.path.getmtime(p) if p.endswith(".jar") and os.path.exists(p) else 0)).encode())

    def make(d):
        def attempt(files):
            shutil.rmtree(d / "src", ignore_errors=True)
            shutil.rmtree(d / "classes", ignore_errors=True)
            rels = _prepare(d / "src", root, overlay)
            return rels, tool(java, {"do": "build", "src": str((d / "src").resolve()), "origin": str(root), "entry": files or rels,
                                     "classpath": class_path + os.pathsep + str(tools(java)), "classes": str(d / "classes"), "mode": mode,
                                     "targets": list(targets), "javac_opts": list(javac_opts)})

        rels, res = attempt(entry)
        if res.get("error"):  # sources that aren't in folders named like their packages: compile everything
            _, again = attempt(None)
            if again.get("error"):
                raise BuildFailed(res["error"])
            res = again
        shutil.rmtree(d / "src", ignore_errors=True)
        at = {f["id"]: i for i, f in enumerate(res["functions"])}
        ranges = [{"unit": r["unit"], "start": r["start"], "end": r["end"], "fn": at[r["id"]]} for r in res["ranges"] if r["id"] in at]
        (d / "table.json").write_text(json.dumps({"functions": res["functions"], "ranges": ranges}), encoding="utf-8")

    return _into_cache("build-" + h.hexdigest()[:24], make)


_seen: dict = {}


def inspect(java: str, root: Path, entry: str, class_path: str, javac_opts: list[str] = ()) -> list[dict]:
    """The functions of the program around one file."""
    root = root.resolve()
    key = (java, str(root), entry, class_path, tuple((str(f), f.stat().st_mtime_ns, f.stat().st_size) for f in java_files(root)))
    if key not in _seen:
        d = build(java, root, [entry], class_path, "inspect", (), javac_opts)
        _seen[key] = json.loads((d / "table.json").read_text(encoding="utf-8"))["functions"]
    return _seen[key]


def py_index(text: str, i: int) -> int:
    """javac counts UTF-16 units, Python code points."""
    return i if text.isascii() else len(text.encode("utf-16-le")[: 2 * i].decode("utf-16-le"))
