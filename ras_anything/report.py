"""report: RAS-REPORT.md + ras-card.svg + docs/RUNBOOK.md + .ras/slo.json from measured results only."""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import cassette, faults, slo, ui
from .paths import UserError, find_project, load_json


def _x(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _p(x, d=0) -> str:
    return "—" if x is None else ("%." + str(d) + "f%%") % (100 * x)


def _n(x) -> str:
    return "—" if x is None else "%.2f" % x


def _runtime(host: dict) -> str:
    return host.get("runtime") or "Python %s" % host.get("python", "")


def collect(project: Path, slo_spec: str | None = None, save_slo: bool = False) -> dict:
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
    slo_doc = slo.evaluate(project=project, bench=bench, hardens=hardens, targets=slo_spec, save=save_slo)
    return {"bench": bench, "scan": scan, "hardens": hardens, "slo": slo_doc}


def _agg_serviceability(data: dict) -> dict | None:
    b = data.get("bench") or {}
    hs = data.get("hardens") or []
    srv_list = [x["verify"].get("serviceability") for x in hs if (x["verify"].get("serviceability") or {}).get("hardened", {}).get("runs")]
    if srv_list:
        o_det = [s["original"]["detection_rate"] for s in srv_list if s.get("original", {}).get("detection_rate") is not None]
        h_det = [s["hardened"]["detection_rate"] for s in srv_list if s.get("hardened", {}).get("detection_rate") is not None]
        o_acc = [s["original"]["diagnosis_accuracy"] for s in srv_list if s.get("original", {}).get("diagnosis_accuracy") is not None]
        h_acc = [s["hardened"]["diagnosis_accuracy"] for s in srv_list if s.get("hardened", {}).get("diagnosis_accuracy") is not None]
        o_ln = [s["original"]["lines_to_diagnosis_p50"] for s in srv_list if s.get("original", {}).get("lines_to_diagnosis_p50") is not None]
        h_ln = [s["hardened"]["lines_to_diagnosis_p50"] for s in srv_list if s.get("hardened", {}).get("lines_to_diagnosis_p50") is not None]
        return {
            "source": "verify",
            "cases": sum(s["hardened"]["runs"] for s in srv_list),
            "orig_detection": sum(o_det) / len(o_det) if o_det else None,
            "hard_detection": sum(h_det) / len(h_det) if h_det else None,
            "orig_accuracy": sum(o_acc) / len(o_acc) if o_acc else None,
            "hard_accuracy": sum(h_acc) / len(h_acc) if h_acc else None,
            "orig_lines": sorted(o_ln)[len(o_ln) // 2] if o_ln else None,
            "hard_lines": sorted(h_ln)[len(h_ln) // 2] if h_ln else None,
            "bench": b.get("serviceability"),
        }
    if b.get("serviceability"):
        bs = b["serviceability"]
        return {
            "source": "bench",
            "cases": (bs.get("hardened") or {}).get("cases", 0),
            "orig_detection": (bs.get("original") or {}).get("detection_rate"),
            "hard_detection": (bs.get("hardened") or {}).get("detection_rate"),
            "orig_accuracy": None,
            "hard_accuracy": None,
            "orig_lines": (bs.get("original") or {}).get("lines_to_diagnosis_p50"),
            "hard_lines": (bs.get("hardened") or {}).get("lines_to_diagnosis_p50"),
            "bench": bs,
        }
    return None


def _agg_ax(data: dict) -> dict | None:
    hs = data.get("hardens") or []
    axs = [x["verify"].get("ax") for x in hs if x["verify"].get("ax")]
    b = data.get("bench") or {}
    leaks = list(b.get("leaks") or [])
    for a in axs:
        leaks.extend(a.get("leaks") or [])
    if not axs:
        return None
    cat = axs[-1].get("catalog")
    obs_list = [a["observed"] for a in axs if a.get("observed") and (a["observed"].get("original") or a["observed"].get("hardened"))]
    return {"catalog": cat, "observed": obs_list[0] if obs_list else None, "leaks": sorted(set(leaks))}


def card_svg(title: str, data: dict) -> str:
    b = data["bench"]
    W = 800
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
        if h.get("amplification") is not None:
            rows.append(("dependency attempts per call", _n(o.get("amplification")), _n(h["amplification"])))
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
    s = data.get("slo")
    if s and s.get("hardened"):
        t_avail = _p(s["targets"]["availability"], 1)
        rows.append(("SLO (%s avail)" % t_avail,
                     s["original"]["status"],
                     s["hardened"]["status"]))
    srv = _agg_serviceability(data)
    if srv and srv.get("hard_accuracy") is not None:
        rows.append(("log diagnosis accuracy", _p(srv.get("orig_accuracy")), _p(srv.get("hard_accuracy"))))

    host = (b or {}).get("host") or {}
    caption1 = "Faults: timeout/hang, refused, HTTP 503, 429 + Retry-After, slow, truncated body, outage · " + rate
    caption2 = "%s · %s %s, %s · %s · measured by ras-anything" % (
        n, host.get("system", ""), host.get("machine", ""), _runtime(host), ((b or {}).get("created") or "")[:10])
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
        y += 25
    div_y = max(274, y + 4)
    cap1_y = div_y + 20
    cap2_y = div_y + 38
    H = div_y + 60
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
  <line x1="516" y1="80" x2="516" y2="{div_y - 16}" stroke="#21262d"/>
  {"".join(metrics)}
  <line x1="32" y1="{div_y}" x2="768" y2="{div_y}" stroke="#21262d"/>
  <text x="32" y="{cap1_y}" font-family="{sans}" font-size="11" fill="#8b949e">{_x(caption1)}</text>
  <text x="32" y="{cap2_y}" font-family="{sans}" font-size="11" fill="#8b949e">{_x(caption2)}</text>
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
        if h.get("amplification") is not None:
            out.append("| retry amplification (dependency attempts per call of the enabled functions; no faults: %s) | %s | %s |" % (
                _n(h.get("clean_per_call")), _n(o["amplification"]), _n(h["amplification"])))
            out.append("| … for calls that met an outage (%d / %d calls) | %s | %s |" % (
                o["outage_calls"], h["outage_calls"], _n(o["outage_amplification"]), _n(h["outage_amplification"])))
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
        ox = [x for x in hs if x["verify"].get("outage")]
        if ox:
            t = ox[0]["verify"]["outage"]
            out.append("### Retry amplification and outages\n")
            out.append("Attempts per call over the fault matrix, and in a sustained outage: %d recorded calls in a row, %g s apart, with the "
                       "dependency down (after %d healthy ones, before %d with it back, %g s apart). Nothing is reset in between, so breakers, "
                       "retry budgets and caches work as they would. `answered` = ok + degraded; `back after` = virtual seconds from the "
                       "first call after the outage until a real answer.\n" % (
                           t["down"]["calls"], t["down"]["gap_s"], t["healthy"]["calls"], t["recovered"]["calls"], t["recovered"]["gap_s"]))
            out.append("| function | healthy | fault matrix | in the outage | answered in the outage | back after |")
            out.append("|---|---|---|---|---|---|")
            for x in ox:
                v = x["verify"]
                a, b2 = v["outage"]["original"], v["outage"]["hardened"]
                back = ["never" if s["recovered_after_s"] is None else "%g s" % round(s["recovered_after_s"], 1) for s in (a, b2)]
                out.append("| `%s` | %s → %s | %s → %s | %s → **%s** | %d → %d of %d | %s → %s |" % (
                    x["id"], _n(a["healthy_per_call"]), _n(b2["healthy_per_call"]), _n(v["original"]["amplification"]), _n(v["hardened"]["amplification"]),
                    _n(a["amplification"]), _n(b2["amplification"]), a["ok"] + a["degraded"], b2["ok"] + b2["degraded"], b2["calls"], *back))
            out.append("")

    # V-9 SLI / SLO & Error-Budget Burn
    s = data.get("slo")
    if s and (s.get("original") or s.get("hardened")):
        t = s["targets"]
        so, sh = s["original"], s["hardened"]
        out.append("## SLI / SLO & error-budget burn\n")
        out.append("Configured targets (`.ras/slo.json`, source: `%s`): availability ≥ **%s**, p99 ≤ **%s**, error rate ≤ **%s**.\n" % (
            s.get("source", "verify"), _p(t["availability"], 1), ui.secs(t["p99_s"]), _p(t["error_rate"], 1)))
        out.append("| SLI / SLO metric | original | hardened | target |\n|---|---:|---:|---:|")
        out.append("| availability | %s | %s | ≥ %s |" % (_p(so["availability"], 1), _p(sh["availability"], 1), _p(t["availability"], 1)))
        out.append("| p99 latency | %s | %s | ≤ %s |" % (ui.secs(so["p99_s"]), ui.secs(sh["p99_s"]), ui.secs(t["p99_s"])))
        out.append("| error rate | %s | %s | ≤ %s |" % (_p(so["error_rate"], 1), _p(sh["error_rate"], 1), _p(t["error_rate"], 1)))
        out.append("| error-budget burn | %.0f%% | %.0f%% | ≤ 100%% |" % (
            so["error_budget_burn_pct"], sh["error_budget_burn_pct"]))
        out.append("| SLO verdict | %s | **%s** | met |\n" % (
            "✓ met" if so["status"] == "met" else "✗ missed",
            "✓ met" if sh["status"] == "met" else "✗ missed"))

    # V-8 Serviceability
    srv = _agg_serviceability(data)
    if srv:
        out.append("## Serviceability (OTel JSON logs & diagnostics)\n")
        out.append("Measured by running the log-only diagnoser over captured faulted-run `stderr` (OTel JSON log records with W3C `traceparent`). "
                   "See [docs/RUNBOOK.md](docs/RUNBOOK.md) for log queries, health snapshot, incident replay (`ras-anything replay`), and rollback.\n")
        out.append("| serviceability metric | original | hardened |\n|---|---:|---:|")
        out.append("| fault detection rate (from logs) | %s | %s |" % (_p(srv.get("orig_detection"), 1), _p(srv.get("hard_detection"), 1)))
        out.append("| root-cause diagnosis accuracy (dep + fault) | %s | %s |" % (_p(srv.get("orig_accuracy"), 1), _p(srv.get("hard_accuracy"), 1)))
        out.append("| median lines to diagnosis | %s | %s |" % (
            srv["orig_lines"] if srv.get("orig_lines") is not None else "unavailable",
            srv["hard_lines"] if srv.get("hard_lines") is not None else "unavailable"))
        out.append("| time to restore (bad-enable rollback fixture) | unavailable | unavailable |\n")

    # V-18 Agent Experience (AX)
    ax_data = _agg_ax(data)
    if ax_data:
        out.append("## Agent experience (RFC 9457 structured errors)\n")
        out.append("Measured with the deterministic policy agent against plain exception text vs RFC 9457 `RasError.problem` objects, plus PII/secret leak scanning.\n")
        cat = ax_data.get("catalog")
        if cat:
            co, ch = cat["original"], cat["hardened"]
            out.append("| AX benchmark (10-cause catalog) | plain error | RFC 9457 `Problem` |\n|---|---:|---:|")
            out.append("| next-action accuracy | %s | **%s** |" % (_p(co["accuracy"], 1), _p(ch["accuracy"], 1)))
            out.append("| mean tokens to decide | %s | %s |" % (_n(co["mean_tokens"]), _n(ch["mean_tokens"])))
            out.append("| mean decision steps | %s | %s |" % (_n(co["mean_steps"]), _n(ch["mean_steps"])))
            out.append("| PII / secret leaks scanned | — | **%d** |\n" % len(ax_data.get("leaks") or []))

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
        out.append("Host: %s %s %s, %s CPUs, %s · %s\n" % (host.get("system"), host.get("release"), host.get("machine"),
                                                                host.get("cpus"), _runtime(host), b["created"]))
    out.append("Undo: `ras-anything disable <id>` removes the swap byte for byte; `RAS_ANYTHING_DISABLE=1` runs the originals.\n")
    out.append("<sub>Made with ras-anything · design inspired by [fast-anything](https://github.com/EL4CTEO/fast-anything) (MIT)</sub>\n")
    return "\n".join(out)


def _fn_runbook(x: dict, project: Path | None = None) -> str:
    fid = x["id"]
    m = x["meta"]
    v = x["verify"]
    deps = v.get("dependencies") or m.get("dependencies") or []
    if not deps and project is not None:
        cp = project / ".ras" / "hardens" / fid / "calls.json"
        if cp.is_file():
            deps = cassette.dependencies(cassette.load(cp), project)
    lines = [
        "# Runbook: `%s`" % fid,
        "",
        "- **Language:** %s" % m.get("lang", "python"),
        "- **Module / file:** `%s`" % m.get("file", "?"),
        "- **Latency budget:** `%ss`" % m.get("budget_s", "?"),
        "- **Verify verdict:** %s (`%s → %s` availability under faults)" % (
            "PASS" if v.get("passed") else "FAIL",
            _p(v["original"]["availability"], 1), _p(v["hardened"]["availability"], 1)),
        "",
        "## Dependencies",
        "",
    ]
    if deps:
        lines.append("| name | kind | method | route |")
        lines.append("|---|---|---|---|")
        for d in deps:
            lines.append("| `%s` | `%s` | `%s` | `%s` |" % (
                d.get("name", "?"), d.get("kind", "?"), d.get("method", "—"), d.get("route", "—")))
    else:
        lines.append("_No external dependencies recorded._")
    lines += [
        "",
        "## Faults handled & patterns",
        "",
        "Hardened function verified against: %s." % ", ".join("`%s`" % s for s in (v.get("chaos") or {}).get("scenarios", [])),
        "",
        "## Log query (OTel JSON on `stderr`)",
        "",
        "```bash",
        "# Filter structured JSON records for this function or failing dependency",
        "jq 'select(.attributes[\"dependency.name\"] != null or .severity_text == \"ERROR\")' app.stderr.log",
        "# Correlate across hops using W3C traceparent / trace_id",
        "jq 'select(.trace_id == \"<32-hex-trace-id>\")' app.stderr.log",
        "```",
        "",
        "## Health, breaker state & incident replay",
        "",
        "- Live health & SLO check: `ras-anything status` (reads `.ras/health.json` or `$RAS_ANYTHING_HEALTH_FILE`, `.ras/slo.json`, and `.ras/incidents/`).",
        "- Breaker open means upstream failures crossed the threshold; calls fail fast (`circuit_open` / `degraded`) until cooldown expires and a half-open probe succeeds.",
        "- Flight-recorder incidents: `ras-anything replay --list` then `ras-anything replay <incident-id>` for deterministic virtual-clock reproduction and RCA.",
        "",
        "## Instant rollback",
        "",
        "```bash",
        "ras-anything disable %s   # byte-for-byte source restore" % fid,
        "# OR without touching files:",
        "RAS_ANYTHING_DISABLE=1 <your-app-command>",
        "```",
        "",
        "## Evidence links",
        "",
        "- Verification results: `.ras/hardens/%s/verify.json`" % fid,
        "- Report: `RAS-REPORT.md`",
        "- SLO status: `.ras/slo.json`",
        "",
    ]
    return "\n".join(lines)


def write_runbooks(project: Path, title: str, data: dict) -> Path:
    hs = data.get("hardens") or []
    for x in hs:
        rb = project / ".ras" / "hardens" / x["id"] / "RUNBOOK.md"
        rb.parent.mkdir(parents=True, exist_ok=True)
        rb.write_text(_fn_runbook(x, project), encoding="utf-8")

    lines = [
        "# %s — Operations Runbook" % title,
        "",
        "Auto-generated by `ras-anything report` from measured verify/bench/SLO artifacts.",
        "",
        "## Quick triage",
        "",
        "1. Run `ras-anything status` to inspect enabled swaps, `.ras/health.json`, `.ras/slo.json`, and recorded incidents.",
        "2. Inspect structured OTel JSON logs on `stderr` by `trace_id`, `attributes[\"dependency.name\"]`, and `attributes[\"fault.class\"]`.",
        "3. Reproduce any captured incident deterministically: `ras-anything replay --list` then `ras-anything replay <incident-id>`.",
        "4. Roll back immediately if needed: `ras-anything disable <id>` (byte-for-byte restore) or set `RAS_ANYTHING_DISABLE=1`.",
        "",
        "## Hardened functions",
        "",
    ]
    for x in hs:
        lines.append("- [`%s`](../.ras/hardens/%s/RUNBOOK.md) — verify %s (`%s → %s`)" % (
            x["id"], x["id"], "PASS" if x["verify"].get("passed") else "FAIL",
            _p(x["verify"]["original"]["availability"], 1), _p(x["verify"]["hardened"]["availability"], 1)))
    lines += [
        "",
        "## Evidence",
        "",
        "- [RAS-REPORT.md](../RAS-REPORT.md)",
        "- [.ras/slo.json](../.ras/slo.json)",
        "",
    ]
    docs_dir = project / "docs"
    docs_dir.mkdir(exist_ok=True)
    out_rb = docs_dir / "RUNBOOK.md"
    out_rb.write_text("\n".join(lines), encoding="utf-8")
    return out_rb


def report(title: str | None = None, slo_spec: str | None = None, quiet: bool = False) -> dict:
    project = find_project()
    data = collect(project, slo_spec=slo_spec, save_slo=True)
    title = title or project.name
    md, svg = project / "RAS-REPORT.md", project / "ras-card.svg"
    md.write_text(markdown(title, data), encoding="utf-8")
    svg.write_text(card_svg(title, data), encoding="utf-8")
    runbook = write_runbooks(project, title, data)
    if not quiet:
        ui.headline("report", title)
        b = data["bench"]
        if b:
            o, h = b["summary"]["original"], b["summary"]["hardened"]
            print("  app availability under faults %s %s %s" % (ui.pct(o["availability"]), ui.ARROW, ui.bold(ui.good(ui.pct(h["availability"])))))
        if data.get("slo"):
            slo.print_slo(data["slo"])
        print("  %s %s" % (ui.good(ui.OK), os.path.relpath(md)))
        print("  %s %s" % (ui.good(ui.OK), os.path.relpath(svg)))
        print("  %s %s" % (ui.good(ui.OK), os.path.relpath(runbook)))
        ui.next_hint("status")
    return {"markdown": str(md), "svg": str(svg), "runbook": str(runbook), "slo": str(project / ".ras" / "slo.json")}
