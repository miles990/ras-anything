"""Python values and exceptions in cassettes (the tags are documented in docs/ARCHITECTURE.md).

Nothing here imports a module or calls a constructor that the file names: exceptions come from a
fixed allowlist (anything else becomes a RemoteError carrying the name), and instances of the
project's own classes are rebuilt as bare objects (no __init__) from classes the caller looks up.
"""

from __future__ import annotations

import base64
import collections
import datetime
import decimal
import enum
import importlib
import json
import logging
import pathlib
import re
import threading
import types
import uuid
from pathlib import Path

from ...cassette import BadCassette

MAX_DEPTH = 60
MAX_NODES = 500_000


class Unsupported(Exception):
    """A value that can't be stored in a cassette."""


class RemoteError(Exception):
    """Stands in for a recorded exception whose type isn't in the allowlist."""

    def __init__(self, type_name: str, msg: str = ""):
        super().__init__("%s: %s" % (type_name, msg) if msg else type_name)
        self.type_name, self.msg = type_name, msg


# --------------------------------------------------------------------------- values


_LOCK, _RLOCK = type(threading.Lock()), type(threading.RLock())
# never stored, even when a client object carries them as default headers
SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"}


class Codec:
    """Turns values into JSON and back.

    project     folder whose path is made relocatable inside strings
    owns(cls)   encoder: may instances of this class be stored by their attributes?
    find(m, q)  decoder: the class `q` of module `m` (raise LookupError to refuse)
    """

    def __init__(self, project: str | None = None, owns=None, find=None):
        self.project = str(project) if project and len(str(project)) > 3 else None
        self._proj_re = re.compile(re.escape(self.project) + r"(?![\w.\-])") if self.project else None
        self.owns = owns or (lambda cls: False)
        self.find = find

    # ---------------------------------------------------------------- encode

    def enc(self, v):
        self._n = 0
        return self._enc(v, 0)

    def _enc(self, v, d):
        self._n += 1
        if d > MAX_DEPTH or self._n > MAX_NODES:
            raise Unsupported("value is nested too deep (or refers to itself) or too large")
        t = type(v)
        if v is None or t is bool or t is int:
            return v
        if t is str:
            if self._proj_re is not None and self.project in v:
                mark = "${PROJECT}"
                while mark in v:
                    mark = "$" + mark
                new = self._proj_re.sub(lambda m: mark, v)
                if new != v:
                    return {"$proj": [mark, new]}
            try:
                v.encode("utf-8")
            except UnicodeEncodeError:
                return {"$str": base64.b64encode(v.encode("utf-8", "surrogatepass")).decode()}
            return v
        if t is float:
            return v if v == v and v not in (float("inf"), float("-inf")) else {"$float": repr(v)}
        if t is list:
            return [self._enc(x, d + 1) for x in v]
        if t is dict:
            if all(type(k) is str and not k.startswith("$") for k in v) and not (
                    self.project and any(self.project in k for k in v)):
                return {k: self._enc(x, d + 1) for k, x in v.items()}
            return {"$dict": self._pairs(v, d)}
        if t is tuple:
            return {"$tuple": [self._enc(x, d + 1) for x in v]}
        if t is bytes:
            return {"$bytes": base64.b64encode(v).decode()}
        if t is bytearray:
            return {"$bytearray": base64.b64encode(bytes(v)).decode()}
        if t in (set, frozenset):
            items = sorted((self._enc(x, d + 1) for x in v), key=lambda x: json.dumps(x, sort_keys=True))
            return {"$set" if t is set else "$frozenset": items}
        if t is complex:
            return {"$complex": [self._enc(v.real, d + 1), self._enc(v.imag, d + 1)]}
        if t is collections.OrderedDict:
            return {"$odict": self._pairs(v, d)}
        if t in (datetime.datetime, datetime.time):
            if v.tzinfo is not None and type(v.tzinfo) is not datetime.timezone:
                raise Unsupported("a %s with a named time zone" % t.__name__)
            return {"$" + t.__name__: v.isoformat()}
        if t is datetime.date:
            return {"$date": v.isoformat()}
        if t is datetime.timedelta:
            return {"$timedelta": [v.days, v.seconds, v.microseconds]}
        if t is decimal.Decimal:
            return {"$decimal": str(v)}
        if t is uuid.UUID:
            return {"$uuid": str(v)}
        if isinstance(v, pathlib.PurePath) and t.__module__.startswith("pathlib"):
            return {"$path": self._enc(str(v), d + 1)}
        ext = _ext_state(v)
        if ext is not None:
            return {"$ext": [ext[0], self._enc(ext[1], d + 1)]}
        if isinstance(v, type):
            if self.owns(v):
                return {"$class": [v.__module__, v.__qualname__]}
            raise Unsupported("the class %s.%s" % (v.__module__, v.__qualname__))
        if not self.owns(t) or "<locals>" in t.__qualname__:
            raise Unsupported("a %s.%s" % (t.__module__, t.__qualname__))
        if isinstance(v, enum.Enum):
            return {"$enum": [t.__module__, t.__qualname__, v.name]}
        if isinstance(v, tuple) and hasattr(t, "_fields"):
            return {"$namedtuple": [t.__module__, t.__qualname__, [self._enc(x, d + 1) for x in v]]}
        if _builtin_layout(t):
            raise Unsupported("a %s.%s (extends a built-in type)" % (t.__module__, t.__qualname__))
        state = {}
        for name in _slots(t):
            if hasattr(v, name):
                state[name] = getattr(v, name)
        state.update(getattr(v, "__dict__", {}))
        return {"$object": [t.__module__, t.__qualname__, [[k, self._enc(x, d + 1)] for k, x in state.items()]]}

    def _pairs(self, v, d):
        return [[self._enc(k, d + 1), self._enc(x, d + 1)] for k, x in v.items()]

    def can(self, v) -> bool:
        try:
            self.enc(v)
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- decode

    def dec(self, j):
        if isinstance(j, list):
            return [self.dec(x) for x in j]
        if not isinstance(j, dict):
            return j
        if len(j) == 1:
            tag, v = next(iter(j.items()))
            if tag.startswith("$"):
                fn = _DECODERS.get(tag)
                if fn is None:
                    raise BadCassette("unknown tag %r in the cassette" % tag)
                return fn(self, v)
        return {k: self.dec(x) for k, x in j.items()}

    def _class(self, module, qualname):
        if self.find is None:
            raise BadCassette("the cassette holds an instance of %s.%s" % (module, qualname))
        try:
            cls = self.find(module, qualname)
        except LookupError as e:
            raise BadCassette("can't find the class %s.%s: %s" % (module, qualname, e)) from e
        if not isinstance(cls, type):
            raise BadCassette("%s.%s isn't a class" % (module, qualname))
        return cls

    def _object(self, v):
        module, qualname, state = v
        cls = self._class(module, qualname)
        obj = object.__new__(cls)  # no __init__ / __new__ of the class runs
        slots = set(_slots(cls))
        for k, x in state:
            if k in slots:
                getattr_static(cls, k).__set__(obj, self.dec(x))
            else:
                obj.__dict__[k] = self.dec(x)
        return obj

    def _enum(self, v):
        cls = self._class(v[0], v[1])
        if not issubclass(cls, enum.Enum):
            raise BadCassette("%s.%s isn't an Enum" % (v[0], v[1]))
        return cls.__members__[v[2]]

    def _namedtuple(self, v):
        cls = self._class(v[0], v[1])
        if not (issubclass(cls, tuple) and hasattr(cls, "_fields")):
            raise BadCassette("%s.%s isn't a namedtuple" % (v[0], v[1]))
        return tuple.__new__(cls, [self.dec(x) for x in v[2]])

    def _ext(self, v):
        name, state = v[0], self.dec(v[1])
        if name not in _EXT:
            raise BadCassette("unknown external object %r in the cassette" % (name,))
        return _EXT[name](state)


