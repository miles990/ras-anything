"""Static reliability signals per function, from the AST (no imports, no execution)."""

from __future__ import annotations

import ast
from functools import lru_cache

HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}
WRITE_VERBS = {"post", "put", "patch", "delete"}
SUBPROCESS = {"run", "call", "check_call", "check_output", "Popen"}

# weight: how much each signal alone says "this will hurt under faults"
SIGNALS = {
    "no_timeout": (0.40, "{call}() without timeout"),
    "unsafe_retry": (0.40, "retries a {verb} with no Idempotency-Key"),
    "swallow": (0.30, "`{handler}` swallows errors"),
    "no_retry": (0.20, "no retry on transient errors"),
    "write_no_key": (0.15, "{verb} without Idempotency-Key"),
    "no_context": (0.10, "re-raises without `from`/context"),
    "open_no_with": (0.10, "open() outside `with`"),
    "no_logging": (0.05, "no logging on the failure path"),
}


def dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append(dotted(node.func) + "()")
    return ".".join(reversed(parts))


class FileInfo:
    def __init__(self, path: str):
        self.path = path
        self.src = open(path, encoding="utf-8", errors="replace").read()
        self.tree = ast.parse(self.src, filename=path)
        self.imports: dict[str, str] = {}  # local name -> full dotted origin
        self.has_main_guard = False
        self.main_guard_line = None
        self.sets_default_timeout = "setdefaulttimeout" in self.src
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.imports[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    self.imports[a.asname or a.name] = node.module + "." + a.name
            elif isinstance(node, ast.If) and _is_main_guard(node.test):
                self.has_main_guard = True
                self.main_guard_line = node.lineno
        self.functions: dict[int, dict] = {}  # first line (decorators included) -> info
        self._index(self.tree.body, prefix="", kind_ctx="module")

    def _index(self, body, prefix, kind_ctx):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                decos = {dotted(d.func if isinstance(d, ast.Call) else d).split(".")[-1] for d in node.decorator_list}
                if kind_ctx == "module":
                    kind = "function"
                elif kind_ctx == "class":
                    kind = "staticmethod" if "staticmethod" in decos else ("classmethod" if "classmethod" in decos else "method")
                else:
                    kind = "nested"
                qual = prefix + node.name
                info = {"qualname": qual, "kind": kind, "line": node.lineno, "first": first,
                        "end": getattr(node, "end_lineno", node.lineno), "async": isinstance(node, ast.AsyncFunctionDef),
                        "decorators": sorted(decos), "node": node}
                self.functions[first] = info
                self.functions.setdefault(node.lineno, info)
                self._index(node.body, qual + ".<locals>.", "function")
            elif isinstance(node, ast.ClassDef):
                self._index(node.body, prefix + node.name + ".", "class" if kind_ctx == "module" else "function")

    def boundary_kind(self, call: ast.Call):
        """('http'|'subprocess', name, verb) if this call crosses the boundary."""
        name = dotted(call.func)
        head, _, last = name.rpartition(".")
        origin = self.imports.get(name.split(".")[0], "")
        full = (origin + name[len(name.split(".")[0]):]) if origin else name
        if last == "urlopen" or full.endswith("urllib.request.urlopen"):
            verb = "GET"
            if len(call.args) > 1 or any(k.arg == "data" for k in call.keywords):
                verb = "POST"
            return "http", "urlopen", verb
        if full.startswith("subprocess.") and full.split(".")[-1] in SUBPROCESS:
            return "subprocess", "subprocess." + full.split(".")[-1], "RUN"
        if last in HTTP_VERBS and (full.startswith("requests.") or "session" in head.lower()
                                   or head.lower().endswith(("client", "http"))):
            verb = last.upper()
            if last == "request":
                verb = (_const_str(call.args[0]) if call.args else _kw_str(call, "method")) or "REQUEST"
            return "http", name, verb.upper()
        return None

    def analyze(self, info: dict) -> dict:
        node = info["node"]
        seg = ast.get_source_segment(self.src, node) or ""
        idem = "idempotency" in seg.lower()
        found: dict[str, str] = {}
        boundary_calls = []
        has_retry = bool({"retry", "retrying", "backoff", "on_exception"} & set(info["decorators"])) or "retry(" in seg
        loops: list = []
        requests_made: dict = {}
        with_items = set()
        logs = False
        handlers = []

        for n in _walk_own(node):
            if isinstance(n, (ast.With, ast.AsyncWith)):
                for it in n.items:
                    with_items.add(id(it.context_expr))
            if isinstance(n, ast.ExceptHandler):
                handlers.append(n)
            if isinstance(n, ast.Call):
                fname = dotted(n.func)
                if fname.split(".")[0] in {"logging", "logger", "log", "LOG", "LOGGER"} or fname.endswith(
                        ("log_event", "warnings.warn", "exception", "warning", "error")) or (
                        fname == "print" and any(k.arg == "file" for k in n.keywords)):
                    logs = True
                if fname.endswith("Request") and _kw_str(n, "method"):
                    requests_made[id(n)] = _kw_str(n, "method").upper()

        loop_nodes = [n for n in _walk_own(node) if isinstance(n, (ast.For, ast.While, ast.AsyncFor))]
        in_loop = set()
        for lp in loop_nodes:
            for n in ast.walk(lp):
                in_loop.add(id(n))

        verb_in_fn = None
        for n in _walk_own(node):
            if not isinstance(n, ast.Call):
                continue
            bk = self.boundary_kind(n)
            if bk is None:
                if dotted(n.func) == "open" and id(n) not in with_items:
                    found["open_no_with"] = SIGNALS["open_no_with"][1]
                continue
            kind, name, verb = bk
            if name == "urlopen" and n.args and isinstance(n.args[0], ast.Name):
                verb = _request_method(node, n.args[0].id) or verb
            if verb == "GET" and requests_made:
                verb = next(iter(requests_made.values()))
            boundary_calls.append((kind, name, verb))
            has_timeout = any(k.arg == "timeout" for k in n.keywords) or (name == "urlopen" and len(n.args) >= 3)
            if not has_timeout and not self.sets_default_timeout and "call_with_timeout" not in seg:
                found.setdefault("no_timeout", SIGNALS["no_timeout"][1].format(call=name))
            if verb.lower() in WRITE_VERBS:
                verb_in_fn = verb
                retried = id(n) in in_loop or has_retry
                if not idem:
                    if retried:
                        found["unsafe_retry"] = SIGNALS["unsafe_retry"][1].format(verb=verb)
                    else:
                        found.setdefault("write_no_key", SIGNALS["write_no_key"][1].format(verb=verb))
            if id(n) in in_loop:
                has_retry = True

        if boundary_calls and not has_retry:
            found["no_retry"] = SIGNALS["no_retry"][1]
        if "unsafe_retry" in found:
            found.pop("write_no_key", None)

        for h in handlers:
            tname = dotted(h.type) if h.type is not None else ""
            broad = h.type is None or tname in {"Exception", "BaseException"}
            if broad and _swallows(h):
                label = "except:" if h.type is None else "except %s: %s" % (tname, _brief(h.body[0]))
                found["swallow"] = SIGNALS["swallow"][1].format(handler=label)
            for r in ast.walk(h):
                if isinstance(r, ast.Raise) and r.exc is not None and r.cause is None:
                    if not (isinstance(r.exc, ast.Name) and r.exc.id == h.name):
                        found["no_context"] = SIGNALS["no_context"][1]
        if (boundary_calls or handlers) and not logs:
            found["no_logging"] = SIGNALS["no_logging"][1]

        risk = 1.0
        for k in found:
            risk *= 1.0 - SIGNALS[k][0]
        return {
            "signals": found,
            "static_risk": round(1.0 - risk, 4),
            "boundary_calls": [list(b) for b in boundary_calls],
            "write_verb": verb_in_fn,
        }


def _request_method(fn, var):
    """`req = Request(url, data, method="POST")` ... `urlopen(req)` -> "POST"."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == var for t in n.targets):
            v = n.value
            if isinstance(v, ast.Call) and dotted(v.func).endswith("Request"):
                m = _kw_str(v, "method")
                if m:
                    return m.upper()
                if len(v.args) > 1 or any(k.arg == "data" for k in v.keywords):
                    return "POST"
    return None


def _walk_own(fn):
    """Nodes of fn's body, not descending into nested functions/classes."""
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                stack.append(c)


def _swallows(h) -> bool:
    """A broad handler that neither re-raises nor logs: the error is gone."""
    for n in ast.walk(h):
        if isinstance(n, ast.Raise):
            return False
        if isinstance(n, ast.Call):
            f = dotted(n.func)
            if f.split(".")[0] in {"logging", "logger", "log", "LOG"} or f.endswith(("log_event", "warn", "exception", "error", "warning")):
                return False
    return True


def _is_swallow_stmt(s) -> bool:
    if isinstance(s, (ast.Pass, ast.Continue)):
        return True
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
        return True
    if isinstance(s, ast.Return):
        return s.value is None or (isinstance(s.value, ast.Constant) and s.value.value in (None, False, 0, "", ()))
    return False


def _brief(s) -> str:
    if isinstance(s, ast.Return):
        return "return" if s.value is None else "return %s" % ast.unparse(s.value)
    return ast.unparse(s).split("\n")[0][:20]


def _const_str(n):
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None


def _kw_str(call, name):
    for k in call.keywords:
        if k.arg == name:
            return _const_str(k.value)
    return None


def _is_main_guard(test) -> bool:
    return (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__"
            and len(test.comparators) == 1 and _const_str(test.comparators[0]) == "__main__")


@lru_cache(maxsize=256)
def load(path: str) -> FileInfo | None:
    try:
        return FileInfo(path)
    except (SyntaxError, OSError, UnicodeDecodeError):
        return None
