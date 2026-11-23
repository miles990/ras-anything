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
_SUPPORTED_PROTOCOL = ("2024-11-05", "2025-03-26")

_cwd = {"cwd": {"type": "string", "description": "project folder (where .ras/ lives or should be created)"}}
_id = {"id": {"type": "string", "description": "function id from the scan, e.g. weather.fetch_forecast"}}
_seeds = {"seeds": {"type": "integer", "description": "number of seeded chaos runs (default from the CLI)"}}
_cmd = {"cmd": {"type": "array", "items": {"type": "string"}, "description": "command that runs the app, e.g. [\"python\", \"app.py\"]"}}

TOOLS = {
    "ras_scan": ("Run the app clean and under seeded dependency chaos; rank functions by risk × exposure × hardenability.",
                 {**_cwd, **_cmd, **_seeds}, ["cmd"]),
    "ras_harden": ("Record real calls of one function with their dependency I/O and write TASK.md + a stub "
                   "(optionally run an agent). Then edit the hardened file following TASK.md.",
                   {**_cwd, **_id, "agent": {"type": "string", "description": "claude | codex | shell command (optional)"}}, []),
    "ras_verify": ("Replay recorded calls (happy path, then the fault matrix, virtual clock) for original vs hardened; "
                   "report gates.", {**_cwd, **_id}, []),
    "ras_enable": ("Swap the verified hardened function in (one marked block in its module).", {**_cwd, **_id}, []),
    "ras_disable": ("Remove the swap, restoring the module byte for byte.", {**_cwd, **_id}, []),
    "ras_bench": ("Whole-app runs under live seeded chaos, original vs hardened, interleaved.",
                  {**_cwd, **_cmd, **_seeds,
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
                   {**_cwd, "format": {"type": "string", "enum": list(EXPORT_FORMATS), "description": "output format"}}, []),
    "ras_events": ("Read .ras/out/events.ndjson incrementally using a sequence cursor.",
                   {**_cwd, "since": {"type": "integer", "description": "sequence cursor (default 0)"},
                    "limit": {"type": "integer", "description": "max events to return (default 200)"}}, []),
}


# MCP argument -> CLI flag, in CLI order: (argument, commands it applies to, flag or None for positional, how to render)
_FLAGS = (
    ("id", ("harden", "verify", "enable", "disable"), None, str),
    ("canary", ("enable",), "--canary", str),
    ("shadow", ("enable",), "--shadow", None),
    ("agent", ("harden",), "--agent", str),
    ("seeds", ("scan", "bench"), "--seeds", lambda v: str(int(v))),
    ("degraded_marker", ("bench",), "--degraded-marker", str),
)


def _special(cmd: str, a: dict) -> list[str] | None:
    if cmd == "flag":
        return list(a.get("argv") or ["list"])
    if cmd == "export":
        return ["--format", str(a.get("format") or "json")]
    if cmd == "replay":
        return ["--list"] if a.get("list") else [a["id"]] if a.get("id") else []
    return None


def _argv(name: str, a: dict) -> list[str]:
    """The CLI argv an MCP tool call stands for (the MCP server is a shell over the CLI)."""
    cmd = name.split("_", 1)[1]
    special = _special(cmd, a)
    if special is not None:
        return [cmd] + special
    argv = [cmd]
    for key, cmds, flag, render in _FLAGS:
        if not a.get(key) or cmd not in cmds:
            continue
        argv += ([flag] if flag else []) + ([render(a[key])] if render else [])
    if a.get("cmd"):
        argv += ["--"] + [str(x) for x in a["cmd"]]
    return argv


def _text_result(text: str, err: bool) -> dict:
    return {"content": [{"type": "text", "text": text or "(no output)"}], "isError": err}


def _events_tool(args: dict) -> dict:
    from pathlib import Path

    from .export import read_events, sync_out

    cwd = Path(args.get("cwd") or os.getcwd())
    if (cwd / ".ras").is_dir():
        sync_out(cwd)
    res = read_events(cwd, since=int(args.get("since") or 0), limit=int(args.get("limit") or 200))
    return _text_result(json.dumps(res, indent=2, sort_keys=True), False)


def _run_cli(name: str, args: dict) -> dict:
    argv = _argv(name, args)
    env = dict(os.environ, NO_COLOR="1")
    env.pop("FORCE_COLOR", None)
    cwd = args.get("cwd") or os.getcwd()
    try:
        p = subprocess.run([sys.executable, "-m", "ras_anything", *argv], cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=3600, stdin=subprocess.DEVNULL)
        text = (p.stdout + ("\n" + p.stderr if p.stderr.strip() else "")).strip()
        return _text_result(text, p.returncode != 0)
    except (OSError, subprocess.TimeoutExpired) as e:
        return _text_result("ras-anything %s failed: %s" % (" ".join(argv), e), True)


# In-process tools (the rest are a thin CLI subprocess).
_TOOL_IMPL = {
    "ras_events": _events_tool,
}


def call_tool(name: str, args: dict) -> dict:
    if name not in TOOLS:
        return _text_result("unknown tool %s" % name, True)
    special = _TOOL_IMPL.get(name)
    if special is not None:
        return special(args)
    return _run_cli(name, args)


def _initialize(params: dict) -> dict:
    req_ver = params.get("protocolVersion")
    ver = req_ver if req_ver in _SUPPORTED_PROTOCOL else PROTOCOL
    return {"protocolVersion": ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ras-anything", "version": __version__},
            "instructions": "ras-anything hardens the few functions that break under dependency faults. Loop: "
                            "ras_scan -> ras_harden -> edit the hardened file per TASK.md -> ras_verify (until PASS) -> "
                            "ras_enable -> ras_bench -> ras_report."}


def _tools_list(_params: dict) -> dict:
    return {"tools": [{"name": n, "description": d,
                       "inputSchema": {"type": "object", "properties": props, "required": req}}
                      for n, (d, props, req) in TOOLS.items()]}


def _tools_call(params: dict) -> dict:
    return call_tool(params.get("name", ""), params.get("arguments") or {})


def _ping(_params: dict) -> dict:
    return {}


METHODS = {
    "initialize": _initialize,
    "tools/list": _tools_list,
    "tools/call": _tools_call,
    "ping": _ping,
}


def handle(msg: dict):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    fn = METHODS.get(method)
    if fn is None:
        if mid is None:  # notification (e.g. notifications/initialized)
            return None
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}
    result = fn(params)
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid, "result": result}


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
