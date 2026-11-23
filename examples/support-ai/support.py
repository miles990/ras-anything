"""Un-hardened AI support triage worker (calls LLM + local tools without timeouts, budgets, or guards)."""

from __future__ import annotations

import json
import urllib.request

TOOLS = {
    "lookup_policy": lambda args: {
        "policy": "30 days full refund" if args.get("topic") == "returns" else "billing dispute within 14 days"
    },
    "lookup_ticket": lambda args: {"status": "open", "tier": "standard"},
    "issue_refund": lambda args: {"refunded": True, "amount": args.get("amount", 0)},
}


def read_optional_note(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def triage_ticket(base_url: str, ticket_id: str, document: str) -> dict:
    messages = [
        {"role": "system", "content": "Classify support tickets as JSON with keys answer, category, priority."},
        {"role": "user", "content": f"Ticket {ticket_id}: {document}"},
    ]
    tools_used: list[str] = []
    while True:
        body = json.dumps({"model": "small", "messages": messages}).encode("utf-8")
        req = urllib.request.Request(
            base_url.rstrip("/") + "/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:  # no timeout, no retry/fallback, no budget
            data = json.loads(resp.read().decode("utf-8"))
        msg = data["choices"][0]["message"]
        tcs = msg.get("tool_calls") or []
        if tcs:
            messages.append(msg)
            for tc in tcs:
                name = tc["function"]["name"]
                args = json.loads(tc["function"]["arguments"] or "{}")
                tools_used.append(name)
                out = TOOLS[name](args)  # no allowlist or loop guard
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(out)})
            continue
        parsed = json.loads(msg["content"])
        return {
            "ticket_id": ticket_id,
            "answer": parsed["answer"],
            "category": parsed["category"],
            "priority": parsed["priority"],
            "tools": tools_used,
        }
