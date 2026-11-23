"""aiohttp.ClientSession._request, intercepted (imported only once the app has imported aiohttp)."""

from __future__ import annotations

import asyncio
import urllib.parse

import aiohttp
from aiohttp import ClientResponse, RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from . import boundary


class _Content:
    """What `resp.content` offers, over a body that is already here."""

    def __init__(self, body: bytes):
        self._b, self._i = body, 0

    def at_eof(self):
        return self._i >= len(self._b)

    async def read(self, n=-1):
        j = len(self._b) if n is None or n < 0 else min(len(self._b), self._i + n)
        out, self._i = self._b[self._i:j], j
        return out

    async def readany(self):
        return await self.read()

    async def readexactly(self, n):
        out = await self.read(n)
        if len(out) < n:
            raise asyncio.IncompleteReadError(out, n)
        return out

    async def readline(self):
        j = self._b.find(b"\n", self._i)
        return await self.read((j + 1 if j >= 0 else len(self._b)) - self._i)

    async def iter_chunked(self, n):
        while not self.at_eof():
            yield await self.read(n)

    async def iter_any(self):
        if not self.at_eof():
            yield await self.read()

    async def __aiter__(self):
        while not self.at_eof():
            yield await self.readline()


class Response(ClientResponse):
    """A finished ClientResponse: status, headers and the whole body, no connection."""

    def __init__(self, method, url: URL, out: dict, request_headers):
        real = URL(out.get("url") or str(url))
        self.method = method
        self.status, self.reason = out["status"], out.get("reason") or ""
        self._real_url, self._url = real, real.with_fragment(None)
        self._cache = {}
        self._headers = CIMultiDictProxy(CIMultiDict([tuple(h) for h in out.get("headers") or []]))
        self._raw_headers = tuple((k.encode(), v.encode()) for k, v in self._headers.items())
        self._request_info = RequestInfo(url, method, CIMultiDictProxy(CIMultiDict(request_headers)), url)
        self._body = out.get("body") or b""
        self._loop = asyncio.get_running_loop()
        self.content = _Content(self._body)

    def __del__(self, *_):
        pass

    async def read(self):
        return self._body

    def release(self):
        return _done()

    def close(self):
        pass

    async def wait_for_close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


class _done:
    def __await__(self):
        return iter(())


def _body_of(session, kw):
    if kw.get("json") is not None:
        data = session._json_serialize(kw["json"])
        return data if isinstance(data, bytes) else data.encode()
    data = kw.get("data")
    if data is None or isinstance(data, (bytes, bytearray, str)):
        return data
    if isinstance(data, dict):
        return urllib.parse.urlencode(sorted((str(k), str(v)) for k, v in data.items()))
    return "<%s>" % type(data).__name__  # a stream or form: its content can't be read twice


def _timeout_of(session, kw):
    t = kw.get("timeout", session.timeout)
    if isinstance(t, (int, float)) or t is None:
        return t
    if not isinstance(t, aiohttp.ClientTimeout):
        t = session.timeout
    return t.total if t.total else t.sock_read


async def request(self, method, str_or_url, **kw):
    url = self._build_url(str_or_url) if hasattr(self, "_build_url") else URL(str_or_url)
    if kw.get("params"):
        url = url.update_query(kw["params"])
    headers = CIMultiDict(self.headers)
    headers.update(kw.get("headers") or {})
    if "Upgrade" in headers or "Sec-WebSocket-Key" in headers:  # a websocket: not a request/response
        return await boundary._orig["aiohttp"](self, method, str_or_url, **kw)
    req = boundary.Req("aiohttp", method, str(url), _body_of(self, kw), dict(headers), _timeout_of(self, kw))

    async def real():
        try:
            r = await boundary._orig["aiohttp"](self, method, str_or_url, **{**kw, "raise_for_status": False})
            body = await r.read()
            return {"status": r.status, "reason": r.reason, "headers": boundary._decoded(r.headers.items()), "body": body,
                    "url": str(r.url)}
        except Exception as e:
            return boundary.exc_outcome(e)

    out = await boundary._adispatch(req, real)
    if "exc" in out:
        boundary.raise_outcome(out)
    resp = Response(method.upper(), url, out, headers)
    check = kw.get("raise_for_status")
    check = self._raise_for_status if check is None else check
    if callable(check):
        await check(resp)
    elif check:
        resp.raise_for_status()
    return resp
