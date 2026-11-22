"""`ras-anything mcp`: a stdio MCP server (JSON-RPC 2.0, one message per line). Standard library only.

Each tool runs the matching CLI command in the given project folder and returns its plain-text output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from . import __version__

PROTOCOL = "2024-11-05"

_cwd = {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}}
_id = {"id": {"type": "string", "description": "function id from the scan, e.g. weather.fetch_forecast"}}
_cmd = {"cmd": {"type": "array", "items": {"type": "string"}, "description": "command that runs the app, e.g. [\"python\", \"app.py\"]"}}

TOOLS = {
    "ras_scan": ("Run the app clean and under seeded dependency chaos; rank functions by risk × exposure × hardenability.",
                 {**_cwd, **_cmd, "seeds": {"type": "integer"}}, ["cmd"]),
    "ras_harden": ("Record real calls of one function with their dependency I/O and write TASK.md + a hardened.py stub "
                   "(optionally run an agent). Then edit hardened.py following TASK.md.",
                   {**_cwd, **_id, "agent": {"type": "string", "description": "claude | codex | shell command (optional)"}}, []),
    "ras_verify": ("Replay recorded calls (happy path, then the fault matrix, virtual clock) for original vs hardened; "
                   "report gates.", {**_cwd, **_id}, []),
    "ras_enable": ("Swap the verified hardened function in (one marked block in its module).", {**_cwd, **_id}, []),
    "ras_disable": ("Remove the swap, restoring the module byte for byte.", {**_cwd, **_id}, []),
    "ras_bench": ("Whole-app runs under live seeded chaos, original vs hardened, interleaved.",
                  {**_cwd, **_cmd, "seeds": {"type": "integer"},
                   "degraded_marker": {"type": "string", "description": "text marking a deliberately degraded output line"}}, []),
    "ras_report": ("Write RAS-REPORT.md and ras-card.svg from measured results.", {**_cwd}, []),
    "ras_status": ("Show scan, hardened functions, verify state, what is enabled and the next step.", {**_cwd}, []),
}


def _argv(name: str, a: dict) -> list[str]:
    cmd = name.split("_", 1)[1]
    argv = [cmd]
    if a.get("id") and cmd in ("harden", "verify", "enable", "disable"):
        argv.append(a["id"])
    if a.get("agent") and cmd == "harden":
        argv += ["--agent", a["agent"]]
    if a.get("seeds") and cmd in ("scan", "bench"):
        argv += ["--seeds", str(int(a["seeds"]))]
    if a.get("degraded_marker") and cmd == "bench":
        argv += ["--degraded-marker", a["degraded_marker"]]
    if a.get("cmd"):
        argv += ["--"] + [str(x) for x in a["cmd"]]
    return argv


def call_tool(name: str, args: dict) -> dict:
    if name not in TOOLS:
        return {"content": [{"type": "text", "text": "unknown tool %s" % name}], "isError": True}
    argv = _argv(name, args)
    env = dict(os.environ, NO_COLOR="1")
    env.pop("FORCE_COLOR", None)
    cwd = args.get("cwd") or os.getcwd()
    try:
        p = subprocess.run([sys.executable, "-m", "ras_anything", *argv], cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=3600, stdin=subprocess.DEVNULL)
        text = (p.stdout + ("\n" + p.stderr if p.stderr.strip() else "")).strip()
        return {"content": [{"type": "text", "text": text or "(no output)"}], "isError": p.returncode != 0}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"content": [{"type": "text", "text": "ras-anything %s failed: %s" % (" ".join(argv), e)}], "isError": True}


def handle(msg: dict):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "ras-anything", "version": __version__},
                  "instructions": "ras-anything hardens the few functions that break under dependency faults. Loop: "
                                  "ras_scan -> ras_harden -> edit hardened.py per TASK.md -> ras_verify (until PASS) -> "
                                  "ras_enable -> ras_bench -> ras_report."}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d,
                             "inputSchema": {"type": "object", "properties": props, "required": req}}
                            for n, (d, props, req) in TOOLS.items()]}
    elif method == "tools/call":
        result = call_tool(params.get("name", ""), params.get("arguments") or {})
    elif method == "ping":
        result = {}
    elif mid is None:  # notification (e.g. notifications/initialized)
        return None
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}
    return None if mid is None else {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                resp = handle(msg)
            except Exception as e:  # report, keep serving
                resp = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": repr(e)}}
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
    return 0