_DECODERS = {
    "$proj": lambda c, v: v[1].replace(v[0], c.project or "."),
    "$str": lambda c, v: base64.b64decode(v).decode("utf-8", "surrogatepass"),
    "$float": lambda c, v: float(v),
    "$dict": lambda c, v: {c.dec(k): c.dec(x) for k, x in v},
    "$odict": lambda c, v: collections.OrderedDict((c.dec(k), c.dec(x)) for k, x in v),
    "$tuple": lambda c, v: tuple(c.dec(x) for x in v),
    "$bytes": lambda c, v: base64.b64decode(v),
    "$bytearray": lambda c, v: bytearray(base64.b64decode(v)),
    "$set": lambda c, v: {c.dec(x) for x in v},
    "$frozenset": lambda c, v: frozenset(c.dec(x) for x in v),
    "$complex": lambda c, v: complex(c.dec(v[0]), c.dec(v[1])),
    "$datetime": lambda c, v: datetime.datetime.fromisoformat(v),
    "$time": lambda c, v: datetime.time.fromisoformat(v),
    "$date": lambda c, v: datetime.date.fromisoformat(v),
    "$timedelta": lambda c, v: datetime.timedelta(*v),
    "$decimal": lambda c, v: decimal.Decimal(v),
    "$uuid": lambda c, v: uuid.UUID(v),
    "$path": lambda c, v: Path(c.dec(v)),
    "$class": lambda c, v: c._class(v[0], v[1]),
    "$enum": lambda c, v: c._enum(v),
    "$namedtuple": lambda c, v: c._namedtuple(v),
    "$object": lambda c, v: c._object(v),
    "$ext": lambda c, v: c._ext(v),
}


