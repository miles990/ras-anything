"""Interception of the dependency boundary, in the app's process:

    urllib.request.urlopen · subprocess.run · requests.Session.request · httpx.Client/AsyncClient.send ·
    aiohttp.ClientSession._request · socket.create_connection (connection faults only)

The third-party clients are patched when the app imports them, so nothing is loaded that the app
doesn't use. Every intercepted call is normalized into a `Req` and handed to the active `Handler`
together with a `serve` callable that produces the real outcome. Outcomes are plain dicts (so they
can be recorded into a cassette and replayed):

    http clients: {"status", "reason", "headers": [[k, v]...], "body": bytes, "url"}
    subprocess:   {"returncode", "stdout", "stderr"}
    any:          {"exc": {"type", "module", "msg", ...}}   (see values.dump_exc)

Keys starting with "_" ("_live": the original exception object, "_socket") serve running code and are
never recorded. The patch then turns the outcome back into exactly what the API returns or raises.
"""

from __future__ import annotations

import errno
import contextvars
import datetime
import hashlib
import importlib.abc
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

from ... import faults, runtime
from . import values

NONIDEMPOTENT = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_orig_urlopen = urllib.request.urlopen
_orig_run = subprocess.run
_orig_create_connection = socket.create_connection
_orig: dict = {}  # originals of the third-party client methods
_installed = False
_busy: contextvars.ContextVar[bool] = contextvars.ContextVar("ras_busy", default=False)


class ReplayMiss(BaseException):
    """The code made a dependency request the recording never saw (BaseException: can't be hidden)."""


# --------------------------------------------------------------------------- requests and outcomes


def norm_subproc_key(argv, cwd: str | None = None) -> str:
    parts = argv if isinstance(argv, list) else [str(argv)]
    norm = []
    base = cwd or os.getcwd()
    for idx, part in enumerate(parts):
        s = str(part)
        if idx == 0 and os.path.basename(s).startswith("python"):
            norm.append("python")
        elif os.path.isabs(s):
            try:
                rel = os.path.relpath(s, base)
                norm.append(rel if not rel.startswith("..") else s)
            except ValueError:
                norm.append(s)
        else:
            norm.append(s)
    return "run " + " ".join(norm)


class Req:
    __slots__ = ("kind", "method", "url", "body_sha", "headers", "timeout", "argv", "text")

    def __init__(self, kind, method="", url="", body=None, headers=None, timeout=None, argv=None, text=False):
        self.kind = kind
        self.method = (method or "").upper()
        self.url = url or ""
        self.body_sha = _sha(body)
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self.timeout = timeout
        self.argv = argv
        self.text = text

    @property
    def key(self) -> str:
        if self.kind == "subprocess":
            return norm_subproc_key(self.argv)
        if self.kind == "socket":
            return "CONNECT " + self.url
        return "%s %s%s" % (self.method, _logical_url(self.url), (" #" + self.body_sha[:12]) if self.body_sha else "")

    @property
    def nonidempotent(self) -> bool:
        return self.method in NONIDEMPOTENT

    @property
    def idem_key(self):
        return self.headers.get("idempotency-key")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "method": self.method, "url": self.url, "body_sha": self.body_sha,
                "idempotency_key": self.idem_key, "timeout": self.timeout, "argv": _portable_argv(self.argv),
                "key": self.key}


def _portable_argv(argv):
    """argv for the record: which python runs a script is this machine's business."""
    if isinstance(argv, list) and argv and os.path.basename(argv[0]).startswith("python"):
        return ["python"] + argv[1:]
    return argv


def _logical_url(url: str) -> str:
    """The URL without its port: local test upstreams bind a fresh port each run, and the same
    logical request must get the same fault (and match the same recording) across runs."""
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname or ""
        return urllib.parse.urlunsplit((u.scheme, host, u.path, u.query, ""))
    except ValueError:
        return url


def _sha(body) -> str:
    if body is None:
        return ""
    if isinstance(body, str):
        body = body.encode()
    if not isinstance(body, (bytes, bytearray)):
        body = repr(body).encode()
    return hashlib.sha256(bytes(body)).hexdigest() if body else ""


def exc_outcome(e: BaseException) -> dict:
    return {"exc": values.dump_exc(e), "_live": e}


def recorded(out: dict) -> dict:
    """The outcome as it goes into a cassette."""
    return {k: v for k, v in out.items() if not k.startswith("_")}


def raise_outcome(out: dict):
    raise out.get("_live") or values.build_exc(out["exc"])


