"""report: RAS-REPORT.md + ras-card.svg from measured results only."""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import faults, ui
from .paths import UserError, find_project, load_json


def _x(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _p(x, d=0) -> str:
    return "—" if x is None else ("%." + str(d) + "f%%") % (100 * x)


def collect(project: Path) -> dict:
    bench = load_json(project / ".ras" / "bench.json")
    scan = load_json(project / ".ras" / "scan.json")
    hardens = []
    base = project / ".ras" / "hardens"
    for d in sorted(base.iterdir()) if base.is_dir() else []:
        v, m = load_json(d / "verify.json"), load_json(d / "meta.json")
        if v and m and not v.get("fatal"):
            hardens.append({"id": d.name, "verify": v, "meta": m})
    if bench is None and not hardens:
        raise UserError("nothing measured yet", "report only shows numbers from verify and bench", "verify")
    return {"bench": bench, "scan": scan, "hardens": hardens}


def card_svg(title: str, data: dict) -> str:
    b = data["bench"]
    W, H = 800, 334
    sans = "system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
    mono = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
    if b:
        o, h = b["summary"]["original"], b["summary"]["hardened"]
        before, after = o["availability"], h["availability"]
        metric = "app availability under injected dependency faults"
        rows = [("identical output", _p(o.get("identical_rate")), _p(h.get("identical_rate"))),
                ("crashes", _p(o["crash_rate"]), _p(h["crash_rate"])),
                ("hangs", _p(o["hang_rate"]), _p(h["hang_rate"])),
                ("p99 run time", ui.secs(o["p99_wall"]), ui.secs(h["p99_wall"])),
                ("duplicate side effects", str(o["dups"]), str(h["dups"]))]
        n = "%d runs per variant, seeds 1–%d, interleaved" % (b["seeds"], b["seeds"])
        rate = "p=%g per attempt" % b["rate"]
    else:
        v = data["hardens"][0]["verify"]
        o, h = v["original"], v["hardened"]
        before, after = o["availability"], h["availability"]
        metric = "function availability under the verify fault matrix"
        rows = [("hangs", str(o["hang"]), str(h["hang"])), ("p99 latency", ui.secs(o["p99"]), ui.secs(h["p99"])),
                ("duplicate side effects", str(o["dups"]), str(h["dups"]))]
        n = "%d trials per variant" % h["trials"]
        rate = "fault matrix"
    eq = sum(x["verify"]["happy"]["equal"] for x in data["hardens"])
    tot = sum(x["verify"]["happy"]["total"] for x in data["hardens"])
    rows.append(("recorded calls, same result", "", "%d/%d" % (eq, tot) if tot else "—"))
    host = (b or {}).get("host") or {}
    caption1 = "Faults: timeout/hang, refused, HTTP 503, 429 + Retry-After, slow, truncated body, outage · " + rate
    caption2 = "%s · %s %s, Python %s · %s · measured by ras-anything" % (
        n, host.get("system", ""), host.get("machine", ""), host.get("python", ""), ((b or {}).get("created") or "")[:10])
    bx, bw = 152, 300

    def bar(y, label, frac, color, value_color):
        w = max(1.5, bw * frac)
        return (f'<text x="32" y="{y + 13}" font-family="{sans}" font-size="13" fill="#9da7b3">{label}</text>'
                f'<rect x="{bx}" y="{y}" width="{bw}" height="18" rx="3" fill="#1f2630"/>'
                f'<rect x="{bx}" y="{y}" width="{w:.1f}" height="18" rx="3" fill="{color}"/>'
                f'<text x="{bx + bw + 10}" y="{y + 14}" font-family="{mono}" font-size="14" font-weight="600" fill="{value_color}">{_p(frac, 1)}</text>')

    metrics = []
    y = 104
    for label, a, c in rows:
        metrics.append(f'<text x="540" y="{y}" font-family="{sans}" font-size="12" fill="#9da7b3">{_x(label)}</text>')
        if a:
            metrics.append(f'<text x="768" y="{y}" font-family="{mono}" font-size="13" text-anchor="end" fill="#e6edf3">'
                           f'<tspan fill="#9da7b3">{_x(a)}</tspan> → <tspan font-weight="700">{_x(c)}</tspan></text>')
        else:
            metrics.append(f'<text x="768" y="{y}" font-family="{mono}" font-size="12" text-anchor="end" fill="#e6edf3">{_x(c)}</text>')
        y += 30
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{_x(title)}: {_x(metric)} {_p(before, 1)} to {_p(after, 1)}">
  <title>{_x(title)}: {_x(metric)} {_p(before, 1)} → {_p(after, 1)}</title>
  <rect width="{W}" height="{H}" rx="14" fill="#0d1117"/>
  <rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="13.5" fill="none" stroke="#30363d"/>
  <text x="32" y="38" font-family="{mono}" font-size="13" fill="#56d364">RAS-anything</text>
  <text x="140" y="38" font-family="{sans}" font-size="13" fill="#9da7b3">{_x(title)}</text>
  <text x="32" y="92" font-family="{sans}" font-size="46" font-weight="800" fill="#9da7b3">{_p(before, 0)}<tspan fill="#6e7681" font-weight="400"> → </tspan><tspan fill="#56d364">{_p(after, 0)}</tspan></text>
  <text x="32" y="118" font-family="{sans}" font-size="14" fill="#c9d1d9">{_x(metric)}</text>
  {bar(150, "original", before, "#6e7681", "#c9d1d9")}
  {bar(182, "hardened", after, "#2ea043", "#56d364")}
  <line x1="{bx}" y1="144" x2="{bx}" y2="206" stroke="#484f58"/>
  <text x="{bx}" y="222" font-family="{mono}" font-size="10" fill="#6e7681" text-anchor="middle">0%</text>
  <text x="{bx + bw}" y="222" font-family="{mono}" font-size="10" fill="#6e7681" text-anchor="middle">100%</text>
  <line x1="516" y1="80" x2="516" y2="246" stroke="#21262d"/>
  {"".join(metrics)}
  <line x1="32" y1="274" x2="768" y2="274" stroke="#21262d"/>
  <text x="32" y="294" font-family="{sans}" font-size="11" fill="#8b949e">{_x(caption1)}</text>
  <text x="32" y="312" font-family="{sans}" font-size="11" fill="#8b949e">{_x(caption2)}</text>
</svg>
'''


def markdown(title: str, data: dict) -> str:
    b, hs = data["bench"], data["hardens"]
    out = []
    if b:
        o, h = b["summary"]["original"], b["summary"]["hardened"]
        out.append("# %s: app availability under faults %s → %s\n" % (title, _p(o["availability"], 1), _p(h["availability"], 1)))
        out.append("`%s`, %d whole-app runs per variant (seeds 1–%d, original and hardened interleaved, same fault "
                   "seed per pair), faults injected live at the dependency boundary with p=%g per attempt.\n"
                   % (b["display_cmd"], b["seeds"], b["seeds"], b["rate"]))
        out.append("![ras-anything card](ras-card.svg)\n")
        out.append("| whole app | original | hardened |\n|---|---:|---:|")
        out.append("| availability (exit 0, output correct or declared-degraded) | %s | **%s** |" % (
            _p(o["availability"], 1), _p(h["availability"], 1)))
        out.append("| byte-identical output (strict) | %s | %s |" % (_p(o.get("identical_rate"), 1), _p(h.get("identical_rate"), 1)))
        for label, k in (("crashes", "crash_rate"), ("hangs", "hang_rate"), ("wrong output", "wrong_rate")):
            out.append("| %s | %s | %s |" % (label, _p(o[k], 1), _p(h[k], 1)))
        out.append("| duplicate non-idempotent side effects | %d | %d |" % (o["dups"], h["dups"]))
        out.append("| p50 / p99 run time | %s / %s | %s / %s |" % (ui.secs(o["p50_wall"]), ui.secs(o["p99_wall"]),
                                                                ui.secs(h["p50_wall"]), ui.secs(h["p99_wall"])))
        out.append("| faults injected (total) | %d | %d |" % (o["faults"], h["faults"]))
        out.append("")
        out.append("A run counts as *degraded* when its output differs only where a hardened function returned its "
                   "declared `RAS_FALLBACK` in that run (and symmetrically for the original if its output matches a declared-degraded output)%s.\n"
                   % ((" or on lines containing %s" % ", ".join("`%s`" % m for m in b["degraded_markers"])) if b["degraded_markers"] else ""))
        for v in ("original", "hardened"):
            pl = b["summary"][v].get("failures_by_place") or {}
            if pl:
                out.append("- %s crashes/hangs: %s" % (v, ", ".join("%s ×%d" % kv for kv in sorted(pl.items(), key=lambda kv: -kv[1]))))
        out.append("")
        if not b["clean_outputs_equal"]:
            out.append("> ⚠ With no faults the hardened app printed different output than the original.\n")
    else:
        out.append("# %s\n" % title)
    if hs:
        out.append("## Verified functions\n")
        out.append("Each hardened function was replayed on its recorded real calls (dependency answers from the "
                   "recording, no network, virtual clock), then under the fault matrix below, next to the original.\n")
        out.append("| function | happy path | availability under faults | wrong | hangs | p99 (budget) | dup side effects | verdict |")
        out.append("|---|---|---|---|---|---|---|---|")
        for x in hs:
            v = x["verify"]
            o, h = v["original"], v["hardened"]
            out.append("| `%s` | %d/%d identical | %s → **%s** | %d → %d | %d → %d | %s → %s (%s) | %d → %d | %s |" % (
                x["id"], v["happy"]["equal"], v["happy"]["total"], _p(o["availability"], 1), _p(h["availability"], 1),
                o["wrong"], h["wrong"], o["hang"], h["hang"], ui.secs(o["p99"]), ui.secs(h["p99"]),
                ui.secs(v["budget_s"]), o["dups"], h["dups"], "✓ pass" if v["passed"] else "✗ fail"))
        out.append("")
    out.append("## Fault model\n")
    out.append("| fault | effect at the boundary |\n|---|---|")
    for k in faults.MATRIX + ("mixed",):
        out.append("| `%s` | %s |" % (k, faults.DESCRIBE[k]))
    out.append("")
    if b:
        out.append("Whole-app bench: each dependency attempt draws its fault from a hash of (seed, request, attempt "
                   "number), so original and hardened see the same fault on the same attempt. Outages hit %g%% of "
                   "requests. Injected waits are scaled ×%g in real time and a call with no timeout is declared hung "
                   "after %gs; the real local upstream answers everything else.\n"
                   % (100 * b["outage_rate"], b["time_scale"], b["hang_cap_s"]))
        host = b.get("host") or {}
        out.append("Host: %s %s %s, %s CPUs, Python %s · %s\n" % (host.get("system"), host.get("release"), host.get("machine"),
                                                                 host.get("cpus"), host.get("python"), b["created"]))
    out.append("Undo: `ras-anything disable <id>` removes the swap byte for byte; `RAS_ANYTHING_DISABLE=1` runs the originals.\n")
    out.append("<sub>Made with ras-anything · design inspired by [fast-anything](https://github.com/EL4CTEO/fast-anything) (MIT)</sub>\n")
    return "\n".join(out)


def report(title: str | None = None, quiet: bool = False) -> dict:
    project = find_project()
    data = collect(project)
    title = title or project.name
    md, svg = project / "RAS-REPORT.md", project / "ras-card.svg"
    md.write_text(markdown(title, data))
    svg.write_text(card_svg(title, data))
    if not quiet:
        ui.headline("report", title)
        b = data["bench"]
        if b:
            o, h = b["summary"]["original"], b["summary"]["hardened"]
            print("  app availability under faults %s %s %s" % (ui.pct(o["availability"]), ui.ARROW, ui.bold(ui.good(ui.pct(h["availability"])))))
        print("  %s %s" % (ui.good(ui.OK), os.path.relpath(md)))
        print("  %s %s" % (ui.good(ui.OK), os.path.relpath(svg)))
        ui.next_hint("status")
    return {"markdown": str(md), "svg": str(svg)}
