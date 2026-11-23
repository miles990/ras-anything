"""Cassettes: recorded calls as plain JSON (`calls.json`), whatever language recorded them.

Values are JSON, plus single-key tag objects for what JSON lacks ({"$bytes": "<base64>"},
{"$tuple": [...]}, ...; the full list is in docs/ARCHITECTURE.md). A dict whose keys are all strings
not starting with "$" is a plain JSON object; any other dict is {"$dict": [[key, value], ...]}.

Strings containing the project's folder are stored as {"$proj": [mark, text]} (the mark in the text
stands for wherever the project is when the cassette is loaded), so cassettes hold no
machine-specific paths and still replay after the project moves.

This module only reads, writes and displays the JSON; turning values back into objects is the job of
each language's agent (agents/python/values.py for Python).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

FORMAT = "ras-anything-cassette"
VERSION = 1


class BadCassette(Exception):
    """The file isn't a cassette this version can read."""


def save(path, calls: list, lang: str) -> None:
    doc = {"format": FORMAT, "version": VERSION, "lang": lang, "calls": calls}
    Path(path).write_text(json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def load(path) -> list:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise BadCassette("%s isn't JSON: %s" % (path, e)) from e
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or not isinstance(doc.get("calls"), list):
        raise BadCassette("%s isn't a ras-anything cassette" % path)
    if doc.get("version") != VERSION:
        raise BadCassette("%s is cassette version %s; this ras-anything reads version %d (run harden again)"
                          % (path, doc.get("version"), VERSION))
    return doc["calls"]


def exc_name(e: dict) -> str:
    """"urllib.error.URLError" for a recorded exception (languages without modules for them: "TypeError")."""
    return e["type"] if e.get("module") in (None, "", "node", "thrown") else "%s.%s" % (e["module"], e["type"])


_SHOW = {"$tuple": "(%s)", "$set": "{%s}", "$frozenset": "frozenset({%s})"}


def show(j, n: int = 240) -> str:
    """An encoded value as short readable text, without decoding it."""
    s = _show(j)
    return s if len(s) <= n else s[: n - 1] + "…"


def _show(j) -> str:
    if isinstance(j, list):
        return "[%s]" % ", ".join(map(_show, j))
    if not isinstance(j, dict):
        return repr(j)
    if len(j) == 1:
        tag, v = next(iter(j.items()))
        if tag in _SHOW:
            return _SHOW[tag] % (", ".join(map(_show, v)) + ("," if tag == "$tuple" and len(v) == 1 else ""))
        if tag == "$proj":
            return repr(v[1])
        if tag == "$bytes":
            return repr(base64.b64decode(v))
        if tag in ("$dict", "$odict"):
            return "{%s}" % ", ".join("%s: %s" % (_show(k), _show(x)) for k, x in v)
        if tag == "$object":
            return "%s(%s)" % (v[1], ", ".join("%s=%s" % (k, _show(x)) for k, x in v[2]))
        if tag in ("$class", "$enum"):
            return ".".join(v[1:])
        if tag == "$namedtuple":
            return "%s(%s)" % (v[1], ", ".join(map(_show, v[2])))
        if tag == "$ext":
            return "<%s>" % v[0]
        if tag.startswith("$"):
            return "%s(%s)" % (tag[1:], _show(v))
    return "{%s}" % ", ".join("%r: %s" % (k, _show(x)) for k, x in j.items())


def text(j, project) -> str:
    """A stored string (plain or {"$proj": ...}) with the project's folder filled in."""
    if isinstance(j, dict) and "$proj" in j:
        mark, s = j["$proj"]
        return s.replace(mark, str(project))
    return str(j)


def dependencies(calls: list, project=None) -> list[dict]:
    """Distinct dependencies seen in recorded calls, formatted for runtime.observed / observe."""
    seen: dict[tuple, dict] = {}
    for c in calls or ():
        for x in c.get("io") or ():
            req = x.get("req") or {}
            kind = req.get("kind") or ("http" if req.get("url") else "subprocess")
            if req.get("url"):
                url = text(req["url"], project) if project is not None else str(req["url"])
                u = urlsplit(url)
                route = u.path or "/"
                segs = [seg for seg in route.split("/") if seg]
                name = segs[-1] if segs else (u.hostname or "http")
                method = req.get("method") or "GET"
                k = ("http", method, route)
                seen.setdefault(k, {"name": name, "kind": "http", "method": method, "route": route, "program": None})
            elif req.get("argv"):
                prog = os.path.basename(text(req["argv"][0], project) if project is not None else str(req["argv"][0]))
                method = req.get("method") or "RUN"
                k = ("subprocess", method, prog)
                seen.setdefault(k, {"name": prog, "kind": "subprocess", "method": method, "route": None, "program": prog})
    return sorted(seen.values(), key=lambda d: (-len(d.get("route") or ""), d["name"], d["method"] or ""))
