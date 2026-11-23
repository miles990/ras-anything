"""Offline OpenAI-compatible mock LLM HTTP server ($0 API spend)."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


class MockProviderHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except ValueError:
            self.send_error(400, "invalid json")
            return
        self.calls.append(req)
        if self.path != "/v1/chat/completions":
            self.send_error(404, "not found")
            return

        model = str(req.get("model") or "small")
        msgs = req.get("messages") or []
        has_tool = any(isinstance(m, dict) and m.get("role") == "tool" for m in msgs)

        if has_tool:
            tool_text = " ".join(str(m.get("content") or "") for m in msgs if isinstance(m, dict) and m.get("role") == "tool")
            if "30 days" in tool_text or "returns" in tool_text:
                content = json.dumps({
                    "answer": "Eligible for a 30-day return under standard policy.",
                    "category": "returns",
                    "priority": "normal",
                })
            else:
                content = json.dumps({
                    "answer": "Ticket reviewed against support policy.",
                    "category": "general",
                    "priority": "normal",
                })
            message = {"role": "assistant", "content": content}
            finish_reason = "stop"
        else:
            user_text = " ".join(str(m.get("content") or "") for m in msgs if isinstance(m, dict))
            topic = "billing" if "invoice" in user_text.lower() or "charge" in user_text.lower() else "returns"
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_policy_1",
                    "type": "function",
                    "function": {"name": "lookup_policy", "arguments": json.dumps({"topic": topic})},
                }],
            }
            finish_reason = "tool_calls"

        payload = {
            "id": "chatcmpl-mock-1",
            "object": "chat.completion",
            "model": model,
            "usage": {"prompt_tokens": 42, "completion_tokens": 24, "total_tokens": 66},
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_server():
    MockProviderHandler.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockProviderHandler)
    t = Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t
