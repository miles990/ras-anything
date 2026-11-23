# Reproducing `examples/support-ai`

100% offline ($0 API cost) using the in-process local mock OpenAI server (`mock_provider.py`).

```bash
cd examples/support-ai
ras-anything scan -- python3 app.py
ras-anything harden support.triage_ticket -- python3 app.py
ras-anything verify support.triage_ticket
ras-anything enable support.triage_ticket
ras-anything bench --seeds 20 -- python3 app.py
ras-anything report --title support-ai --slo availability=99.0,p99=5s,error_rate=1%
ras-anything drift --model backup --eval support@v1 --export-promptfoo .ras/evals/support-promptfoo.yaml
ras-anything disable support.triage_ticket
```
