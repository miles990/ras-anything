"""JavaScript/TypeScript source analysis for the Node adapter: a tokenizer, the top-level declarations of a
module, per-function reliability signals, and the instrumented sources the agent loads.

Deliberately small: no AST, no npm packages. Where the tokens don't settle a question the answer is the
safe one (a function isn't offered for hardening, a statement isn't instrumented).
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ..signals import SIGNALS

EXTS = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")
SKIP_DIRS = {"node_modules", ".ras", ".git", "_ras_hardened", "bower_components", "__pycache__", ".venv"}
MAX_FILE = 1_000_000  # bytes: bigger files are bundles
MAX_FILES = 4000

PUNCT = sorted(["{", "}", "(", ")", "[", "]", ";", ",", "<", ">", "<=", ">=", "==", "!=", "===", "!==", "+", "-", "*", "/",
                "%", "**", "++", "--", "<<", ">>", ">>>", "&", "|", "^", "!", "~", "&&", "||", "??", "?", "?.", ":", "=",
                "+=", "-=", "*=", "/=", "%=", "**=", "<<=", ">>=", ">>>=", "&=", "|=", "^=", "&&=", "||=", "??=", "=>",
                ".", "...", "@", "#"], key=len, reverse=True)
TOKEN = re.compile(
    r"(?P<ws>[^\S\n]+|\n)|(?P<comment>//[^\n]*|/\*.*?\*/|\A#![^\n]*)"
    r"|(?P<id>#?[A-Za-z_$\u0080-\U0010ffff][\w$\u0080-\U0010ffff‌‍]*)"
    r"|(?P<num>(?:0[xXbBoO][\da-fA-F_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?)n?)"
    r"|(?P<str>\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*')|(?P<tick>`)|(?P<p>" + "|".join(map(re.escape, PUNCT)) + ")",
    re.S)
REGEX = re.compile(r"/(?![*/])(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\[\n])+/[A-Za-z]*")
BEFORE_REGEX = {"return", "typeof", "case", "in", "of", "delete", "void", "throw", "new", "instanceof", "yield", "await", "else", "do"}
KEYWORDS = BEFORE_REGEX | {"break", "catch", "class", "const", "continue", "debugger", "default", "export", "extends", "finally",
                           "for", "function", "if", "import", "super", "switch", "this", "try", "var", "while", "with", "let",
                           "static", "async", "null", "true", "false", "undefined"}
ASSIGN = {"=", "+=", "-=", "*=", "/=", "%=", "**=", "<<=", ">>=", ">>>=", "&=", "|=", "^=", "&&=", "||=", "??="}
MUTATORS = {"set", "add", "delete", "clear", "push", "pop", "shift", "unshift", "splice", "sort", "reverse", "fill", "copyWithin"}

HTTP_MODULES = {"http", "https", "node:http", "node:https"}
CP_MODULES = {"child_process", "node:child_process"}
CP_NAMES = {"exec", "execFile", "spawn", "execSync", "execFileSync", "spawnSync"}
CLIENT_MODULES = {"axios", "got", "node-fetch", "undici", "ky", "superagent", "needle"}
WRITE = {"POST", "PUT", "PATCH", "DELETE"}
LOGGERS = {"console", "logger", "log", "pino", "winston", "LOG", "logEvent", "logError", "warn"}


class LexError(Exception):
    pass


@dataclass
class Tok:
    kind: str  # id num str re tpl p
    text: str
    start: int
    end: int
    line: int
    nl: bool  # first token after a line break


def tokenize(src: str) -> tuple[list[Tok], dict[int, int]]:
    """Tokens and bracket pairs (both directions). Template literals become chunks plus the tokens of
    their ${...} parts."""
    toks: list[Tok] = []
    pairs: dict[int, int] = {}
    stack: list[tuple[str, int]] = []  # (opener, token index); "${" and "`" included
    close = {")": "(", "]": "[", "}": "{"}
    pos, line, nl, n = 0, 1, True, len(src)

    def add(kind, start, end):
        nonlocal nl
        toks.append(Tok(kind, src[start:end], start, end, line, nl))
        nl = False

    def template(start):
        """From `start` (just after ` or the } of a ${...}) to the next ${ or `."""
        nonlocal line
        j = start
        while j < n:
            c = src[j]
            if c == "\\":
                j += 2
            elif c == "`":
                add("tpl", start, j)
                line += src.count("\n", start, j)
                stack.pop()
                return j + 1
            elif c == "$" and src[j + 1: j + 2] == "{":
                add("tpl", start, j)
                line += src.count("\n", start, j)
                add("p", j, j + 2)
                stack.append(("${", len(toks) - 1))
                return j + 2
            else:
                j += 1
        raise LexError("unterminated template literal")

    while pos < n:
        c = src[pos]
        if c == "/" and src[pos + 1: pos + 2] not in ("/", "*") and _regex_here(toks, pairs, nl):
            m = REGEX.match(src, pos)
            if m:
                add("re", pos, m.end())
                pos = m.end()
                continue
        m = TOKEN.match(src, pos)
        if not m:
            raise LexError("unexpected %r on line %d" % (c, line))
        kind, text = m.lastgroup, m.group()
        if kind in ("ws", "comment"):
            if "\n" in text:
                nl = True
                line += text.count("\n")
            pos = m.end()
            continue
        if kind == "tick":
            stack.append(("`", len(toks)))
            pos = template(pos + 1)
            continue
        if kind == "p" and text in "([{":
            add("p", pos, m.end())
            stack.append((text, len(toks) - 1))
        elif kind == "p" and text in ")]}":
            if not stack:
                raise LexError("unbalanced %s on line %d" % (text, line))
            opener, i = stack.pop()
            add("p", pos, m.end())
            pairs[i], pairs[len(toks) - 1] = len(toks) - 1, i
            if opener == "${" and text == "}":
                pos = template(pos + 1)
                continue
            if opener != close[text]:
                raise LexError("unbalanced %s on line %d" % (text, line))
        else:
            add(kind, pos, m.end())
            if kind == "str":
                line += text.count("\n")
        pos = m.end()
    if stack:
        raise LexError("unbalanced %s" % stack[-1][0])
    return toks, pairs


def _regex_here(toks, pairs, nl) -> bool:
    if not toks:
        return True
    t = toks[-1]
    if t.kind == "id":
        return t.text in BEFORE_REGEX and not (len(toks) > 1 and toks[-2].text in (".", "?."))
    if t.kind in ("num", "str", "re", "tpl"):
        return False
    if t.text == ")":
        i = pairs.get(len(toks) - 1, 0)
        return i > 0 and toks[i - 1].text in ("if", "while", "for", "with")
    if t.text in ("]", "++", "--"):
        return False
    if t.text == "}":
        return nl
    return True


@dataclass
class Func:
    qualname: str
    name: str
    kind: str               # function | method
    line: int
    tokens: tuple[int, int]  # token range of the function itself
    text: tuple[int, int]    # source range of the declaration to copy
    is_async: bool = False
    gen: bool = False
    binding: str = ""       # function | const | let | inline | method
    accessor: str = ""      # expression for the function in its module's scope
    exports: list = field(default_factory=list)
    default_value: bool = False  # exported only as `export default name` (a value, not a live binding)
    params: tuple[int, int] = (0, 0)
    body: tuple[int, int] = (0, 0)
    stmt_end: int | None = None  # source offset right after its statement, when that is certain
    owner: str = ""          # class of a method
    static: bool = False


@dataclass
class Var:
    kind: str                # const | let | var | class | import | require | enum
    init: tuple[int, int] | None = None
    text: tuple[int, int] | None = None  # source range of the whole statement
    source: str = ""         # imports: the specifier
    imported: str = ""       # imports: the name there ("*" = namespace, "default")


class FileInfo:
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            raw = f.read()
        self.sha = hashlib.sha256(raw).hexdigest()
        self.src = raw.decode("utf-8")
        if self.src.startswith("\ufeff"):
            raise LexError("byte order mark")
        self.ext = os.path.splitext(path)[1]
        self.ts = self.ext in (".ts", ".mts", ".cts")
        self.toks, self.pairs = tokenize(self.src)
        self.funcs: list[Func] = []
        self.vars: dict[str, Var] = {}
        self.types: dict[str, tuple[int, int]] = {}
        self.local_exports: dict[str, list[str]] = {}
        self.default_values: set[str] = set()
        self.has_esm = False
        self.prologue = 0        # where code may go before everything else
        self._cjs_assigned: list = []
        self._parse()
        self.esm = self._is_esm()
        self._link_exports()
        self.by_name = {f.qualname: f for f in self.funcs}

    # ---------------------------------------------------------------- module structure

    def t(self, i: int) -> str:
        return self.toks[i].text if 0 <= i < len(self.toks) else ""

    def tk(self, i: int) -> Tok | None:
        return self.toks[i] if 0 <= i < len(self.toks) else None

    def _is_esm(self) -> bool:
        if self.ext in (".mjs", ".mts"):
            return True
        if self.ext in (".cjs", ".cts"):
            return False
        if self.has_esm:
            return True
        return package_type(os.path.dirname(os.path.abspath(self.path))) == "module"

    def _skip(self, i: int) -> int:
        """The token after i, or after the bracket group that opens at i."""
        return self.pairs[i] + 1 if i in self.pairs and self.pairs[i] > i else i + 1

    def _parse(self):
        i, n = 0, len(self.toks)
        directives = True
        while i < n:
            tok = self.toks[i]
            if directives:
                if tok.kind == "str" and (self.t(i + 1) == ";" or i + 1 == n or self.toks[i + 1].nl):
                    i += 2 if self.t(i + 1) == ";" else 1
                    self.prologue = self.toks[i - 1].end
                    continue
                directives = False
                if self.prologue == 0:
                    self.prologue = tok.start
            j = self._statement(i)
            i = max(j, i + 1)

    def _statement(self, i: int) -> int:
        start = i
        exported = default = False
        while self.t(i) == "@":  # decorator
            i += 1
            while self.tk(i) and (self.tk(i).kind == "id" or self.t(i) == "."):
                i += 1
            if self.t(i) == "(":
                i = self._skip(i)
        if self.t(i) == "export":
            self.has_esm = True
            exported = True
            i += 1
            if self.t(i) == "default":
                default = True
                i += 1
            elif self.t(i) == "{" or (self.t(i) == "type" and self.t(i + 1) == "{"):
                return self._export_list(i)
            elif self.t(i) == "*":
                return self._end(start)
        if self.t(i) == "declare":
            return self._end(i + 1, braces=True)
        if self.t(i) == "abstract" and self.t(i + 1) == "class":
            i += 1
        word = self.t(i)
        if word == "import" and self.t(i + 1) not in ("(", "."):
            self.has_esm = True
            return self._import(i)
        if word in ("interface", "type", "enum", "namespace") and self.tk(i + 1) and self.tk(i + 1).kind == "id" and not self.tk(i + 1).nl and self.ts:
            return self._type(start, i, word)
        if word == "const" and self.t(i + 1) == "enum":
            return self._type(start, i + 1, "enum")
        if word == "function" or (word == "async" and self.t(i + 1) == "function" and not self.toks[i + 1].nl):
            return self._function_decl(start, i, exported, default)
        if word == "class":
            return self._class(start, i, exported, default)
        if word in ("const", "let", "var") and self.tk(i + 1) and (self.tk(i + 1).kind == "id" or self.t(i + 1) in "{["):
            return self._declaration(start, i, exported)
        if default:
            end = self._end(i)
            if end - i <= 2 and self.tk(i).kind == "id":
                self.default_values.add(self.t(i))
            return end
        if word in ("module", "exports"):
            return self._cjs(i)
        return self._end(i, braces=True)

    def _end(self, i: int, braces: bool = False) -> int:
        """First token after the statement at i. braces: a block ends it too."""
        n = len(self.toks)
        while i < n:
            if self.t(i) == ";":
                return i + 1
            nxt = self._skip(i)
            if nxt >= n:
                return n
            if self._ends_between(nxt - 1, nxt, braces):
                return nxt
            i = nxt
        return n

    def _ends_between(self, a: int, b: int, braces: bool = False) -> bool:
        """Does a statement end between neighbours a and b (automatic semicolon insertion)?"""
        x, y = self.toks[a], self.toks[b]
        if braces and x.text == "}" and y.text not in (")", ",", ";", ".", "?.", "(", "["):
            return True
        if not y.nl:
            return False
        before = x.kind in ("num", "str", "re", "tpl") or x.text in (")", "]", "}", "++", "--") or (
            x.kind == "id" and x.text not in (BEFORE_REGEX - {"of"}) | {"extends", "const", "let", "var", "function", "class", "import",
                                                                         "export", "async", "default", "from"})
        after = y.kind in ("num", "str") or y.text in ("{", "}", "@", "!", "~", "#", "++", "--") or (
            y.kind == "id" and y.text not in ("in", "of", "instanceof", "as", "satisfies", "from"))
        return before and after

    def _certain_end(self, i: int) -> int | None:
        """Source offset after the statement whose next token is i, if the boundary is beyond doubt."""
        if i > 0 and self.t(i - 1) == ";":
            return self.toks[i - 1].end
        if i >= len(self.toks):
            return self.toks[-1].end
        t = self.toks[i]
        if t.nl and t.kind == "id" and self.toks[i - 1].text in ("}", ")", "]"):
            return self.toks[i - 1].end
        return None

    def _export_list(self, i: int) -> int:
        type_only = self.t(i) == "type"
        if type_only:
            i += 1
        close = self.pairs[i]
        names, k = [], i + 1
        while k < close:
            item = []
            while k < close and self.t(k) != ",":
                item.append(self.toks[k].text)
                k += 1
            k += 1
            if item and not (item[0] == "type" and len(item) > 1):
                names.append((item[0], item[-1]))
        end = self._end(close)
        if self.t(close + 1) != "from" and not type_only:
            for local, out in names:
                self.local_exports.setdefault(local, []).append(out)
        return end

    def _import(self, i: int) -> int:
        end = self._end(i)
        text = (self.toks[i].start, self.toks[end - 1].end)
        spec = next((self.toks[k].text[1:-1] for k in range(end - 1, i, -1) if self.toks[k].kind == "str"), "")
        k = i + 1
        if self.t(k) == "type" and self.t(k + 1) not in ("from", ","):
            k += 1
        while k < end and self.toks[k].kind != "str" and self.t(k) != "from":
            t = self.toks[k]
            if t.text == "*" and self.t(k + 1) == "as":
                self.vars[self.t(k + 2)] = Var("import", None, text, spec, "*")
                k += 3
            elif t.text == "{":
                close, m = self.pairs[k], k + 1
                while m < close:
                    item = []
                    while m < close and self.t(m) != ",":
                        item.append(self.toks[m].text.strip("\"'"))
                        m += 1
                    m += 1
                    if item and item[0] == "type" and len(item) > 1 and item[1] != "as":
                        item = item[1:]
                    if item:
                        self.vars[item[-1]] = Var("import", None, text, spec, item[0])
                k = close + 1
            elif t.kind == "id":
                self.vars[t.text] = Var("import", None, text, spec, "default")
                k += 1
            else:
                k += 1
        return end

    def _type(self, start: int, i: int, word: str) -> int:
        name = self.t(i + 1)
        end = self._end(i, braces=word != "type")
        rng = (self.toks[start].start, self.toks[end - 1].end)
        if word in ("interface", "type"):
            self.types[name] = rng
        else:
            self.vars[name] = Var("enum", None, rng)
        return end

    def _signature(self, i: int) -> tuple[int, int, int, int] | None:
        """(params "(", params ")", body start, body end+1) of a function whose "(" or "<" is at i; None
        without a block body."""
        if self.t(i) == "<":
            i = self._skip_angle(i)
        if self.t(i) != "(":
            return None
        p1 = self.pairs[i]
        b = self._return_type(p1 + 1)
        if b is None or self.t(b) != "{":
            return None
        return i, p1, b, self.pairs[b] + 1

    def _skip_angle(self, i: int) -> int:
        depth = 0
        while i < len(self.toks):
            t = self.t(i)
            if t == "<":
                depth += 1
            elif t in (">", ">>", ">>>"):
                depth -= len(t)
                if depth <= 0:
                    return i + 1
            elif t in ("(", "[", "{"):
                i = self.pairs[i]
            i += 1
        return i

    def _return_type(self, i: int) -> int | None:
        """Index of the body's "{" or of "=>", past a TypeScript return type if there is one."""
        if self.t(i) != ":":
            return i
        i += 1
        complete = False  # could the type end here?
        while i < len(self.toks):
            t = self.toks[i]
            if t.text == "{":
                if complete:
                    return i
                i, complete = self.pairs[i] + 1, True
            elif t.text == "=>":
                if complete and self.t(i - 1) != ")":
                    return i
                if complete and not self._fn_type_paren(i - 1):
                    return i
                i, complete = i + 1, False
            elif t.text in ("(", "["):
                i, complete = self.pairs[i] + 1, True
            elif t.text == "<":
                i, complete = self._skip_angle(i), True
            elif t.text in ("|", "&", ".", "?", ":", ","):
                i, complete = i + 1, False
            elif t.kind in ("id", "str", "num", "tpl"):
                complete = t.text not in ("is", "asserts", "keyof", "typeof", "readonly", "infer", "extends", "new", "unique")
                i += 1
            else:
                return None
        return None

    def _fn_type_paren(self, close: int) -> bool:
        """Is the "(...)" closing here a function type's parameter list (so "=>" belongs to the type)?"""
        before = self.t(self.pairs[close] - 1)
        return before in (":", "|", "&", "=>", ">")

    def _function_decl(self, start: int, i: int, exported: bool, default: bool) -> int:
        first = i
        is_async = self.t(i) == "async"
        k = i + (2 if is_async else 1)
        gen = self.t(k) == "*"
        if gen:
            k += 1
        name = self.t(k) if self.tk(k) and self.tk(k).kind == "id" else ""
        sig = self._signature(k + (1 if name else 0))
        if sig is None:  # an overload signature
            return self._end(i)
        end = sig[3]
        if name:
            f = Func(name, name, "function", self.toks[first].line, (first, end), (self.toks[first].start, self.toks[end - 1].end),
                     is_async, gen, "function", name, params=(sig[0], sig[1]), body=(sig[2], end),
                     stmt_end=self.toks[end - 1].end)
            if exported:
                f.exports.append("default" if default else name)
            self.funcs.append(f)
        else:
            f = Func("default", "default", "function", self.toks[first].line, (first, end), (self.toks[first].start, self.toks[end - 1].end),
                     is_async, gen, "anonymous", "", params=(sig[0], sig[1]), body=(sig[2], end))
            f.exports.append("default")
            self.funcs.append(f)
        return end

    def _function_expr(self, i: int, limit: int) -> dict | None:
        """A function or arrow expression that fills tokens [i, limit)."""
        first, is_async, gen, name = i, False, False, ""
        if self.t(i) == "async" and self.tk(i + 1) and not self.toks[i + 1].nl and self.t(i + 1) not in ("=>", ".", "("):
            is_async, i = True, i + 1
        elif self.t(i) == "async" and self.t(i + 1) == "(" and self.t(self._skip(i + 1)) in ("=>", ":"):
            is_async, i = True, i + 1
        if self.t(i) == "function":
            i += 1
            if self.t(i) == "*":
                gen, i = True, i + 1
            if self.tk(i) and self.tk(i).kind == "id":
                name, i = self.t(i), i + 1
            sig = self._signature(i)
            if sig is None or sig[3] != limit:
                return None
            return {"name": name, "async": is_async, "gen": gen, "params": (sig[0], sig[1]), "body": (sig[2], limit),
                    "arrow": False, "tokens": (first, limit)}
        params = None
        if self.t(i) == "<" and self.ts:
            i = self._skip_angle(i)
        if self.t(i) == "(":
            params = (i, self.pairs[i])
            i = self._return_type(self.pairs[i] + 1)
            if i is None:
                return None
        elif self.tk(i) and self.tk(i).kind == "id" and self.t(i) not in KEYWORDS and self.t(i + 1) == "=>":
            params = (i, i)
            i += 1
        if params is None or self.t(i) != "=>" or i + 1 >= limit:
            return None
        i += 1
        if self.t(i) == "{" and self.pairs[i] + 1 != limit:
            return None
        return {"name": "", "async": is_async, "gen": False, "params": params, "body": (i, limit), "arrow": True, "tokens": (first, limit)}

    def _declaration(self, start: int, i: int, exported: bool) -> int:
        kind = self.t(i)
        end = self._end(i)
        stop = end - 1 if self.t(end - 1) == ";" else end
        text = (self.toks[start].start, self.toks[end - 1].end)
        groups, k, a = [], i + 1, i + 1
        while k < stop:
            if self.t(k) == ",":
                groups.append((a, k))
                a = k + 1
                k += 1
            elif self.t(k) == "<" and self.ts and self._before_eq(a, k):
                k = self._skip_angle(k)
            else:
                k = self._skip(k)
        groups.append((a, stop))
        certain = self._certain_end(end)
        for a, b in groups:
            eq = self._find_eq(a, b)
            init = (eq + 1, b) if eq is not None else None
            names = [self.t(a)] if self.tk(a).kind == "id" else self._pattern_names(a)
            req = self._require(init)
            for nm in names:
                if req is not None:
                    self.vars[nm] = Var("require", init, text, req, "*" if self.tk(a).kind == "id" else self._pattern_source(a, nm))
                else:
                    self.vars[nm] = Var(kind, init, text)
                if exported:
                    self.local_exports.setdefault(nm, []).append(nm)
            if init and len(names) == 1 and self.tk(a).kind == "id" and kind in ("const", "let", "var"):
                fx = self._function_expr(init[0], init[1])
                if fx:
                    decl = (self.toks[a].start, self.toks[b - 1].end)
                    f = Func(names[0], names[0], "function", self.toks[i].line, fx["tokens"], decl, fx["async"], fx["gen"],
                             "const" if kind == "const" else "let", names[0], params=fx["params"], body=fx["body"],
                             stmt_end=certain if len(groups) == 1 else None)
                    if len(groups) > 1:
                        f.binding = "many"
                    self.funcs.append(f)
                    del self.vars[names[0]]
        return end

    def _before_eq(self, a: int, k: int) -> bool:
        return self._find_eq(a, k) is None

    def _find_eq(self, a: int, b: int) -> int | None:
        k = a
        while k < b:
            if self.t(k) == "=":
                return k
            k = self._skip(k)
        return None

    def _require(self, init) -> str | None:
        if init and self.t(init[0]) == "require" and self.t(init[0] + 1) == "(" and self.tk(init[0] + 2).kind == "str" \
                and self.pairs.get(init[0] + 1) == init[0] + 3 and "require" not in self.vars:
            return self.toks[init[0] + 2].text[1:-1]
        return None

    def _pattern_names(self, i: int) -> list[str]:
        out, k, close = [], i + 1, self.pairs[i]
        while k < close:
            t = self.toks[k]
            if t.text in ("{", "["):
                out += self._pattern_names(k)
                k = self.pairs[k] + 1
            elif t.text == "=":  # default value
                k += 1
                while k < close and self.t(k) != ",":
                    k = self._skip(k)
            elif t.kind == "id" and self.t(k + 1) != ":":
                out.append(t.text)
                k += 1
            else:
                k += 1
        return out

    def _pattern_source(self, i: int, local: str) -> str:
        for k in range(i + 1, self.pairs[i]):
            if self.t(k) == local and self.t(k - 1) == ":":
                return self.t(k - 2)
        return local

    def _class(self, start: int, i: int, exported: bool, default: bool) -> int:
        name = self.t(i + 1) if self.tk(i + 1) and self.tk(i + 1).kind == "id" and self.t(i + 1) not in ("extends", "implements") else ""
        k = i + 1
        while k < len(self.toks) and self.t(k) != "{":
            k = self._skip_angle(k) if self.t(k) == "<" else self._skip(k)
        if k >= len(self.toks):
            return self._end(i)
        close = self.pairs[k]
        rng = (self.toks[start].start, self.toks[close].end)
        if name:
            self.vars[name] = Var("class", None, rng)
            if exported:
                self.local_exports.setdefault(name, []).append("default" if default else name)
            self._methods(name, k, close)
        return close + 1

    def _methods(self, cls: str, open_: int, close: int):
        k = open_ + 1
        while k < close:
            static = is_async = gen = skip = False
            while self.t(k) == "@":
                k += 2
                while self.t(k) == ".":
                    k += 2
                if self.t(k) == "(":
                    k = self._skip(k)
            while self.t(k) in ("static", "async", "get", "set", "public", "private", "protected", "readonly", "override", "abstract",
                                "declare", "accessor") and self.t(k + 1) not in ("(", "=", ";", "<", ":", "?", "!"):
                static |= self.t(k) == "static"
                is_async |= self.t(k) == "async"
                skip |= self.t(k) in ("get", "set")
                k += 1
            if self.t(k) == "*":
                gen, k = True, k + 1
            first, name = k, self.t(k)
            if self.t(k) == "[":
                k, name = self._skip(k), ""
            elif self.t(k) == "{":  # static block
                k = self._skip(k)
                continue
            else:
                k += 1
            sig = self._signature(k) if self.t(k) in ("(", "<") else None
            if sig:
                if name and name.isidentifier() and name != "constructor" and not skip and not gen:
                    self.funcs.append(Func("%s.%s" % (cls, name), name, "method", self.toks[first].line, (first, sig[3]),
                                           (self.toks[first].start, self.toks[sig[3] - 1].end), is_async, gen, "method", "",
                                           params=(sig[0], sig[1]), body=(sig[2], sig[3]), owner=cls, static=static,
                                           stmt_end=self.toks[close].end))
                k = sig[3]
                continue
            while k < close and self.t(k) != ";":  # a field or a signature
                nxt = self._skip(k)
                if nxt < close and self.toks[nxt].nl and self._ends_between(nxt - 1, nxt):
                    k = nxt
                    break
                k = nxt
            else:
                k += 1

    def _cjs(self, i: int) -> int:
        """module.exports = ..., exports.name = ..., exports = module.exports = ..."""
        k, name = i, None
        while True:
            if self.t(k) == "module" and self.t(k + 1) == "." and self.t(k + 2) == "exports":
                k += 3
            elif self.t(k) == "exports":
                k += 1
            else:
                return self._end(i, braces=True)
            if self.t(k) == "." and self.tk(k + 1) and self.tk(k + 1).kind == "id":
                name, k = self.t(k + 1), k + 2
            if self.t(k) != "=":
                return self._end(i, braces=True)
            k += 1
            if self.t(k) not in ("module", "exports"):
                break
        end = self._end(i)
        stop = end - 1 if self.t(end - 1) == ";" else end
        self._cjs_assigned.append((name, k, stop, self._certain_end(end)))
        return end

    def _link_exports(self):
        local = {f.name: f for f in self.funcs if f.kind == "function" and f.accessor}
        if self.esm:
            for nm, outs in self.local_exports.items():
                if nm in local:
                    local[nm].exports += [o for o in outs if o not in local[nm].exports]
            for nm in self.default_values:
                if nm in local and not local[nm].exports:
                    local[nm].default_value = True
            return
        for name, a, b, certain in self._cjs_assigned:
            if name is None and self.t(a) == "{" and self.pairs[a] + 1 == b:
                self._cjs_object(a, certain, local)
            else:
                self._cjs_value(name, a, b, certain, local)

    def _cjs_value(self, name, a, b, certain, local):
        if b - a == 1 and self.t(a) in local:
            local[self.t(a)].exports.append(name or "module.exports")
            return
        fx = self._function_expr(a, b)
        if not fx:
            return
        fname = name or fx["name"]
        acc = "module.exports.%s" % name if name else "module.exports"
        f = Func(fname or "default", fname or "default", "function", self.toks[a].line, fx["tokens"],
                 (self.toks[a].start, self.toks[b - 1].end), fx["async"], fx["gen"], "inline" if fname else "anonymous",
                 acc, [name or "module.exports"], params=fx["params"], body=fx["body"], stmt_end=certain)
        if fname not in {g.qualname for g in self.funcs}:
            self.funcs.append(f)

    def _cjs_object(self, open_: int, certain, local):
        k, close = open_ + 1, self.pairs[open_]
        while k < close:
            a = k
            while k < close and self.t(k) != ",":
                k = self._skip(k)
            b = k
            k += 1
            if b - a == 1:
                if self.t(a) in local:
                    local[self.t(a)].exports.append(self.t(a))
                continue
            key = self.t(a).strip("\"'")
            if self.t(a + 1) == ":" and self.tk(a).kind in ("id", "str"):
                if key.isidentifier():
                    self._cjs_value(key, a + 2, b, certain, local)
                continue
            m, is_async, gen = a, False, False  # method shorthand
            if self.t(m) == "async" and self.t(m + 1) != "(":
                is_async, m = True, m + 1
            if self.t(m) == "*":
                gen, m = True, m + 1
            name = self.t(m)
            sig = self._signature(m + 1) if self.tk(m) and self.tk(m).kind == "id" and self.t(m + 1) in ("(", "<") else None
            if sig and sig[3] == b and name.isidentifier() and name not in {g.qualname for g in self.funcs}:
                f = Func(name, name, "function", self.toks[a].line, (m, b), (self.toks[m].start, self.toks[b - 1].end), is_async, gen,
                         "inline", "module.exports.%s" % name, [name], params=(sig[0], sig[1]), body=(sig[2], b), stmt_end=certain)
                f.kind = "function"
                f.owner = "shorthand"
                self.funcs.append(f)

    # ---------------------------------------------------------------- names a function uses

    def locals_of(self, f: Func) -> set[str]:
        out: set[str] = set()
        a, b = f.params
        if a == b:
            out.add(self.t(a))
        starts = ("(", ",", "{", "[", "...") if self.ts else ("(", ",", "{", "[", "...", ":")  # (TypeScript: a type follows ":")
        out |= {self.t(k) for k in range(a, b) if self.toks[k].kind == "id" and self.t(k - 1) in starts}
        for k in range(f.body[0], f.body[1]):
            w = self.t(k)
            if w in ("const", "let", "var") and self.tk(k + 1):
                if self.t(k + 1) in ("{", "["):
                    out |= set(self._pattern_names(k + 1))
                elif self.tk(k + 1).kind == "id":
                    out.add(self.t(k + 1))
            elif w in ("function", "class") and self.tk(k + 1) and self.tk(k + 1).kind == "id":
                out.add(self.t(k + 1))
            elif w == "catch" and self.t(k + 1) == "(" and self.tk(k + 2).kind == "id":
                out.add(self.t(k + 2))
            elif w == "=>":
                if self.t(k - 1) == ")":
                    p = self.pairs[k - 1]
                    out |= {self.t(m) for m in range(p + 1, k - 1) if self.toks[m].kind == "id" and self.t(m - 1) in starts}
                elif self.toks[k - 1].kind == "id":
                    out.add(self.t(k - 1))
        return out

    def names_in(self, f: Func) -> set[str]:
        """Identifiers f refers to (members, object keys and its own locals left out)."""
        mine = self.locals_of(f)
        out = set()
        first = f.tokens[0]
        while f.binding in ("const", "let") and self.toks[first - 1].start >= f.text[0]:  # its type annotation
            first -= 1
        for k in range(first, f.tokens[1]):
            t = self.toks[k]
            if t.kind != "id" or t.text in KEYWORDS or self.t(k - 1) in (".", "?."):
                continue
            if self.t(k + 1) == ":" and self.t(k - 1) in ("{", ","):
                continue
            out.add(t.text)
        return out - mine - {f.name}

    def uses(self, f: Func) -> dict:
        """What f needs to stand on its own: imports (statement ranges), scope (live module names),
        helpers (non-exported functions copied next to it), types."""
        imports, scope, types, helpers = {}, [], {}, []
        seen, todo = {f.qualname}, [f]
        while todo:
            g = todo.pop(0)
            for nm in sorted(self.names_in(g)):
                v = self.vars.get(nm)
                h = self.by_name.get(nm)
                if nm in self.types:
                    if nm not in types:
                        types[nm] = self.types[nm]
                        for x in self.types:
                            if x != nm and re.search(r"\b%s\b" % re.escape(x), self.src[slice(*self.types[nm])]):
                                types.setdefault(x, self.types[x])
                        for x, vv in self.vars.items():
                            if vv.kind == "import" and re.search(r"\b%s\b" % re.escape(x), self.src[slice(*self.types[nm])]):
                                imports[vv.text] = vv
                elif v is not None and v.kind in ("import", "require"):
                    imports[v.text] = v
                elif h is not None and h.kind == "function" and nm not in seen:
                    seen.add(nm)
                    if not h.exports and h.binding in ("function", "const", "let") and self.refusal(h, helper=True) is None:
                        helpers.append(h)
                        todo.append(h)
                    else:
                        scope.append(nm)
                elif v is not None and nm not in scope:
                    scope.append(nm)
        return {"imports": imports, "scope": sorted(scope), "helpers": helpers, "types": types}

    def state(self, f: Func) -> str | None:
        """A module-level name that f (or a module function it calls) reaches and the module changes at run time."""
        seen, todo = {f.qualname}, [(f, "")]
        while todo:
            g, via = todo.pop(0)
            for nm in sorted(self.names_in(g)):
                h = self.by_name.get(nm)
                if h is not None and nm not in seen:
                    seen.add(nm)
                    todo.append((h, via or nm))
                v = self.vars.get(nm)
                if v is not None and v.kind in ("const", "let", "var") and self._changes(nm, v):
                    return "`%s`%s" % (nm, " (through %s)" % via if via else "")
        return None

    @property
    def _fn_ranges(self):
        r = sorted(f.body for f in self.funcs if f.body != (0, 0))
        return r

    def _inside_function(self, k: int) -> bool:
        return any(a <= k < b for a, b in self._fn_ranges)

    def _changes(self, nm: str, v: Var) -> bool:
        for k, t in enumerate(self.toks):
            if t.kind != "id" or t.text != nm or self.t(k - 1) in (".", "?."):
                continue
            if self.t(k + 1) == ":" and self.t(k - 1) in ("{", ","):
                continue
            nxt = self.t(k + 1)
            if v.init and k == v.init[0] - 2:
                continue  # its own declaration
            if v.kind != "const":
                if (nxt in ASSIGN and self.t(k - 1) not in ("const", "let", "var")) or nxt in ("++", "--") or self.t(k - 1) in ("++", "--"):
                    return True
            if not self._inside_function(k):
                continue
            if nxt in (".", "?.") and self.t(k + 2) in MUTATORS and self.t(k + 3) == "(":
                return True
            m = k + 1
            while (self.t(m) == "." and self.tk(m + 1) and self.tk(m + 1).kind == "id") or self.t(m) == "[":
                m = m + 2 if self.t(m) == "." else self._skip(m)
            if m > k + 1 and (self.t(m) in ASSIGN or self.t(m) in ("++", "--")):
                return True
            if self.t(k - 1) == "delete" or (self.t(k - 1) == "(" and self.t(k - 2) == "assign" and self.t(k - 4) == "Object"):
                return True
        return False

    def refusal(self, f: Func, helper: bool = False) -> str | None:
        """Why f can't be swapped (or, helper: copied), from its source."""
        if f.kind == "method":
            return "class method (exported top-level functions only)"
        if f.binding == "anonymous":
            return "anonymous function (give it a name)"
        if f.gen:
            return "generator (not supported yet)"
        if not helper:
            if f.default_value:
                return "exported as a value (write `export default function %s`)" % f.name
            if not f.exports:
                return "not exported (exported top-level functions only)"
            if f.binding == "many":
                return "declared together with others in one `const` (split the statement)"
        words = [(k, self.t(k)) for k in range(f.tokens[0], f.tokens[1]) if self.toks[k].kind == "id"]
        for k, w in words:
            if w == "this" and self.t(k - 1) not in (".", "?."):
                return "uses `this`"
            if w == "super":
                return "uses `super`"
            if w == "meta" and self.t(k - 1) == "." and self.t(k - 2) in ("import", "new"):
                return "uses import.meta (move it to a module-level const)"
            if w == "target" and self.t(k - 1) == "." and self.t(k - 2) == "new":
                return "uses new.target"
            if w in ("__dirname", "__filename") and self.t(k - 1) not in (".", "?."):
                return "uses %s (move it to a module-level const)" % w
        if not helper:
            st = self.state(f)
            if st:
                return "closure state: the module changes %s at run time" % st
        return None

    # ---------------------------------------------------------------- text for the stub

    def rebased(self, start: int, end: int, up: str = "../") -> str:
        """src[start:end] with relative module specifiers as seen from a subfolder of the module's folder."""
        out, pos = [], start
        own_require = "require" in self.vars  # createRequire: resolves from the module itself
        for k, t in self._tokens_between(start, end):
            if t.kind != "str" or not re.match(r"\.{1,2}(/|$)", t.text[1:-1]):
                continue
            before = self.t(k - 1)
            if not (before in ("from", "import") or (before == "(" and (self.t(k - 2) == "import" or (self.t(k - 2) == "require" and not own_require)))):
                continue
            spec = t.text[1:-1]
            spec = up + spec[2:] if spec.startswith("./") else up + spec
            out += [self.src[pos:t.start], t.text[0] + spec.rstrip("/") + t.text[0]]
            pos = t.end
        out.append(self.src[pos:end])
        return "".join(out)

    def _tokens_between(self, start, end):
        i = bisect.bisect_left([t.start for t in self.toks], start)
        while i < len(self.toks) and self.toks[i].end <= end:
            yield i, self.toks[i]
            i += 1

    def function_text(self, f: Func) -> str:
        """f as a declaration of its own."""
        text = self.rebased(*f.text)
        if f.binding == "function":
            return text
        if f.owner == "shorthand":
            return "%sfunction%s %s" % ("async " if f.is_async else "", "*" if f.gen else "", text)
        if f.binding == "inline":
            return "const %s = %s;" % (f.name, text)
        return "const %s;" % text

    # ---------------------------------------------------------------- signals

    def callee(self, paren: int) -> tuple[str, int]:
        """Dotted name before the "(" at `paren`, and the token it starts at."""
        k, parts = paren - 1, []
        while k >= 0:
            t = self.toks[k]
            if t.kind == "id":
                parts.append(t.text)
                if self.t(k - 1) in (".", "?.") and self.tk(k - 2) and self.tk(k - 2).kind == "id":
                    k -= 2
                    continue
                if self.t(k - 1) in (".", "?."):
                    parts.append("")
            break
        return ".".join(reversed(parts)), k

    def origin(self, dotted: str) -> tuple[str, str]:
        """(module, name) a dotted callee comes from; ("", dotted) for globals and locals."""
        root, _, rest = dotted.partition(".")
        v = self.vars.get(root)
        if v is None or v.kind not in ("import", "require"):
            return "", dotted
        if v.imported in ("*", "default"):
            return v.source, rest or "default"
        return v.source, v.imported + ("." + rest if rest else "")

    def boundary_call(self, dotted: str) -> tuple[str, str] | None:
        """(kind, function) if calling this name crosses the boundary."""
        if not dotted:
            return None
        mod, name = self.origin(dotted)
        if mod == "" and dotted in ("fetch", "globalThis.fetch", "global.fetch") and "fetch" not in self.vars and "fetch" not in self.by_name:
            return "fetch", "fetch"
        if mod in HTTP_MODULES and name in ("request", "get"):
            return "http", "%s.%s" % (mod.split(":")[-1], name)
        if mod in CP_MODULES and name in CP_NAMES:
            return "subprocess", name
        if mod in CLIENT_MODULES:
            return "http", dotted
        v = self.vars.get(dotted)
        if v is not None and v.init and "promisify" in {self.t(k) for k in range(*v.init)}:
            for k in range(*v.init):
                if self.toks[k].kind == "id" and (self.t(k - 1) not in (".", "?.")):
                    full = self.t(k) + ("." + self.t(k + 2) if self.t(k + 1) == "." else "")
                    m2, n2 = self.origin(full)
                    if m2 in CP_MODULES and n2 in CP_NAMES:
                        return "subprocess", n2
        return None

    def analyze(self, f: Func) -> dict[str, str]:
        ranges = [f.tokens] + [h.tokens for h in self.uses(f)["helpers"]]
        found: dict[str, str] = {}
        calls = handlers = logs = retry = False
        words = {self.t(k).lower().strip("\"'") for a, b in ranges for k in range(a, b) if self.toks[k].kind in ("id", "str")}
        idem = any("idempotency" in w for w in words)
        sees = {"signal": "signal" in words, "timeout": "timeout" in words, "settimeout": "settimeout" in words}
        retry = any(re.search(r"retry|backoff", self.t(k), re.I) and self.t(k + 1) == "(" for a, b in ranges for k in range(a, b)
                    if self.toks[k].kind == "id")
        looped_write = None
        for a, b in ranges:
            loops = self._retry_loops(a, b)
            for k in range(a, b):
                if self.t(k) == "(":
                    name, first = self.callee(k)
                    if name.split(".")[0] in LOGGERS or name.startswith("process.stderr"):
                        logs = True
                    bc = self.boundary_call(name)
                    if bc is None:
                        continue
                    calls = True
                    kind, shown = bc
                    inner = [self.toks[m] for m in range(k + 1, self.pairs[k])]
                    txt = {t.text for t in inner}
                    inline = "{" in txt
                    if kind == "fetch":
                        timed = "signal" in txt or (not inline and self._args(k) > 1 and sees["signal"])
                    elif kind == "http":
                        timed = bool(txt & {"timeout", "signal"}) or sees["settimeout"] or (not inline and (sees["timeout"] or sees["signal"]))
                    else:
                        timed = bool(txt & {"timeout", "signal"}) or (not inline and (sees["timeout"] or sees["signal"]))
                    if not timed:
                        found.setdefault("no_timeout", "%s() without %s" % (shown, "a timeout (signal)" if kind == "fetch" else "timeout"))
                    verb = self._verb(k, a, b) if kind != "subprocess" else ""
                    in_loop = any(x <= k < y for x, y in loops)
                    retry = retry or in_loop
                    if verb in WRITE and not idem:
                        looped_write = verb if in_loop else looped_write
                        found.setdefault("write_no_key", SIGNALS["write_no_key"][1].format(verb=verb))
                elif self.t(k) == "catch" and self.t(k - 1) in (".", "?.") and self.t(k + 1) == "(":
                    handlers = True
                    if self._swallowing_callback(k + 1):
                        found["swallow"] = SIGNALS["swallow"][1].format(handler=".catch(%s)" % _brief(self.src[self.toks[k + 1].end:self.toks[self.pairs[k + 1]].start]))
                elif self.t(k) == "catch" and self.toks[k].kind == "id":
                    handlers = True
                    self._catch(k, found)
        if looped_write or ("write_no_key" in found and retry):
            verb = looped_write or found["write_no_key"].split()[0]
            found.pop("write_no_key", None)
            found["unsafe_retry"] = SIGNALS["unsafe_retry"][1].format(verb=verb)
        if calls and not retry:
            found["no_retry"] = SIGNALS["no_retry"][1]
        if (calls or handlers) and not logs:
            found["no_logging"] = SIGNALS["no_logging"][1]
        return found

    def _args(self, paren: int) -> int:
        n, k, close = 0, paren + 1, self.pairs[paren]
        if k < close:
            n = 1
        while k < close:
            if self.t(k) == ",":
                n += 1
            k = self._skip(k)
        return n

    def _verb(self, paren: int, a: int, b: int) -> str:
        name = self.callee(paren)[0].rsplit(".", 1)[-1].upper()
        for lo, hi in ((paren + 1, self.pairs[paren]), (a, b)):
            for k in range(lo, hi):
                if self.t(k) == "method" and self.t(k + 1) == ":" and self.tk(k + 2).kind == "str":
                    return self.t(k + 2)[1:-1].upper()
        return name if name in WRITE else "GET"

    def _retry_loops(self, a: int, b: int) -> list[tuple[int, int]]:
        """Loops whose body has a try: what they repeat is an attempt."""
        out = []
        for k in range(a, b):
            if self.t(k) in ("for", "while") and self.t(k - 1) not in (".", "?."):
                p = k + 1 if self.t(k + 1) == "(" else k + 2
                if self.t(p) != "(":
                    continue
                body = self.pairs[p] + 1
                if self.t(body) == "{":
                    end = self.pairs[body]
                    if any(self.t(m) == "try" for m in range(body, end)):
                        out.append((body, end))
            elif self.t(k) == "do" and self.t(k + 1) == "{" and any(self.t(m) == "try" for m in range(k + 1, self.pairs[k + 1])):
                out.append((k + 1, self.pairs[k + 1]))
        return out

    def _catch(self, k: int, found: dict):
        var, b = "", k + 1
        if self.t(b) == "(":
            var, b = self.t(b + 1), self.pairs[b] + 1
        if self.t(b) != "{":
            return
        close = self.pairs[b]
        if self._gone(b + 1, close, var):
            body = " ".join(self.src[self.toks[b].end:self.toks[close].start].split())
            found["swallow"] = SIGNALS["swallow"][1].format(handler="catch%s {%s}" % (" (%s)" % var if var else "", " %s " % _brief(body) if body else ""))
        for m in range(b, close):
            if self.t(m) == "throw" and self.t(m + 1) == "new":
                p = m + 2
                while p < close and self.t(p) not in ("(", ";"):
                    p += 1
                if self.t(p) == "(" and not any(self.t(x) == "cause" or self.t(x) == var for x in range(p, self.pairs[p])):
                    found["no_context"] = "re-throws without `cause`/context"

    def _gone(self, a: int, b: int, var: str) -> bool:
        """Does a handler body neither throw, nor log, nor do anything with the error?"""
        for m in range(a, b):
            if self.t(m) == "throw" or (var and self.t(m) == var and self.t(m - 1) not in (".", "?.")):
                return False
            if self.t(m) == "(":
                name = self.callee(m)[0]
                if name.split(".")[0] in LOGGERS or name.startswith("process.stderr") or name in ("reject", "process.exit"):
                    return False
        return True

    def _swallowing_callback(self, paren: int) -> bool:
        a, b = paren + 1, self.pairs[paren]
        arrow = next((m for m in range(a, b) if self.t(m) == "=>"), None)
        if arrow is None:
            if self.t(a) != "function":
                return False
            arrow = a
        p = next((m for m in range(a, arrow + 2) if self.t(m) == "("), None)
        at = p + 1 if p is not None else a
        var = self.t(at) if self.toks[at].kind == "id" else ""
        return self._gone(arrow + 1, b, var)


def _brief(s: str) -> str:
    s = " ".join(s.split())
    return s if len(s) <= 40 else s[:39] + "…"


# --------------------------------------------------------------------------- files


@lru_cache(maxsize=512)
def package_type(folder: str) -> str:
    """"type" of the nearest package.json."""
    p = os.path.join(folder, "package.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return str(json.load(f).get("type") or "commonjs")
        except (OSError, ValueError, AttributeError):
            return "commonjs"
    up = os.path.dirname(folder)
    return "commonjs" if up == folder else package_type(up)


@lru_cache(maxsize=2048)
def _load(path: str, mtime: int, size: int) -> FileInfo | None:
    try:
        return FileInfo(path)
    except (LexError, OSError, UnicodeDecodeError, KeyError, IndexError, AttributeError):
        return None  # not understood: the app runs, this file just isn't looked into


def load(path: str) -> FileInfo | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_size > MAX_FILE:
        return None
    return _load(str(path), st.st_mtime_ns, st.st_size)


def project_files(root: str) -> list[str]:
    out = []
    for base, dirs, files in os.walk(os.path.realpath(root)):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.endswith(EXTS) and not name.endswith((".d.ts", ".d.mts", ".d.cts", ".min.js")):
                out.append(os.path.join(base, name))
                if len(out) >= MAX_FILES:
                    return out
    return out


def module_id(path: str, root: str) -> str:
    """"src/api/orders.mjs" -> "src.api.orders"; a file next to one with the same stem keeps its extension."""
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    stem, ext = os.path.splitext(rel)
    if any(e != ext and os.path.exists(os.path.splitext(path)[0] + e) for e in EXTS):
        stem += "_" + ext[1:]
    return re.sub(r"[^\w.$-]", "_", stem.replace(os.sep, "."))


# --------------------------------------------------------------------------- instrumented sources


def instrument(fi: FileInfo, root: str, only: set[str] | None = None, over: set[str] = frozenset()) -> dict | None:
    """The module's source with its functions wrapped by the agent, and the table of those functions.
    Line numbers stay the same. `over`: functions wrapped after the module's own code ran (bench: over the enable block)."""
    mod = module_id(fi.path, root)
    table, edits, hoisted, tail = [], [], [], []
    for f in fi.funcs:
        if only is not None and f.qualname not in only:
            continue
        if f.binding == "anonymous" and not f.accessor or f.gen:
            continue
        n, at_end = len(table), f.qualname in over
        if f.kind == "method":
            code = "$w.method(%d,%s,%s,%s);" % (n, f.owner, json.dumps(f.name), "true" if f.static else "false")
        elif at_end and not fi.esm:
            code = "$w.swap(%d,%s,module,$f=>{%s=$f});" % (n, f.accessor, f.accessor)
        else:
            code = "%s=$w(%d,%s);" % (f.accessor, n, f.accessor)
        if at_end:
            tail.append(code)
        elif f.binding == "function":
            hoisted.append(code)
        elif f.stmt_end is not None and f.binding != "many":
            edits.append((f.stmt_end, 0, _guard(fi, code)))
        else:
            continue
        if f.binding == "const":
            at = _const_at(fi, f)
            if at is None:
                if at_end:
                    tail.pop()
                else:
                    edits.pop()
                continue
            edits.append((at, 5, "let  "))
        refused = fi.refusal(f)
        table.append({"qualname": f.qualname, "name": f.name, "line": f.line, "end": fi.toks[f.tokens[1] - 1].line,
                      "async": f.is_async, "kind": f.kind,
                      "exported": bool(f.exports) and f.kind == "function", "hardenable": refused is None, "module": mod})
    if not table:
        return None
    if hoisted:
        edits.append((fi.prologue, 0, _guard(fi, "".join(hoisted))))
    src = fi.src
    for at, cut, text in sorted(edits, key=lambda e: (-e[0], -e[1])):
        src = src[:at] + text + src[at + cut:]
    if tail:
        src += "\n" + _guard(fi, "".join(tail))
    return {"sha": fi.sha, "source": src, "functions": table}


def _guard(fi: FileInfo, code: str) -> str:
    return ";{const $w=globalThis[Symbol.for(\"ras-anything.agent\")]?.(%s);if($w){%s}}" % (json.dumps(os.path.realpath(fi.path)), code)


def _const_at(fi: FileInfo, f: Func) -> int | None:
    """Offset of the `const` keyword that declares f (None when it isn't right before the name)."""
    m = re.search(r"const(?=\s+$)", fi.src[max(0, f.text[0] - 40):f.text[0]])
    return max(0, f.text[0] - 40) + m.start() if m else None


def scope_object(names: list[str]) -> str:
    return "{ %s}" % "".join("get %s() { return %s; }, " % (n, n) for n in names)


def with_scope(fi: FileInfo, f: Func, fid: str) -> dict:
    """The module's source, also handing the replay worker the original function and the names it uses."""
    names = fi.uses(f)["scope"]
    tail = "\n;globalThis[Symbol.for(\"ras-anything.scope\")]?.(%s,%s,()=>%s);\n" % (json.dumps(fid), scope_object(names), f.accessor)
    return {"sha": fi.sha, "source": fi.src + tail, "functions": []}


# --------------------------------------------------------------------------- hardened code

FORBIDDEN_MODULES = {"module", "vm", "worker_threads", "inspector", "inspector/promises", "repl", "v8", "cluster", "async_hooks",
                     "diagnostics_channel"}


def forbidden(path) -> list[str]:
    """What hardened code must not touch: ways into the harness that judges it."""
    try:
        src = Path(path).read_text(encoding="utf-8")
        toks, _ = tokenize(src)
    except (OSError, UnicodeDecodeError, LexError):
        return []  # the worker reports it
    out = set()

    def text(i):
        return toks[i].text if 0 <= i < len(toks) else ""

    for i, t in enumerate(toks):
        if t.kind in ("str", "tpl"):
            s = t.text.strip("\"'`")
            bare = s[5:] if s.startswith("node:") else s
            if bare in FORBIDDEN_MODULES and text(i - 1) in ("from", "import", "("):
                out.add("imports %s" % s)
            if re.search(r"node_agent|ras_anything|ras-anything\.(agent|scope|swap)|RAS_ANYTHING_", s):
                out.add("mentions %s" % s[:40])
        elif t.kind == "id":
            if t.text in ("eval", "Function") and text(i + 1) == "(" and text(i - 1) not in (".", "function"):
                out.add("%s()" % t.text)
            if t.text in ("getBuiltinModule", "_linkedBinding", "dlopen", "mainModule") or (t.text == "binding" and text(i - 2) == "process"):
                out.add("process.%s" % t.text)
            if t.text == "cache" and text(i - 1) == "." and text(i - 2) == "require":
                out.add("require.cache")
            if t.text in ("fetch", "Date", "setTimeout", "AbortSignal") and text(i - 1) == "." and text(i - 2) in ("globalThis", "global") and text(i + 1) == "=":
                out.add("replaces %s" % t.text)
    return sorted(out)
