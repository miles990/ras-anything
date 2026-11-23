"""Interception of the dependency boundary: urllib.request.urlopen, subprocess.run and (if
installed) requests.Session.request.

Every intercepted call is normalized into a `Req` and handed to the active `Handler` together with
a `serve` callable that produces the real outcome. Outcomes are plain dicts (so they can be
recorded into a cassette and replayed):

    http:       {"status", "reason", "headers": [[k, v]...], "body": bytes, "url"}
    subprocess: {"returncode", "stdout", "stderr"}
    any:        {"exc": {"type", "module", "msg", ...}}   (see cassette.dump_exc)

A live exception also travels as out["_live"] so running code gets the original object; it is never
recorded. The adapter then turns the outcome back into exactly what the API returns or raises.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

from . import cassette, faults, runtime

NONIDEMPOTENT = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_orig_urlopen = urllib.request.urlopen
_orig_run = subprocess.run
_orig_requests = None
_installed = False
_tls = threading.local()


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
        return "%s %s%s" % (self.method, _logical_url(self.url), (" #" + self.body_sha[:12]) if self.body_sha else "")

    @property
    def nonidempotent(self) -> bool:
        return self.kind != "subprocess" and self.method in NONIDEMPOTENT

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
    return {"exc": cassette.dump_exc(e), "_live": e}


def recorded(out: dict) -> dict:
    """The outcome as it goes into a cassette."""
    return {k: v for k, v in out.items() if k != "_live"}


def raise_outcome(out: dict):
    raise out.get("_live") or cassette.build_exc(out["exc"])


# --------------------------------------------------------------------------- handlers


class Handler:
    """Pass-through. Subclasses decide faults, record, or replay."""

    def perform(self, req: Req, serve):
        out, _dur = serve()
        return out


def _live_serve(fn):
    def serve():
        t0 = time.perf_counter()
        out = fn()
        return out, time.perf_counter() - t0
    return serve


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
        n = self.counts.get(req.key, 0)
        self.counts[req.key] = n + 1
        fault = self.plan.fault(req.key, n, req.nonidempotent)
        rec = {"key": req.key, "n": n, "fault": fault, "reached": False, "nonidempotent": req.nonidempotent,
               "idem": req.idem_key, "method": req.method}
        self.attempts.append(rec)
        self.on_attempt(req, rec)
        return self.execute(fault, req, serve, rec)

    def _timeout(self, req):
        t = req.timeout
        rem = runtime.remaining_s()
        if rem is not None and (t is None or rem < t):
            return rem, True
        return t, False

    def _serve(self, serve, rec, extra=0.0, t=None, by_deadline=False, req=None):
        if self.clock.virtual:
            out, dur = serve()
            rec["reached"] = True
            total = extra + (dur or 0.0)
            if t is not None and total > t:
                self.clock.wait(t)
                return exc_outcome(timeout_exc(req, t, by_deadline))
            self.clock.wait(total)
            return out
        if extra:
            if t is not None and t < extra:
                if req.nonidempotent:
                    serve()
                    rec["reached"] = True
                self.clock.wait(t)
                return exc_outcome(timeout_exc(req, t, by_deadline))
            self.clock.wait(extra)
        out, _ = serve()
        rec["reached"] = True
        return out

    def execute(self, fault, req, serve, rec):
        t, by_deadline = self._timeout(req)
        if fault is None:
            return self._serve(serve, rec, 0.0, t, by_deadline, req)
        if fault == "slow":
            return self._serve(serve, rec, faults.SLOW_S, t, by_deadline, req)
        if fault == "truncated":
            out = self._serve(serve, rec, 0.0, t, by_deadline, req)
            return truncate(out)
        if fault == "timeout":
            if req.nonidempotent:  # the request went out; only the answer is lost
                serve()
                rec["reached"] = True
            if t is None:
                self.hangs += 1
                rec["hang"] = True
                self.clock.hang()
            self.clock.wait(t)
            return exc_outcome(timeout_exc(req, t, by_deadline))
        if fault == "refused":
            return exc_outcome(refused_exc(req))
        if fault in ("http5xx", "http429"):
            return error_outcome(req, 503 if fault == "http5xx" else 429)
        raise ValueError("unknown fault %r" % fault)


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
    return urllib.error.URLError(socket.timeout("timed out"))


def refused_exc(req: Req) -> BaseException:
    if req.kind == "subprocess":
        return BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable (fork)")
    if req.kind == "requests":
        import requests

        return requests.exceptions.ConnectionError("Connection refused: %s" % req.url)
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
    global _handler, _installed, _orig_requests
    _handler = handler
    if _installed:
        return
    _installed = True
    urllib.request.urlopen = _urlopen
    subprocess.run = _run
    try:
        import requests  # noqa: F401
        import requests.sessions

        _orig_requests = requests.sessions.Session.request
        requests.sessions.Session.request = _requests_request
    except Exception:  # requests is optional
        _orig_requests = None


def set_handler(handler: Handler) -> None:
    global _handler
    _handler = handler


def _dispatch(req: Req, real) -> dict:
    if getattr(_tls, "busy", False):  # a boundary call made by a boundary call: pass through
        return real()
    _tls.busy = True
    try:
        return _handler.perform(req, _live_serve(real))
    finally:
        _tls.busy = False


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
                return {"status": resp.status, "reason": resp.reason, "headers": [list(h) for h in resp.headers.items()],
                        "body": body_, "url": resp.geturl()}
            finally:
                resp.close()
        except urllib.error.HTTPError as e:
            try:
                b = e.read()
            except Exception:
                b = b""
            return {"status": e.code, "reason": str(e.reason), "headers": [list(h) for h in (e.headers or {}).items()],
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
            r = _orig_requests(self, method, url, *args, **kwargs)
            return {"status": r.status_code, "reason": r.reason, "headers": [list(h) for h in r.headers.items()],
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
