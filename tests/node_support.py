"""Shared by the Node tests: they are skipped where Node.js is missing or too old."""

import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from ras_anything.lang import node
from tests.test_pipeline import Project

NODE = shutil.which("node")
INFO = node.probe(NODE) if NODE else {}
HAVE = bool(NODE) and "error" not in INFO and not node.too_old(INFO)
needs_node = unittest.skipUnless(HAVE, "needs " + node.MIN_TEXT)
needs_ts = unittest.skipUnless(HAVE and INFO.get("ts"), "needs a Node.js that runs TypeScript")
AGENT = node.AGENT_DIR

UPSTREAM = """
import http from "node:http";

export const seen = [];

export function start() {
  return new Promise((ready) => {
    const server = http.createServer((req, res) => {
      const chunks = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => {
        seen.push({ method: req.method, path: req.url, key: req.headers["idempotency-key"] ?? null });
        const body = JSON.stringify({ path: req.url, method: req.method, sent: Buffer.concat(chunks).toString() });
        res.writeHead(req.url.includes("missing") ? 404 : 200, { "content-type": "application/json", "x-test": "yes" });
        res.end(body);
      });
    });
    server.listen(0, "127.0.0.1", () => {
      process.env.API = `http://127.0.0.1:${server.address().port}`;
      ready(() => { server.close(); server.closeAllConnections(); });
    });
  });
}
"""

APP = """
import { start } from "./upstream.mjs";
import * as ns from "./%s";
const lib = { ...ns.default, ...ns };
const stop = await start();
for (let i = 0; i < 3; i++) console.log(JSON.stringify(%s));
stop();
"""


def js(code: str) -> str:
    return textwrap.dedent(code).lstrip("\n")


def run_js(code: str, name: str = "probe.mjs", *flags: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "upstream.mjs").write_text(UPSTREAM)
        p = Path(d) / name
        p.write_text(js(code))
        return subprocess.run([NODE, *flags, str(p)], cwd=d, capture_output=True, text=True, timeout=60)


def run_json(code: str, *flags: str):
    r = run_js(code, "probe.mjs", *flags)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


class NodeCase(unittest.TestCase):
    def project(self, files: dict, lib: str = "lib.mjs", call: str = "await lib.get(i)") -> Project:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "app"
        src.mkdir()
        (src / "upstream.mjs").write_text(UPSTREAM)
        (src / "app.mjs").write_text(APP % (lib, call))
        for name, text in files.items():
            (src / name).parent.mkdir(parents=True, exist_ok=True)
            (src / name).write_bytes(js(text).encode())
        p = Project(src)
        self.addCleanup(p.close)
        return p
