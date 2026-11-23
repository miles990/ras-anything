# Reproduce: shipping-java (Java)

Everything runs offline. `shipping.Main` starts its own carrier (`com.sun.net.httpserver`, 127.0.0.1, a free port, honours
`Idempotency-Key`), rates each parcel (`HttpClient` GET), books it (`HttpClient` POST) and gets its label code from a program
(`ProcessBuilder`, `sh tools/label.sh`). Faults are injected at the app's side of the boundary; the carrier is always healthy.

Host for the committed numbers: Linux x86_64 (8 CPUs), OpenJDK 21.0.13, Python 3.13 (orchestrator), 2026-11-23 (Asia/Taipei).
The hardened code was written by Claude Code (Opus) through `--agent claude --model opus`.

```sh
# from the repo root
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
cd examples/shipping-java
javac -d out $(find src -name '*.java') && java -cp out shipping.Main     # 6 parcels, always the same output

ras-anything scan -- java -cp out shipping.Main
for f in Carrier.fetchRate Carrier.bookShipment Labels.labelCode; do
  ras-anything harden shipping.$f --agent claude --model opus             # keeps an existing hardened.java
  ras-anything verify shipping.$f
  ras-anything enable shipping.$f
done
ras-anything bench --seeds 20 --rate 0.1 --slo "availability=95.0,p99=3s,error_rate=5%" -- java -cp out shipping.Main
ras-anything report --title shipping-java
for f in Carrier.fetchRate Carrier.bookShipment Labels.labelCode; do ras-anything disable shipping.$f; done
```

ras-anything compiles an instrumented copy of the sources itself (before a run is timed), so `out/` only matters for running the app
without it. To run an enabled app yourself, compile again: the swap is in the sources.

What to expect: the fault decisions are seeded, so outcomes repeat; wall times don't exactly. The p99 target (3 s) is **missed** by the
hardened app: the worst run waits out several real `Retry-After: 1` answers. About half a second of every run is the fake carrier shutting down.