def is_failure(out: dict) -> bool:
    """Did the dependency fail in this outcome? (exception or timeout, 5xx/429, a temp-failed or killed process)"""
    if "exc" in out:
        return True
    if "status" in out:
        return out["status"] >= 500 or out["status"] == 429
    rc = out.get("returncode")
    return rc is not None and (rc == 75 or rc < 0)


# --------------------------------------------------------------------------- handlers


class Handler:
    """Passes calls through. Subclasses look on (before/after), inject faults, or replay."""

    def before(self, req: Req) -> None:
        pass

    def after(self, req: Req, out: dict, dur: float) -> None:
        pass

    def perform(self, req: Req, serve):
        self.before(req)
        out, dur = serve()
        self.after(req, out, dur)
        return out

    async def aperform(self, req: Req, serve):
        self.before(req)
        out, dur = await serve()
        self.after(req, out, dur)
        return out


def _live_serve(fn):
    def serve():
        t0 = time.perf_counter()
        out = fn()
        return out, time.perf_counter() - t0
    return serve


# One list per wrapped call in progress in this context; every attempt is added to all of them.
_scopes: contextvars.ContextVar[tuple] = contextvars.ContextVar("ras_scopes", default=())


class FaultingHandler(Handler):
    """Injects the faults a `Plan` picks, on a live or virtual clock.

    `serve()` returns (outcome, seconds): live serve does the real call (its time already spent);
    replay serve returns a recorded outcome whose recorded latency the virtual clock then adds.
    """

    def __init__(self, plan: faults.Plan, clock):
        self.plan = plan
        self.clock = clock
        self.reset()

    def reset(self):
        self.counts: dict[str, int] = {}
        self.attempts: list[dict] = []
        self.hangs = 0

    def on_attempt(self, req: Req, rec: dict) -> None:  # hook for attribution / counting
        pass

    def perform(self, req, serve):
        rec, steps = self._open(req)
        result = None
        try:
            while True:
                op, arg = steps.send(result)
                if op == "serve":
                    result = serve()
                elif op == "wait":
                    result = self.clock.wait(arg)
                else:
                    self.hangs += 1
                    self.clock.hang()
        except StopIteration as e:
            return self._close(rec, e.value)

    async def aperform(self, req, serve):
        rec, steps = self._open(req)
        result = None
        while True:
            try:
                op, arg = steps.send(result)
            except StopIteration as e:
                return self._close(rec, e.value)
            if op == "serve":
                result = await serve()
            elif op == "wait":
                result = await self.clock.await_(arg)
            else:
                await self.clock.await_hang()  # a wait_for around it may cut this short: then it's no hang
                self.hangs += 1
                raise faults.ChaosHang("boundary call with no timeout hung (chaos)")

    def _open(self, req):
        n = self.counts.get(req.key, 0)
        self.counts[req.key] = n + 1
        fault = self.plan.fault(req.key, n, req.nonidempotent)
        if req.kind == "socket" and fault in ("http5xx", "http429", "truncated"):
            fault = None  # nothing a connection can do
        rec = {"key": req.key, "n": n, "fault": fault, "reached": False, "nonidempotent": req.nonidempotent,
               "idem": req.idem_key, "method": req.method, "failed": True, "outage": self.plan.outage(req.key, n)}
        self.attempts.append(rec)
        for calls in _scopes.get():
            calls.append(rec)
        self.on_attempt(req, rec)
        return rec, self._steps(fault, req, rec)

    def _close(self, rec, out):  # a hang never gets here: failed stays True
        rec["failed"] = is_failure(out) or bool(rec.get("truncated"))
        return out

    def _timeout(self, req):
        t = req.timeout
        rem = runtime.enforced_remaining_s()
        if rem is not None and (t is None or rem < t):
            return rem, True
        return t, False

    # What happens to one attempt, as steps the sync or async loop above carries out:
    # ("serve", None) -> (outcome, seconds) · ("wait", seconds) · ("hang", None).

    def _steps(self, fault, req, rec):
        t, by_deadline = self._timeout(req)
        if fault is None:
            return (yield from self._answer(rec, 0.0, t, by_deadline, req))
        if fault == "slow":
            return (yield from self._answer(rec, faults.SLOW_S, t, by_deadline, req))
        if fault == "truncated":
            whole = yield from self._answer(rec, 0.0, t, by_deadline, req)
            out = truncate(whole)
            rec["truncated"] = out != whole
            return out
        if fault == "timeout":
            if req.nonidempotent:  # the request went out; only the answer is lost
                yield "serve", None
                rec["reached"] = True
            if t is None:
                rec["hang"] = True
                yield "hang", None
            yield "wait", t
            return exc_outcome(timeout_exc(req, t, by_deadline))
        if fault == "refused":
            return exc_outcome(refused_exc(req))
        if fault in ("http5xx", "http429"):
            return error_outcome(req, 503 if fault == "http5xx" else 429)
        raise ValueError("unknown fault %r" % fault)

    def _answer(self, rec, extra, t, by_deadline, req):
        if self.clock.virtual:
            out, dur = yield "serve", None
            rec["reached"] = True
            total = extra + (dur or 0.0)
            if t is not None and total > t:
                yield "wait", t
                return exc_outcome(timeout_exc(req, t, by_deadline))
            yield "wait", total
            return out
        if extra:
            if t is not None and t < extra:
                if req.nonidempotent:
                    yield "serve", None
                    rec["reached"] = True
                yield "wait", t
                return exc_outcome(timeout_exc(req, t, by_deadline))
            yield "wait", extra
        out, _ = yield "serve", None
        rec["reached"] = True
        return out