def getattr_static(cls, name):
    for c in cls.__mro__:
        if name in c.__dict__:
            return c.__dict__[name]
    raise AttributeError(name)


def _slots(cls) -> list[str]:
    return [n for c in cls.__mro__ for n, m in c.__dict__.items()
            if isinstance(m, types.MemberDescriptorType) and c.__module__ != "builtins"]


def _builtin_layout(cls) -> bool:
    return any(issubclass(cls, b) for b in (dict, list, tuple, set, frozenset, str, bytes, bytearray, int, float,
                                            complex, BaseException, type))


# objects that are plumbing rather than data: stored by kind, recreated fresh


def _ext_state(v):
    t = type(v)
    if t is _LOCK:
        return "threading.Lock", {}
    if t is _RLOCK:
        return "threading.RLock", {}
    if isinstance(v, logging.Logger):
        return "logging.Logger", {"name": v.name}
    name = "%s.%s" % (t.__module__, t.__qualname__)
    if name == "requests.sessions.Session":
        return "requests.Session", {"headers": _public(v.headers)}
    if name == "httpx.Client":
        return "httpx.Client", {"headers": _public(v.headers), "base_url": str(v.base_url),
                                "timeout": v.timeout.as_dict()}
    return None


def _public(headers) -> dict:
    return {str(k): str(x) for k, x in dict(headers).items() if str(k).lower() not in SECRET_HEADERS}


def _new_requests_session(state):
    import requests

    s = requests.Session()
    s.headers.update(state.get("headers") or {})
    return s


def _new_httpx_client(state):
    import httpx

    return httpx.Client(headers=state.get("headers"), base_url=state.get("base_url") or "",
                        timeout=httpx.Timeout(**state["timeout"]))


_EXT = {
    "threading.Lock": lambda s: threading.Lock(),
    "threading.RLock": lambda s: threading.RLock(),
    "logging.Logger": lambda s: logging.getLogger(s.get("name")),
    "requests.Session": _new_requests_session,
    "httpx.Client": _new_httpx_client,
}


# --------------------------------------------------------------------------- exceptions


# Where a recorded exception's type may come from. The only modules this file ever imports by name.
EXC_MODULES = frozenset({
    "builtins", "socket", "ssl", "urllib.error", "http.client", "subprocess", "json.decoder", "asyncio.exceptions",
    "requests.exceptions", "httpx", "aiohttp.client_exceptions",
})


