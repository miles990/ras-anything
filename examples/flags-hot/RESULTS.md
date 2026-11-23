# Flag hot-update

A process already running reads `.ras/flags.json` on every evaluation (mtime poll, no restart).
This script flips a boolean flag 80 times and measures wall time from the write to the first call that sees the new value. Dropped or failed switches: **0**.

| n | observed | dropped | p50 | p99 | max |
|---|---|---|---|---|---|
| 80 | 80 | 0 | 0.0001 s | 0.0002 s | 0.0002 s |
