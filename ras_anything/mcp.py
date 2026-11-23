"""`ras-anything mcp`: a stdio MCP server (JSON-RPC 2.0, one message per line). Standard library only.

Each tool runs the matching CLI command in the given project folder and returns its plain-text output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from . import __version__
from .contracts import EXPORT_FORMATS

PROTOCOL = "2024-11-05"

_cwd = {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}}
_id = {"id": {"type": "string", "description": "function id from the scan, e.g. weather.fetch_forecast"}}
_cmd = {"cmd": {"type": "array", "items": {"type": "string"}, "description": "command that runs the app, e.g. [\"python\", \"app.py\"]"}}

TOOLS = {
    "ras_scan": ("Run the app clean and under seeded dependency chaos; rank functions by risk × exposure × hardenability.",
                 {**_cwd, **_cmd, "seeds": {"type": "integer"}}, ["cmd"]),
    "ras_harden": ("Record real calls of one function with their dependency I/O and write TASK.md + a stub "
                   "(optionally run an agent). Then edit the hardened file following TASK.md.",
                   {**_cwd, **_id, "agent": {"type": "string", "description": "claude | codex | shell command (optional)"}}, []),
    "ras_verify": ("Replay recorded calls (happy path, then the fault matrix, virtual clock) for original vs hardened; "
                   "report gates.", {**_cwd, **_id}, []),
    "ras_enable": ("Swap the verified hardened function in (one marked block in its module).", {**_cwd, **_id}, []),
    "ras_disable": ("Remove the swap, restoring the module byte for byte.", {**_cwd, **_id}, []),
    "ras_bench": ("Whole-app runs under live seeded chaos, original vs hardened, interleaved.",
                  {**_cwd, **_cmd, "seeds": {"type": "integer"},
                   "degraded_marker": {"type": "string", "description": "text marking a deliberately degraded output line"}}, []),
    "ras_report": ("Write RAS-REPORT.md and ras-card.svg from measured results.", {**_cwd}, []),
    "ras_status": ("Show scan, hardened functions, verify state, what is enabled, flags and the next step.", {**_cwd}, []),
    "ras_replay": ("Replay a recorded incident from .ras/incidents/ on the virtual clock and write RCA (or list incidents).",
                   {**_cwd, "id": {"type": "string", "description": "incident id (optional; omit for latest)"},
                    "list": {"type": "boolean", "description": "list recorded incidents instead of replaying"}}, []),
    "ras_flag": ("List, get, set or delete flags in .ras/flags.json (hot-reloaded in running processes).",
                 {**_cwd, "argv": {"type": "array", "items": {"type": "string"},
                                   "description": "flag args, e.g. set ras.pattern.retry off, or list"}}, []),
    "ras_export": ("Export .ras/out snapshots and telemetry as %s." % ", ".join(EXPORT_FORMATS),
                   {**_cwd, "format": {"type": "string", "enum": list(EXPORT_FORMATS)}}, []),
    "ras_events": ("Read .ras/out/events.ndjson incrementally using a sequence cursor.",
                   {**_cwd, "since": {"type": "integer", "description": "sequence cursor (default 0)"},
                    "limit": {"type": "integer", "description": "max events to return (default 200)"}}, []),
}


def _argv(name: str, a: dict) -> list[str]:
    cmd = name.split("_", 1)[1]
    argv = [cmd]
    if cmd == "flag":
        argv += list(a.get("argv") or ["list"])
        return argv
    if cmd == "export":
        argv += ["--format", str(a.get("format") or "json")]
        return argv
    if cmd == "replay":
        if a.get("list"):
            argv.append("--list")
        elif a.get("id"):
            argv.append(a["id"])
        return argv
    if a.get("id") and cmd in ("harden", "verify", "enable", "disable"):
        argv.append(a["id"])
    if a.get("canary") and cmd == "enable":
        argv += ["--canary", str(a["canary"])]
    if a.get("shadow") and cmd == "enable":
        argv.append("--shadow")
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
    if name == "ras_events":
        from pathlib import Path
        from .export import read_events, sync_out
        cwd = Path(args.get("cwd") or os.getcwd())
        if (cwd / ".ras").is_dir():
            sync_out(cwd)
        res = read_events(cwd, since=int(args.get("since") or 0), limit=int(args.get("limit") or 200))
        return {"content": [{"type": "text", "text": json.dumps(res, indent=2, sort_keys=True)}], "isError": False}
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
        req_ver = params.get("protocolVersion")
        ver = req_ver if req_ver in ("2024-11-05", "2025-03-26") else PROTOCOL
        result = {"protocolVersion": ver,
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "ras-anything", "version": __version__},
                  "instructions": "ras-anything hardens the few functions that break under dependency faults. Loop: "
                                  "ras_scan -> ras_harden -> edit the hardened file per TASK.md -> ras_verify (until PASS) -> "
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
