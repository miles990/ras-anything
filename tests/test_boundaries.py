"""Each dependency boundary end to end (scan -> record -> verify -> enable -> bench), offline, against a
local http.server; and methods as hardening targets."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from ras_anything.bench import bench
from ras_anything.enable import disable, enable
from ras_anything.harden import harden
from ras_anything.scan import scan
from ras_anything.verify import verify
from tests.test_pipeline import Project, quiet

UPSTREAM = '''
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        sent = self.rfile.read(n).decode()
        body = json.dumps({"path": self.path, "method": self.command, "sent": sent}).encode()
        self.send_response(404 if "missing" in self.path else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _go

    def log_message(self, *a):
        pass


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1]
'''

APP = '''
import os
import upstream
import lib


def main():
    os.environ["API"] = upstream.start()
    for i in range(4):
        print(%s)


if __name__ == "__main__":
    main()
'''


def have(*mods):
    return all(importlib.util.find_spec(m) for m in mods)


class Case(unittest.TestCase):
    def project(self, lib: str, call: str = "lib.fetch(i)") -> Project:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "app"
        src.mkdir()
        (src / "upstream.py").write_text(UPSTREAM)
        (src / "lib.py").write_text(textwrap.dedent(lib))
        (src / "app.py").write_text(APP % call)
        p = Project(src)
        self.addCleanup(p.close)
        return p

    def scan(self, p, **kw):
        with quiet():
            res = scan([sys.executable, "app.py"], seeds=kw.get("seeds", 2), rate=kw.get("rate", 0.5), quiet=True)
        return res, {f["id"]: f for f in res["functions"]}

    def verify(self, p, fid, code):
        hp = p.dir / ".ras" / "hardens" / fid / "hardened.py"
        hp.write_text(textwrap.dedent(code))
        with quiet():
            return verify(fid, quiet=True)

    def loop(self, p, fid, kind, good, unchanged):
        """scan sees the boundary; the recording replays with no network; the original is rejected; a correct
        hardening passes, runs when enabled, and comes out byte for byte."""
        _, fns = self.scan(p)
        f = fns[fid]
        self.assertTrue(f["hardenable"], f["verdict"])
        self.assertEqual(f["boundary_kinds"], {kind: f["boundary"]})
        self.assertGreater(f["chaos"]["failed"], 0)
        with quiet():
            r = harden(fid, quiet=True)
        self.assertEqual(r["recorded"], 4)
        v = self.verify(p, fid, unchanged)
        self.assertTrue(v.get("gates", {}).get("happy"), v.get("fatal") or v["happy"])
        self.assertFalse(v["passed"])
        self.assertGreater(v["original"]["hang"] + v["original"]["error"], 0)
        v = self.verify(p, fid, good)
        self.assertTrue(v["passed"], v.get("fatal") or (v["gates"], v["chaos"]["first_failure"], v["happy"]["first_failure"]))
        before = (p.dir / "lib.py").read_bytes()
        clean = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True)
        with quiet():
            enable(fid, quiet=True)
        on = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True, env={**os.environ, "API": ""})
        self.assertEqual(on.stdout, clean.stdout)
        self.assertNotIn("not loaded", on.stderr)
        with quiet():
            b = bench(seeds=3, rate=0.3, quiet=True)
        self.assertTrue(b["clean_outputs_equal"])
        st = b["functions"]["hardened"][fid]
        self.assertGreaterEqual(st["calls"], 4)
        self.assertGreater(b["summary"]["hardened"]["availability"], b["summary"]["original"]["availability"])
        with quiet():
            disable(fid, quiet=True)
        self.assertEqual((p.dir / "lib.py").read_bytes(), before)
        return v


SYNC_GOOD = '''
    import os
    {imp}
    from ras_anything.runtime import is_transient, retry, log_event

    RAS_FALLBACK = lambda i: {{"unavailable": i}}


    def fetch(i):
        def once():
            {body}
        try:
            return retry(once, attempts=5, base=0.05, cap=0.4, budget_s=4.0, retry_on=lambda e: is_transient(e) or isinstance(e, ValueError))
        except Exception as e:
            if not (is_transient(e) or isinstance(e, ValueError)):
                raise
            log_event("unavailable", i=i, error=repr(e))
            return RAS_FALLBACK(i)
    '''

ASYNC_GOOD = '''
    import os
    {imp}
    from ras_anything.runtime import aretry, is_transient, log_event

    RAS_FALLBACK = lambda i: {{"unavailable": i}}


    async def fetch(i):
        async def once():
            {body}
        try:
            return await aretry(once, attempts=5, base=0.05, cap=0.4, budget_s=4.0,
                                retry_on=lambda e: is_transient(e) or isinstance(e, ValueError))
        except Exception as e:
            if not (is_transient(e) or isinstance(e, ValueError)):
                raise
            log_event("unavailable", i=i, error=repr(e))
            return RAS_FALLBACK(i)
    '''


@unittest.skipUnless(have("requests"), "requests not installed")
class RequestsTest(Case):
    LIB = '''
        import os
        import requests


        def fetch(i):
            r = requests.post(os.environ["API"] + "/x", params={"i": i}, json={"n": i}, headers={"Idempotency-Key": "k%d" % i})
            r.raise_for_status()
            return r.json()
        '''

    def test_loop(self):
        self.loop(self.project(self.LIB), "lib.fetch", "requests", SYNC_GOOD.format(imp="import requests", body='''
            r = requests.post(os.environ["API"] + "/x", params={"i": i}, json={"n": i}, headers={"Idempotency-Key": "k%d" % i}, timeout=0.8)
            r.raise_for_status()
            return r.json()'''.strip()), self.LIB)


@unittest.skipUnless(have("httpx"), "httpx not installed")
class HttpxTest(Case):
    LIB = '''
        import os
        import httpx


        def fetch(i):
            r = httpx.post(os.environ["API"] + "/x", params={"i": i}, json={"n": i}, headers={"Idempotency-Key": "k%d" % i}, timeout=None)
            r.raise_for_status()
            return r.json()
        '''

    def test_sync_loop(self):
        self.loop(self.project(self.LIB), "lib.fetch", "httpx", SYNC_GOOD.format(imp="import httpx", body='''
            r = httpx.post(os.environ["API"] + "/x", params={"i": i}, json={"n": i}, headers={"Idempotency-Key": "k%d" % i}, timeout=0.8)
            r.raise_for_status()
            return r.json()'''.strip()), self.LIB)

    ALIB = '''
        import os
        import httpx


        async def fetch(i):
            async with httpx.AsyncClient() as c:
                r = await c.get(os.environ["API"] + "/x", params={"i": i})
                r.raise_for_status()
                return r.json()
        '''

    def test_async_loop(self):
        v = self.loop(self.project(self.ALIB, "__import__('asyncio').run(lib.fetch(i))"), "lib.fetch", "httpx",
                      ASYNC_GOOD.format(imp="import httpx", body='''
            async with httpx.AsyncClient(timeout=0.8) as c:
                r = await c.get(os.environ["API"] + "/x", params={"i": i})
                r.raise_for_status()
                return r.json()'''.strip()), self.ALIB)
        self.assertLess(v["wall_s"], 60)

    def test_streaming_and_error_status_behave_like_httpx(self):
        p = self.project('''
            import os
            import httpx


            def fetch(i):
                with httpx.Client(base_url=os.environ["API"]) as c:
                    with c.stream("GET", "/s%d" % i) as r:
                        body = b"".join(r.iter_bytes())
                    try:
                        c.get("/missing").raise_for_status()
                    except httpx.HTTPStatusError as e:
                        return [body.decode(), e.response.status_code, str(e.request.url.path)]
            ''')
        plain = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True)
        _, fns = self.scan(p, seeds=0)
        self.assertEqual(fns["lib.fetch"]["boundary"], 8)
        self.assertEqual((p.dir / ".ras" / "clean.stdout").read_text(), plain.stdout)
        self.assertIn("404", plain.stdout)


@unittest.skipUnless(have("aiohttp"), "aiohttp not installed")
class AiohttpTest(Case):
    LIB = '''
        import os
        import aiohttp


        async def fetch(i):
            async with aiohttp.ClientSession() as s:
                async with s.post(os.environ["API"] + "/x", params={"i": i}, json={"n": i}, headers={"Idempotency-Key": "k%d" % i}) as r:
                    r.raise_for_status()
                    return await r.json()
        '''

    def test_loop(self):
        self.loop(self.project(self.LIB, "__import__('asyncio').run(lib.fetch(i))"), "lib.fetch", "aiohttp",
                  ASYNC_GOOD.format(imp="import aiohttp", body='''
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.8)) as s:
                async with s.post(os.environ["API"] + "/x", params={"i": i}, json={"n": i}, headers={"Idempotency-Key": "k%d" % i}) as r:
                    r.raise_for_status()
                    return await r.json()'''.strip()), self.LIB)

    def test_responses_behave_like_aiohttp(self):
        p = self.project('''
            import os
            import aiohttp


            async def fetch(i):
                out = []
                async with aiohttp.ClientSession(os.environ["API"], raise_for_status=False) as s:
                    r = await s.get("/a", params={"q": "x y"})
                    out += [r.status, r.ok, r.content_type, r.method, r.url.path, await r.text()]
                    r.release()
                    async with s.get("/b") as r:
                        out.append([c.decode() async for c in r.content.iter_chunked(7)])
                        out.append(isinstance(r, aiohttp.ClientResponse))
                    try:
                        await s.get("/missing", raise_for_status=True)
                    except aiohttp.ClientResponseError as e:
                        out += [e.status, e.request_info.url.path]
                return out
            ''', "__import__('asyncio').run(lib.fetch(i))")
        plain = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        _, fns = self.scan(p, seeds=0)
        self.assertEqual(fns["lib.fetch"]["boundary"], 12)
        self.assertEqual((p.dir / ".ras" / "clean.stdout").read_text(), plain.stdout)

    def test_concurrent_tasks_are_recorded_apart(self):
        p = self.project('''
            import asyncio
            import os
            import aiohttp


            async def fetch(i, s):
                async with s.get(os.environ["API"] + "/%d" % i) as r:
                    return (await r.json())["path"]


            async def go(i):
                async with aiohttp.ClientSession() as s:
                    return await asyncio.gather(*[one(i, j) for j in range(3)])


            async def one(i, j):
                async with aiohttp.ClientSession() as s:
                    return await fetch(i * 10 + j, s)
            ''', "__import__('asyncio').run(lib.go(i))")
        _, fns = self.scan(p, seeds=1)
        self.assertEqual(fns["lib.one"]["calls"], 12)  # resumed awaits aren't calls
        self.assertFalse(fns["lib.fetch"]["hardenable"])  # a live session isn't data
        self.assertEqual(fns["lib.fetch"]["boundary"], 12)


class WaitForTest(unittest.TestCase):
    """A timeout around the call counts, on the virtual clock, without real waiting."""

    def test_wait_for_cuts_a_hang(self):
        import asyncio
        import time
        from ras_anything import faults
        from ras_anything.agents.python import boundary

        clock = faults.VirtualClock()
        h = boundary.FaultingHandler(faults.FirstAttempt("timeout"), clock)
        req = boundary.Req("httpx", "GET", "http://a/b")

        async def serve():
            return {"status": 200}, 0.0

        async def guarded():
            try:
                await asyncio.wait_for(h.aperform(req, serve), 1.5)
            except (TimeoutError, asyncio.TimeoutError):
                return "cut"

        real = time.perf_counter
        t0 = real()
        clock.install()
        try:
            self.assertEqual(asyncio.run(guarded()), "cut")
            self.assertAlmostEqual(clock.elapsed(), 1.5, delta=0.01)
            self.assertEqual(h.hangs, 0)
            h.reset()
            with self.assertRaises(faults.ChaosHang):
                asyncio.run(h.aperform(req, serve))
            self.assertEqual(h.hangs, 1)
        finally:
            clock.uninstall()
        self.assertLess(real() - t0, 2.0)


class SocketTest(Case):
    LIB = '''
        import http.client
        import os
        import socket
        import urllib.parse


        def fetch(i):
            u = urllib.parse.urlsplit(os.environ["API"])
            s = socket.create_connection((u.hostname, u.port))
            s.sendall(b"GET /%d HTTP/1.0\\r\\n\\r\\n" % i)
            out = b""
            while chunk := s.recv(4096):
                out += chunk
            s.close()
            return out.splitlines()[-1].decode()


        def fetch_http_client(i):
            u = urllib.parse.urlsplit(os.environ["API"])
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=2)
            c.request("GET", "/%d" % i)
            try:
                return c.getresponse().read().decode()
            finally:
                c.close()
        '''

    def test_connection_faults_are_measured_but_not_replayable(self):
        p = self.project(self.LIB, "lib.fetch(i), lib.fetch_http_client(i)")
        plain = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True).stdout
        res, fns = self.scan(p, seeds=4)
        self.assertEqual((p.dir / ".ras" / "clean.stdout").read_text(), plain)
        for name in ("lib.fetch", "lib.fetch_http_client"):
            f = fns[name]
            self.assertEqual(f["boundary_kinds"], {"socket": 4})
            self.assertFalse(f["hardenable"])
            self.assertIn("raw socket", f["verdict"])
        self.assertIn("no_timeout", fns["lib.fetch"]["signals"])
        self.assertTrue(any(r["outcome"] in ("crash", "hang") for r in res["chaos"]["runs"]))
        self.assertTrue(all(r["uncaught"] is None or r["uncaught"]["type"] in ("ConnectionRefusedError", "TimeoutError", "ChaosHang")
                            for r in res["chaos"]["runs"]), res["chaos"]["runs"])

    def test_urlopen_is_not_counted_twice(self):
        p = self.project('''
            import os
            import urllib.request


            def fetch(i):
                with urllib.request.urlopen(os.environ["API"] + "/%d" % i, timeout=2) as r:
                    return r.read().decode()
            ''')
        _, fns = self.scan(p, seeds=0)
        self.assertEqual(fns["lib.fetch"]["boundary_kinds"], {"http": 4})


CLIENT = '''
    import json
    import logging
    import os
    import threading
    import urllib.request


    class Client:
        """An API client."""

        TIMEOUT = 2
        prefix = "/c"

        def __init__(self, name):
            self.name = name
            self.calls = 0
            self.__token = "t-" + name
            self._lock = threading.Lock()
            self.log = logging.getLogger("client")

        def get(self, i, *, suffix=""):
            with self._lock:
                self.calls += 1
            url = os.environ["API"] + self.prefix + "/%s/%d%s?t=%s" % (self.name, i, suffix, self.__token)
            with urllib.request.urlopen(url) as r:
                return json.loads(r.read())

        @classmethod
        def cget(cls, i):
            with urllib.request.urlopen(os.environ["API"] + cls.prefix + "/%d" % i) as r:
                return json.loads(r.read())

        @staticmethod
        def sget(i):
            with urllib.request.urlopen(os.environ["API"] + "/s/%d" % i) as r:
                return json.loads(r.read())

        async def aget(self, i):
            with urllib.request.urlopen(os.environ["API"] + "/a/%d" % i) as r:
                return json.loads(r.read())

        @property
        def status(self):
            with urllib.request.urlopen(os.environ["API"] + "/status") as r:
                return r.read().decode()


    class Sub(Client):
        prefix = "/sub"

        def get(self, i, *, suffix=""):
            with urllib.request.urlopen(os.environ["API"] + "/x") as r:
                r.read()
            return super().get(i, suffix=suffix)
    '''

METHOD_GOOD = '''
    import json
    import os
    import urllib.request

    from ras_anything.runtime import is_transient, log_event, retry

    RAS_FALLBACK = lambda *a, **k: {{"unavailable": True}}


    def {name}({args}):
        {first}
        def once():
            with urllib.request.urlopen({url}, timeout=0.8) as r:
                return json.loads(r.read())
        try:
            return retry(once, attempts=5, base=0.05, cap=0.4, budget_s=4.0, retry_on=lambda e: is_transient(e) or isinstance(e, ValueError))
        except Exception as e:
            log_event("unavailable", error=repr(e))
            return RAS_FALLBACK()
    '''

GET = dict(name="get", args='self, i, *, suffix=""', first="with self._lock:\n            self.calls += 1",
           url='os.environ["API"] + self.prefix + "/%s/%d%s?t=%s" % (self.name, i, suffix, self._Client__token)')


class MethodTest(Case):
    def test_method(self):
        p = self.project(CLIENT, "lib.Client('n').get(i, suffix='!')")
        fid = "lib.Client.get"
        with quiet():
            scan([sys.executable, "app.py"], seeds=1, quiet=True)
            harden(fid, quiet=True)
        stub = (p.dir / ".ras/hardens" / fid / "hardened.py").read_text()
        self.assertIn("def get(self, i, *, suffix=\"\"):", stub)
        self.assertIn("self._Client__token", stub)
        self.loop(p, fid, "http", METHOD_GOOD.format(**GET), stub)
        call = json.loads((p.dir / ".ras/hardens" / fid / "calls.json").read_text())["calls"][0]
        self.assertEqual(call["args"][0]["$object"][:2], ["lib", "Client"])
        self.assertEqual(call["kwargs"], {"suffix": "!"})

    def test_classmethod_gets_the_class_it_was_called_on(self):
        p = self.project(CLIENT, "(lib.Sub.cget(i), lib.Client.cget(i))[i % 2]['path']")
        with quiet():
            res = scan([sys.executable, "app.py"], seeds=1, quiet=True)
        fid = "lib.Client.cget"
        self.assertTrue({f["id"]: f for f in res["functions"]}[fid]["hardenable"])
        with quiet():
            harden(fid, quiet=True)
        v = self.verify(p, fid, METHOD_GOOD.format(name="cget", args="cls, i", first="", url='os.environ["API"] + cls.prefix + "/%d" % i'))
        self.assertTrue(v["passed"], v.get("fatal") or v["gates"])
        clean = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True).stdout
        with quiet():
            enable(fid, quiet=True)
        self.assertIn("Client.cget = classmethod(_ras_swap(", (p.dir / "lib.py").read_text())
        self.assertEqual(subprocess.run([sys.executable, "app.py"], capture_output=True, text=True).stdout, clean)
        off = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True, env={**os.environ, "RAS_ANYTHING_DISABLE": "1"})
        self.assertEqual(off.stdout, clean)
        with quiet():
            b = bench(seeds=2, rate=0.2, quiet=True)
            disable(fid, quiet=True)
        self.assertEqual(b["functions"]["hardened"][fid]["calls"], 16)

    def test_staticmethod(self):
        self.loop(self.project(CLIENT, "lib.Client.sget(i)"), "lib.Client.sget", "http",
                  METHOD_GOOD.format(name="sget", args="i", first="", url='os.environ["API"] + "/s/%d" % i'),
                  '''
                  import json, os, urllib.request
                  def sget(i):
                      with urllib.request.urlopen(os.environ["API"] + "/s/%d" % i) as r:
                          return json.loads(r.read())
                  ''')

    def test_async_method(self):
        p = self.project(CLIENT, "__import__('asyncio').run(lib.Client('n').aget(i))")
        with quiet():
            scan([sys.executable, "app.py"], seeds=1, quiet=True)
            harden("lib.Client.aget", quiet=True)
        good = METHOD_GOOD.format(name="aget", args="self, i", first="", url='os.environ["API"] + "/a/%d" % i').replace("def aget", "async def aget")
        v = self.verify(p, "lib.Client.aget", good)
        self.assertTrue(v["passed"], v.get("fatal") or v["gates"])

    def test_what_cannot_move_out_of_its_class_says_why(self):
        p = self.project(CLIENT, "(lib.Sub('n').get(i), lib.Client('n').status)[1]")
        _, fns = self.scan(p, seeds=1)
        self.assertIn("super()", fns["lib.Sub.get"]["verdict"])
        self.assertIn("@property", fns["lib.Client.status"]["verdict"])

    def test_method_of_a_class_in_the_entry_script(self):
        p = self.project("", "Api().send(Order(i))")
        app = (p.dir / "app.py").read_text().replace("import lib", textwrap.dedent("""
            import dataclasses, json, urllib.request


            @dataclasses.dataclass
            class Order:
                id: int


            class Api:
                def send(self, order):
                    with urllib.request.urlopen(os.environ["API"] + "/o%d" % order.id) as r:
                        return Order(len(json.loads(r.read())["path"]))
            """))
        (p.dir / "app.py").write_text(app.replace("print(", "print(type(Api().send(Order(i))) is Order, "))
        fid = "app.Api.send"
        with quiet():
            scan([sys.executable, "app.py"], seeds=1, quiet=True)
            harden(fid, quiet=True)
        v = self.verify(p, fid, '''
            import json, os, urllib.request
            from ras_anything.runtime import is_transient, retry
            from app import Order

            def send(self, order):
                def once():
                    with urllib.request.urlopen(os.environ["API"] + "/o%d" % order.id, timeout=0.8) as r:
                        return Order(len(json.loads(r.read())["path"]))
                return retry(once, attempts=5, base=0.05, cap=0.3, budget_s=4, retry_on=lambda e: is_transient(e) or isinstance(e, ValueError))
            ''')
        self.assertTrue(v["passed"], v.get("fatal") or v["gates"])
        with quiet():
            enable(fid, quiet=True)
        on = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True)
        self.assertEqual(on.stdout, "True Order(id=3)\n" * 4, on.stderr)

    def test_a_method_must_leave_self_as_the_original_does(self):
        p = self.project(CLIENT, "lib.Client('n').get(i)")
        with quiet():
            scan([sys.executable, "app.py"], seeds=1, quiet=True)
            harden("lib.Client.get", quiet=True)
        v = self.verify(p, "lib.Client.get", METHOD_GOOD.format(**{**GET, "first": "pass  # forgets self.calls += 1"}))
        self.assertFalse(v["gates"]["happy"])
        self.assertIn("different state", v["happy"]["first_failure"]["note"])

    def test_function_that_mutates_its_argument(self):
        p = self.project('''
            import os
            import urllib.request


            def fetch(order):
                with urllib.request.urlopen(os.environ["API"] + "/x") as r:
                    order["n"] = len(r.read())
                return True
            ''', "lib.fetch({'i': i})")
        with quiet():
            scan([sys.executable, "app.py"], seeds=1, quiet=True)
            harden("lib.fetch", quiet=True)
        v = self.verify(p, "lib.fetch", '''
            import os, urllib.request
            from ras_anything.runtime import retry

            def fetch(order):
                def once():
                    with urllib.request.urlopen(os.environ["API"] + "/x", timeout=1) as r:
                        return r.read()
                retry(once, attempts=5, budget_s=4)
                return True
            ''')
        self.assertFalse(v["gates"]["happy"])


if __name__ == "__main__":
    unittest.main()