def exc_name(e: BaseException | type) -> tuple[str, str]:
    t = e if isinstance(e, type) else type(e)
    return t.__module__, t.__qualname__


def dump_exc(e: BaseException, depth: int = 0) -> dict:
    """{"type", "module", "msg"} plus the few fields some types need to be rebuilt faithfully."""
    if isinstance(e, RemoteError):
        mod, _, name = e.type_name.rpartition(".")
        return {"type": name, "module": mod, "msg": e.msg}
    mod, name = exc_name(e)
    d: dict = {"type": name, "module": mod, "msg": str(e)}
    if isinstance(e, OSError) and isinstance(e.errno, int):
        d["errno"] = e.errno
        d["msg"] = e.strerror if isinstance(e.strerror, str) else str(e)
    reason = getattr(e, "reason", None)
    if mod == "urllib.error" and name == "URLError" and depth < 4:
        d["reason"] = dump_exc(reason, depth + 1) if isinstance(reason, BaseException) else str(reason)
    key = getattr(e, "_conn_key", None)
    if mod == "aiohttp.client_exceptions" and key is not None:
        d["host"], d["port"] = getattr(key, "host", None), getattr(key, "port", None)
    if mod == "subprocess":
        d["cmd"] = [str(x) for x in e.cmd] if isinstance(e.cmd, (list, tuple)) else str(e.cmd)
        for k in ("timeout", "returncode"):
            if isinstance(getattr(e, k, None), (int, float)):
                d[k] = getattr(e, k)
    return d


def exc_type(d: dict) -> type | None:
    """The exception class a record names, if it is in the allowlist."""
    mod, name = str(d.get("module") or ""), str(d.get("type") or "")
    if mod not in EXC_MODULES or not name.isidentifier():
        return None
    try:
        cls = getattr(importlib.import_module(mod), name, None)
    except ImportError:  # requests/httpx/aiohttp not installed here
        return None
    return cls if isinstance(cls, type) and issubclass(cls, Exception) else None


def aiohttp_connector_error(host, port, code: int, msg: str, cls=None):
    import aiohttp

    key = types.SimpleNamespace(host=host, port=port, ssl=True, is_ssl=False)
    return (cls or aiohttp.ClientConnectorError)(key, OSError(code, msg))


def build_exc(d: dict) -> Exception:
    """The exception a record describes; a RemoteError when its type isn't in the allowlist."""
    full = "%s.%s" % (d.get("module"), d.get("type"))
    cls, msg = exc_type(d), str(d.get("msg") or "")
    if cls is None:
        return RemoteError(full, msg)
    import subprocess
    import urllib.error

    try:
        if cls is urllib.error.URLError:
            r = d.get("reason")
            return urllib.error.URLError(build_exc(r) if isinstance(r, dict) else str(r if r is not None else msg))
        if cls is urllib.error.HTTPError:
            return RemoteError(full, msg)
        if cls is subprocess.TimeoutExpired:
            return cls(d.get("cmd") or "", d.get("timeout") or 0)
        if cls is subprocess.CalledProcessError:
            return cls(d.get("returncode") or 1, d.get("cmd") or "")
        if "host" in d and d.get("module") == "aiohttp.client_exceptions":
            return aiohttp_connector_error(d["host"], d.get("port"), d.get("errno") or 0, msg, cls)
        if cls is json.JSONDecodeError:
            return cls(msg, "", 0)
        if issubclass(cls, OSError) and isinstance(d.get("errno"), int):
            return cls(d["errno"], msg)
        return cls(msg)
    except Exception:  # an unusual constructor: keep the name and the message
        return RemoteError(full, msg)


def matches(exc: BaseException, d: dict, aliases: dict | None = None) -> bool:
    """Is `exc` of the recorded type (or a subclass)? aliases: {recorded module: module here}."""
    mod = (aliases or {}).get(d.get("module"), d.get("module"))
    if isinstance(exc, RemoteError):
        return exc.type_name == "%s.%s" % (d.get("module"), d.get("type"))
    return any(exc_name(c) == (mod, d.get("type")) for c in type(exc).__mro__)
