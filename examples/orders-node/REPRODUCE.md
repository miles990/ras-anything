# Reproduce: evening order run (Node.js)

Everything runs offline: `node app.mjs` starts its own shop API (`upstream.mjs`, Node `http`)
on a free localhost port. Like a real provider it honours `Idempotency-Key` and its ledger
reports duplicate charges, which the app prints. Shipping quotes go over `http.get`, labels
are printed by a child process (`label.mjs` via `execFile`). Faults are injected at the app's
side of the boundary; the upstream is always healthy.

Host for the committed numbers: Linux 6.12 x86_64, 8 CPUs, Node v24.22.1, Python 3.13.5 (orchestrator), 2026-11-23 (Asia/Taipei).

```sh
# from the repo root
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
cd examples/orders-node
node app.mjs                                    # 12 orders, deterministic output

ras-anything scan -- node app.mjs
ras-anything harden orders.charge --agent claude --model opus
ras-anything harden orders.getItem --agent claude --model opus
ras-anything harden orders.shippingQuote --agent claude --model opus
ras-anything harden orders.printLabel --agent claude --model opus
ras-anything verify orders.charge
ras-anything verify orders.getItem
ras-anything verify orders.shippingQuote
ras-anything verify orders.printLabel
ras-anything enable orders.charge
ras-anything enable orders.getItem
ras-anything enable orders.shippingQuote
ras-anything enable orders.printLabel
ras-anything bench --seeds 20 --rate 0.1 --degraded-marker PENDING -- node app.mjs
ras-anything report --title orders-node
ras-anything disable orders.charge
ras-anything disable orders.getItem
ras-anything disable orders.shippingQuote
ras-anything disable orders.printLabel          # committed disabled; source restored byte for byte
```

The scan ranked all four exported order functions `harden it`. Each was hardened by a real
Claude Opus coding-agent run (logs under `.ras/hardens/<id>/agent.log`); verify passed on the
first try for every one. The first `orders.getItem` agent run was interrupted before it wrote
the body; a second `--agent claude --model opus` run (keeping the same recording) produced the
committed `hardened.mjs`.

Measured (from the commands above):

| function | happy path | availability under faults | wrong | hangs | p99 (budget 5s) | dups | verdict |
|---|---|---|---|---|---|---|---|
| `orders.charge` | 12/12 identical | 78.3% → **100.0%** | 12 → 0 | 14 → 0 | 10.30s → 3.60s | 28 → 0 | ✓ pass |
| `orders.getItem` | 12/12 identical | 29.2% → **100.0%** | 30 → 0 | 16 → 0 | 60.00s → 4.00s | 0 → 0 | ✓ pass |
| `orders.shippingQuote` | 12/12 identical | 39.2% → **100.0%** | 23 → 0 | 14 → 0 | 60.00s → 3.52s | 0 → 0 | ✓ pass |
| `orders.printLabel` | 12/12 identical | 32.5% → **100.0%** | 0 → 0 | 13 → 0 | 60.00s → 3.52s | 0 → 0 | ✓ pass |

Whole-app bench (20 seeds, p=0.1, `--degraded-marker PENDING`): availability **0.0% → 90.0%**,
identical output 0.0% → 90.0%, crashes 75.0% → 10.0%, hangs 15.0% → 0.0%, wrong output 10.0% → 0.0%,
duplicate charges 6 → 0. Two hardened runs still failed in `app.mjs` itself (TypeError, TimeoutError)
on a call that is not one of the four hardened functions (the ledger `fetch` after the loop).
