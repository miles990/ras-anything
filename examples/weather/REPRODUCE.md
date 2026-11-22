# Reproduce: weather dashboard

Everything runs offline: `app.py` starts its own forecast API (`upstream.py`, stdlib `http.server`)
in a thread on a free localhost port. Faults are injected at the app's side of the network boundary;
the upstream itself is always healthy.

Host for the committed numbers: Linux 6.12 x86_64, 8 CPUs, Python 3.13.5, 2026-11-23.

```sh
# from the repo root
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
cd examples/weather
python app.py                                   # the workload: 12 cities, deterministic output

# 1. the whole loop, with a real agent writing hardened.py (Claude Code, Opus):
ras-anything go --agent claude --model opus --degraded-marker unavailable -- python app.py
```

`go` ran scan → harden (recorded 12 calls, ran `claude -p --model opus`, 34 s) → verify (PASS on the
first try) → enable → bench → report. The agent's code is `.ras/hardens/weather.fetch_forecast/hardened.py`;
its summary is in `agent.log` next to it.

After that run I fixed three things in the tool (bench pairs now ignore the random upstream port when
seeding faults; the vendored runtime classifies EAGAIN as transient; bench reports a strict
"identical output" row and where failures come from). The `go` run above was repeated from scratch
after the port fix. The committed `verify.json`, `bench.json`, `RAS-REPORT.md` and `ras-card.svg`
come from re-measuring the same agent-written hardened.py with the final tool:

```sh
ras-anything verify weather.fetch_forecast
ras-anything enable weather.fetch_forecast
ras-anything bench --degraded-marker unavailable     # 20 seeds, original/hardened interleaved
ras-anything report
ras-anything disable weather.fetch_forecast          # weather.py back byte for byte (committed disabled)
```

Measured (from the commands above):

| | original | hardened |
|---|---:|---:|
| verify: availability under the fault matrix (120 trials) | 32.5% | 100.0% |
| verify: hangs / p99 latency (budget 5 s, virtual clock) | 15 / 60.00s | 0 / 3.60s |
| bench: app availability, 20 seeds | 40.0% | 100.0% |
| bench: crashes / hangs | 45.0% / 15.0% | 0.0% / 0.0% |
| bench: p50 / p99 run time | 94ms / 2.10s | 338ms / 1.04s |

The `unavailable` marker is the text the dashboard already prints for a city the provider has in
maintenance; the hardened function returns that same documented shape (`RAS_FALLBACK`) when the
provider stays down.