def truncate(out: dict) -> dict:
    out = dict(out)
    if "body" in out and out["body"]:
        out["body"] = out["body"][: len(out["body"]) // 2]
    elif "stdout" in out and out["stdout"]:
        out["stdout"] = out["stdout"][: len(out["stdout"]) // 2]
    return out


def timeout_exc(req: Req, t, by_deadline=False) -> BaseException:
    if by_deadline:
        return TimeoutError("deadline exceeded after %.3gs" % (t or 0))
    if req.kind == "subprocess":
        return subprocess.TimeoutExpired(req.argv, t)
    if req.kind == "requests":
        import requests

        return requests.exceptions.ReadTimeout("Read timed out. (read timeout=%s)" % t)
    if req.kind == "httpx":
        import httpx

        return httpx.ReadTimeout("The read operation timed out")
    if req.kind == "aiohttp":
        import asyncio

        return asyncio.TimeoutError()
    if req.kind == "socket":
        return socket.timeout("timed out")
    return urllib.error.URLError(socket.timeout("timed out"))


def refused_exc(req: Req) -> BaseException:
    if req.kind == "subprocess":
        return BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable (fork)")
    if req.kind == "requests":
        import requests

        return requests.exceptions.ConnectionError("Connection refused: %s" % req.url)
    if req.kind == "httpx":
        import httpx

        return httpx.ConnectError("[Errno %d] Connection refused" % errno.ECONNREFUSED)
    if req.kind == "aiohttp":
        u = urllib.parse.urlsplit(req.url)
        return values.aiohttp_connector_error(u.hostname, u.port, errno.ECONNREFUSED, "Connect call failed")
    if req.kind == "socket":
        return ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
    return urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))


def error_outcome(req: Req, status: int) -> dict:
    if req.kind == "subprocess":
        empty = "" if req.text else b""
        msg = "temporarily unavailable (exit 75)\n"
        return {"returncode": 75, "stdout": empty, "stderr": msg if req.text else msg.encode()}
    reason = "Service Unavailable" if status == 503 else "Too Many Requests"
    headers = [["Content-Type", "application/json"]]
    if status == 429:
        headers.append(["Retry-After", faults.RETRY_AFTER])
    return {"status": status, "reason": reason, "headers": headers,
            "body": b'{"error": "%s"}' % reason.lower().encode(), "url": req.url}


# --------------------------------------------------------------------------- install


_handler: Handler = Handler()


def install(handler: Handler) -> None:
    """Route every boundary call through `handler` (patches are installed once per process)."""
    global _handler, _installed
    _handler = handler
    if _installed:
        return
    _installed = True
    urllib.request.urlopen = _urlopen
    subprocess.run = _run
    socket.create_connection = _create_connection
    on_import("requests", _patch_requests)
    on_import("httpx", _patch_httpx)
    on_import("aiohttp", _patch_aiohttp)


def on_import(name: str, patch) -> None:
    """Run patch(module) once the module is imported (now, if it already is)."""
    if name in sys.modules:
        return patch(sys.modules[name])
    sys.meta_path.insert(0, _PatchOnImport(name, patch))


