"""JSON cassettes: values and exceptions round-trip, and loading one is data parsing only."""

import collections
import datetime
import decimal
import enum
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import uuid
from dataclasses import dataclass
from pathlib import Path

from ras_anything import cassette
from ras_anything.agents.python import values
from ras_anything.agents.python.values import Codec, RemoteError, Unsupported, build_exc, dump_exc
from ras_anything.cassette import BadCassette

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ras_anything"


class Color(enum.Enum):
    RED = 1


Point = collections.namedtuple("Point", "x y")


@dataclass
class Order:
    id: str
    lines: list


class Slotted:
    __slots__ = ("a",)

    def __init__(self, a):
        self.a = a


class Loud:
    def __init__(self):
        raise AssertionError("__init__ must not run when a cassette is loaded")


def here(module, qualname):
    if module != __name__:
        raise LookupError(module)
    return getattr(sys.modules[__name__], qualname)


CODEC = Codec("/srv/some/project", owns=lambda c: c.__module__ == __name__, find=here)


def trip(v, codec=CODEC):
    return codec.dec(json.loads(json.dumps(codec.enc(v), allow_nan=False)))


class ValuesTest(unittest.TestCase):
    def test_round_trip(self):
        for v in (None, True, 3, -2.5, 1.0, "héllo", [1, [2]], {"a": {"b": [1]}}, (1, (2, "x")), b"\x00\xff", bytearray(b"z"),
                  {1, 2}, frozenset({"a"}), {1: "a", (2, 3): "b"}, {"$bytes": "not a tag"}, {"$x": 1, "y": 2}, {}, (), complex(1, 2),
                  collections.OrderedDict([("b", 1), ("a", 2)]), datetime.datetime(2026, 1, 2, 3, 4, 5, 6),
                  datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc), datetime.date(2026, 1, 2),
                  datetime.timedelta(1, 2, 3), decimal.Decimal("1.10"), uuid.UUID(int=7), Path("a/b"),
                  Color.RED, Point(1, [2]), Order("o1", [Point(1, 2)]), float("inf"), "\ud800"):
            got = trip(v)
            self.assertEqual(got, v)
            self.assertIs(type(got), type(v))
        self.assertNotEqual(trip(float("nan")), trip(float("nan")))
        self.assertEqual(trip(Slotted(5)).a, 5)
        self.assertIs(trip(Order), Order)
        self.assertIsInstance(trip(threading.Lock()), type(threading.Lock()))

    def test_unsupported_values_are_refused_not_mangled(self):
        loop = []
        loop.append(loop)
        for v in (object(), lambda: 1, sys.stdout.buffer, loop, {"f": subprocess.Popen}):
            self.assertRaises(Unsupported, CODEC.enc, v)
        self.assertRaises(Unsupported, Codec().enc, Order("a", []))  # not the project's class

    def test_objects_are_rebuilt_without_running_their_code(self):
        obj = object.__new__(Loud)
        obj.n = 1
        self.assertEqual(trip(obj).n, 1)

    def test_loader_never_imports_or_looks_up_unknown_classes(self):
        for j in ({"$object": ["os", "system", []]}, {"$class": ["subprocess", "Popen"]}, {"$enum": ["signal", "Signals", "SIGINT"]},
                  {"$ext": ["os.system", {}]}, {"$reduce": ["os.system", ["true"]]}):
            self.assertRaises(BadCassette, CODEC.dec, j)
        self.assertRaises(BadCassette, Codec().dec, {"$object": [__name__, "Order", []]})

    def test_project_paths_move_with_the_project(self):
        j = CODEC.enc(["/srv/some/project/a.py", "/srv/some/project2", {"/srv/some/project/k": "/srv/some/project"}, "${PROJECT} /srv/some/project/x"])
        self.assertNotIn("/srv/some/project/", json.dumps(j))
        self.assertEqual(Codec("/new/home").dec(j), ["/new/home/a.py", "/srv/some/project2", {"/new/home/k": "/new/home"}, "${PROJECT} /new/home/x"])

    def test_file_header_is_checked(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "calls.json"
            cassette.save(p, [{"args": []}], "python")
            self.assertEqual(cassette.load(p), [{"args": []}])
            for text in ("\x80\x04", "[]", '{"format": "ras-anything-cassette", "version": 99, "calls": []}'):
                p.write_text(text)
                self.assertRaises(BadCassette, cassette.load, p)


class ExceptionsTest(unittest.TestCase):
    def test_boundary_exceptions_come_back_as_themselves(self):
        for e in (TimeoutError("t"), ConnectionRefusedError(111, "Connection refused"), BlockingIOError(11, "again"),
                  urllib.error.URLError(socket.timeout("timed out")), urllib.error.URLError(ConnectionRefusedError(111, "no")),
                  http.client.RemoteDisconnected("bye"), subprocess.TimeoutExpired(["a", "b"], 3), socket.gaierror(-2, "Name or service not known"),
                  json.JSONDecodeError("bad", "{", 1)):
            got = build_exc(json.loads(json.dumps(dump_exc(e))))
            self.assertIs(type(got), type(e))
            if isinstance(e, OSError):
                self.assertEqual(got.errno, e.errno)
        u = build_exc(dump_exc(urllib.error.URLError(ConnectionRefusedError(111, "no"))))
        self.assertIsInstance(u.reason, ConnectionRefusedError)

    def test_unknown_types_become_remote_error_with_the_name(self):
        for module, name in (("os", "system"), ("evil", "Boom"), ("builtins", "eval"), ("builtins", "SystemExit"),
                             ("builtins", "KeyboardInterrupt"), ("subprocess", "Popen"), ("builtins", "__import__")):
            e = build_exc({"type": name, "module": module, "msg": "x"})
            self.assertIsInstance(e, RemoteError)
            self.assertEqual(e.type_name, "%s.%s" % (module, name))
            self.assertTrue(values.matches(e, {"type": name, "module": module}))

    def test_optional_http_clients(self):
        for mod, name in (("requests.exceptions", "ReadTimeout"), ("httpx", "ConnectError")):
            try:
                __import__(mod)
            except ImportError:
                continue
            e = build_exc({"type": name, "module": mod, "msg": "m"})
            self.assertEqual(type(e).__name__, name)


class NoPickleTest(unittest.TestCase):
    def test_package_has_no_code_executing_deserializer(self):
        bad = re.compile(r"\b(c?[pP]ickle|dill|marshal|shelve)\b|(?<![.\w])(eval|exec)\s*\(")
        hits = []
        for p in sorted(PKG.rglob("*.py")):
            for n, ln in enumerate(p.read_text().splitlines(), 1):
                if bad.search(ln) and "codex exec" not in ln and '"exec"' not in ln:
                    hits.append("%s:%d %s" % (p.relative_to(REPO), n, ln.strip()))
        self.assertEqual(hits, [])

    def test_no_binary_cassettes_in_examples(self):
        self.assertEqual([str(p) for p in (REPO / "examples").rglob("*.pkl")], [])


if __name__ == "__main__":
    unittest.main()
