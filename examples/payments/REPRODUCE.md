# Reproduce: nightly billing run

Everything runs offline: `app.py` starts its own payment API (`upstream.py`, stdlib `http.server`) in a
thread on a free localhost port. Like real providers it honours `Idempotency-Key` and its ledger
reports duplicate charges, which the app prints. Receipts are rendered by a separate process
(`render.py`). Faults are injected at the app's side of the boundary; the upstream is always healthy.

Host for the committed numbers: Linux 6.12 x86_64, 8 CPUs, Python 3.13.5, 2026-11-23.

```sh
# from the repo root
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
cd examples/payments
python app.py                                   # the workload: 16 invoices, deterministic output

# 1. the loop for the top function (billing.charge), real agent (Claude Code, Opus):
ras-anything go --agent claude --model opus -- python app.py

# 2. the second weak spot the scan ranked (billing.render_receipt: subprocess with no timeout):
ras-anything harden billing.render_receipt --agent claude --model opus
```

`go` hardened `billing.charge` (agent run: `.ras/hardens/billing.charge/agent.log`); verify passed on
the first try. Its bench showed app availability unchanged (5% → 5%) because `render_receipt` still
crashed or hung the app, so I hardened that one too.

The first verify of the agent's `render_receipt` failed for a tool bug, which the agent itself
diagnosed in its log: the recording ran under `.venv/bin/python` and the replay under
`.venv/bin/python3`, so no recorded command matched. I fixed the tool to replay under the recorded
interpreter, then re-recorded without touching the agent's code. I also fixed `runtime.is_transient`
to treat EAGAIN (`BlockingIOError`) as transient:

```sh
ras-anything harden billing.render_receipt      # re-record; keeps the agent's hardened.py
ras-anything verify billing.render_receipt      # PASS
ras-anything verify billing.charge              # PASS (re-measured with the final tool)
ras-anything enable billing.charge
ras-anything enable billing.render_receipt
ras-anything bench                              # 20 seeds, no degraded marker
ras-anything report
ras-anything disable billing.charge && ras-anything disable billing.render_receipt   # committed disabled
```

Measured (from the commands above):

| | original | hardened |
|---|---:|---:|
| verify `billing.charge`: availability (160 trials) | 76.9% | 100.0% |
| verify `billing.charge`: duplicate charges | 38 | 0 |
| verify `billing.render_receipt`: availability (160 trials) | 33.1% | 100.0% |
| bench: app availability (correct or declared-degraded), 20 seeds | 5.0% | 90.0% |
| bench: byte-identical output | 5.0% | 50.0% |
| bench: crashes / hangs / wrong output | 60.0% / 15.0% / 20.0% | 10.0% / 0.0% / 0.0% |
| bench: duplicate charges | 5 | 0 |
| bench: p50 / p99 run time | 1.18s / 3.63s | 1.27s / 3.85s |

Degraded runs are runs where `charge` returned its declared fallback (`None`, which the app already
prints as `PENDING (payment failed, retry tomorrow)`; safe now because the retry tomorrow reuses the
same Idempotency-Key). The two remaining hardened crashes are the ledger read in `app.main`, which was
not hardened (the scan ranks it 0.7).

Wall times on this host are noisy: in one earlier bench run two runs took 10.7 s and 20.9 s, and both
seeds took 1.2 s and 1.9 s when re-run alone. The committed run is the one shown above.