class _PatchOnImport(importlib.abc.MetaPathFinder):
    def __init__(self, name, patch):
        self.name, self.patch, self.searching = name, patch, False

    def find_spec(self, name, path, target=None):
        if name != self.name or self.searching:
            return None
        self.searching = True  # let the other finders answer
        try:
            spec = importlib.util.find_spec(name)
        finally:
            self.searching = False
        if spec is None or not hasattr(spec.loader, "exec_module"):
            return spec
        run = spec.loader.exec_module

        def exec_module(module):
            run(module)
            try:
                self.patch(module)
            except Exception as e:  # an unfamiliar version: leave the app alone, say so
                sys.stderr.write("ras-anything: can't watch %s: %r\n" % (name, e))

        spec.loader.exec_module = exec_module
        return spec


def set_handler(handler: Handler) -> None:
    global _handler
    _handler = handler


def _dispatch(req: Req, real) -> dict:
    if _busy.get():  # a boundary call made by a boundary call: pass through
        return real()
    token = _busy.set(True)
    try:
        return _handler.perform(req, _live_serve(real))
    finally:
        _busy.reset(token)


async def _adispatch(req: Req, real) -> dict:
    if _busy.get():
        return await real()

    async def serve():
        t0 = time.perf_counter()
        out = await real()
        return out, time.perf_counter() - t0

    token = _busy.set(True)
    try:
        return await _handler.aperform(req, serve)
    finally:
        _busy.reset(token)


# urllib ------------------------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    """What urlopen returns: enough of http.client.HTTPResponse for real code."""

    def __init__(self, out: dict):
        super().__init__(out.get("body") or b"")
        self.status = self.code = out.get("status", 200)
        self.reason = self.msg = out.get("reason", "OK")
        self.headers = _headers(out.get("headers"))
        self.url = out.get("url", "")

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def info(self):
        return self.headers

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def getheaders(self):
        return list(self.headers.items())


def _pairs(items) -> list:
    return [[str(k), str(v)] for k, v in items]


def _headers(pairs) -> Message:
    m = Message()
    for k, v in pairs or []:
        m[k] = v
    return m


def _urlopen(url, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *args, **kwargs):
    if isinstance(url, urllib.request.Request):
        method, full, body = url.get_method(), url.full_url, url.data if data is None else data
        headers = dict(url.header_items())
        if data is not None and url.data is None:
            method = "POST"
    else:
        full, body, headers = str(url), data, {}
        method = "POST" if data is not None else "GET"
    t = socket.getdefaulttimeout() if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
    req = Req("http", method, full, body, headers, t)

    def real():
        try:
            resp = _orig_urlopen(url, data, timeout, *args, **kwargs)
            try:
                body_ = resp.read()
                return {"status": resp.status, "reason": resp.reason, "headers": _pairs(resp.headers.items()),
                        "body": body_, "url": resp.geturl()}
            finally:
                resp.close()
        except urllib.error.HTTPError as e:
            try:
                b = e.read()
            except Exception:
                b = b""
            return {"status": e.code, "reason": str(e.reason), "headers": _pairs((e.headers or {}).items()),
                    "body": b, "url": e.url or full}
        except Exception as e:
            return exc_outcome(e)

    out = _dispatch(req, real)
    if "exc" in out:
        raise_outcome(out)
    if out["status"] >= 400:
        raise urllib.error.HTTPError(out.get("url") or full, out["status"], out.get("reason", ""),
                                     _headers(out.get("headers")), io.BytesIO(out.get("body") or b""))
    return FakeResponse(out)


# subprocess --------------------------------------------------------------------------------------


def _run(*popenargs, **kwargs):
    args = popenargs[0] if popenargs else kwargs.get("args")
    argv = [os.fspath(a) if not isinstance(a, (str, bytes)) else (a.decode() if isinstance(a, bytes) else a)
            for a in args] if isinstance(args, (list, tuple)) else str(args)
    text = bool(kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding") or kwargs.get("errors"))
    req = Req("subprocess", "RUN", "", None, None, kwargs.get("timeout"), argv, text)
    check = kwargs.pop("check", False)

    def real():
        try:
            cp = _orig_run(*popenargs, **kwargs)
            return {"returncode": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr}
        except Exception as e:
            return exc_outcome(e)

    out = _dispatch(req, real)
    if "exc" in out:
        raise_outcome(out)
    cp = subprocess.CompletedProcess(args, out["returncode"], out.get("stdout"), out.get("stderr"))
    if check and cp.returncode:
        raise subprocess.CalledProcessError(cp.returncode, args, cp.stdout, cp.stderr)
    return cp


# requests ----------------------------------------------------------------------------------------


def _patch_requests(requests):
    _orig["requests"] = requests.Session.request
    requests.Session.request = _requests_request


def _requests_request(self, method, url, *args, **kwargs):
    import requests

    names = ("params", "data", "headers", "cookies", "files", "auth", "timeout", "allow_redirects", "proxies",
             "hooks", "stream", "verify", "cert", "json")
    kw = dict(kwargs)
    for name, value in zip(names, args):
        kw[name] = value
    prep = self.prepare_request(requests.Request(method=method.upper(), url=url, params=kw.get("params"),
                                                 data=kw.get("data"), json=kw.get("json"), headers=kw.get("headers"),
                                                 files=kw.get("files"), auth=kw.get("auth"), cookies=kw.get("cookies")))
    t = kw.get("timeout")
    if isinstance(t, tuple):
        t = sum(x for x in t if x is not None) if any(x is not None for x in t) else None
    req = Req("requests", method, prep.url, prep.body, dict(prep.headers), t)

    def real():
        try:
            r = _orig["requests"](self, method, url, *args, **kwargs)
            return {"status": r.status_code, "reason": r.reason, "headers": _pairs(r.headers.items()),
                    "body": r.content, "url": r.url}
        except Exception as e:
            return exc_outcome(e)

    out = _dispatch(req, real)
    if "exc" in out:
        raise_outcome(out)
    r = requests.Response()
    r.status_code = out["status"]
    r.reason = out.get("reason", "")
    r._content = out.get("body") or b""
    r.headers = requests.structures.CaseInsensitiveDict(dict(out.get("headers") or []))
    r.url = out.get("url") or prep.url
    r.request = prep
    r.encoding = None
    return r


# httpx -------------------------------------------------------------------------------------------


def _patch_httpx(httpx):
    _orig["httpx"], _orig["httpx-async"] = httpx.Client.send, httpx.AsyncClient.send
    httpx.Client.send, httpx.AsyncClient.send = _httpx_send, _httpx_asend


def _httpx_req(request, body) -> Req:
    t = request.extensions.get("timeout") or {}
    return Req("httpx", request.method, str(request.url), body, dict(request.headers), t.get("read"))


def _httpx_outcome(r) -> dict:
    return {"status": r.status_code, "reason": r.reason_phrase, "headers": _decoded(r.headers.multi_items()),
            "body": r.content, "url": str(r.url)}


def _decoded(pairs) -> list:
    """Headers of a response whose body is already decoded and complete."""
    return [[str(k), str(v)] for k, v in pairs if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]


def _httpx_result(out, request):
    import httpx

    if "exc" in out:
        e = out.get("_live") or values.build_exc(out["exc"])
        if isinstance(e, httpx.RequestError):
            e.request = request
        raise e
    r = httpx.Response(out["status"], headers=[tuple(h) for h in out.get("headers") or []], content=out.get("body") or b"",
                       request=request, extensions={"http_version": b"HTTP/1.1", "reason_phrase": (out.get("reason") or "").encode()})
    r.elapsed = datetime.timedelta(0)
    return r


def _httpx_send(self, request, *, stream=False, **kw):
    def real():
        try:
            return _httpx_outcome(_orig["httpx"](self, request, **kw))
        except Exception as e:
            return exc_outcome(e)

    return _httpx_result(_dispatch(_httpx_req(request, request.read()), real), request)


async def _httpx_asend(self, request, *, stream=False, **kw):
    async def real():
        try:
            return _httpx_outcome(await _orig["httpx-async"](self, request, **kw))
        except Exception as e:
            return exc_outcome(e)

    return _httpx_result(await _adispatch(_httpx_req(request, await request.aread()), real), request)


# aiohttp -----------------------------------------------------------------------------------------


def _patch_aiohttp(aiohttp):
    from . import fake_aiohttp

    _orig["aiohttp"] = aiohttp.ClientSession._request
    aiohttp.ClientSession._request = fake_aiohttp.request


# sockets -----------------------------------------------------------------------------------------


def _create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *args, **kwargs):
    if _busy.get():
        return _orig_create_connection(address, timeout, *args, **kwargs)
    host, port = str(address[0]), address[1]
    where = host if host in ("localhost", "::1") or host.startswith("127.") else "%s:%s" % (host, port)
    t = socket.getdefaulttimeout() if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
    req = Req("socket", "CONNECT", where, None, None, t)

    def real():
        try:
            return {"_socket": _orig_create_connection(address, timeout, *args, **kwargs)}
        except Exception as e:
            return exc_outcome(e)

    out = _dispatch(req, real)
    if "exc" in out:
        raise_outcome(out)
    return out["_socket"]
