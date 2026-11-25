#!/usr/bin/env python3
"""A stand-in for an OpenAI-compatible model API, for testing the verifier with real OpenCode.

It streams a short text reply to every request. When the latest user message contains
VJOB, it first asks for one bash tool call that starts a long job through vjob, and
replies with text once the tool result comes back. A line `BASH: COMMAND` in it asks
for that command instead. Every request is appended to the
log file as one JSON line, so a test can see what OpenCode sent.

    python3 fake_model.py PORT LOGFILE
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG_PATH = int(sys.argv[1]), sys.argv[2]
VJOB_COMMAND = "vjob start sleeper -- sleep 300"


def text_of(message: dict) -> str:
    """The text of one chat message, whether its content is a string or a list of parts."""
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def requested_command(text: str) -> str | None:
    """The bash command a user message asks for, if any."""
    for line in text.splitlines():
        if "BASH: " in line:
            return line.split("BASH: ", 1)[1].strip()
    return VJOB_COMMAND if "VJOB" in text else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "m1", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def chunk(self, obj: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        self.wfile.flush()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        messages = body.get("messages", [])
        last = messages[-1] if messages else {}
        system = " ".join(text_of(m) for m in messages if m.get("role") == "system")
        with open(LOG_PATH, "a") as log:
            tools = [t["function"]["name"] for t in body.get("tools") or []]
            log.write(json.dumps({"t": time.time(), "last_role": last.get("role"),
                                  "last": text_of(last)[:2000],
                                  "last_length": len(text_of(last)), "system": system,
                                  "tools": tools})
                      + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        base = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()),
                "model": "m1"}
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        command = requested_command(text_of(last)) if last.get("role") == "user" else None
        if command and body.get("tools"):
            call = {"index": 0, "id": f"call_{time.time_ns()}", "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps(
                        {"command": command, "description": "run a command"})}}
            self.chunk({**base, "choices": [{"index": 0, "delta": {"role": "assistant",
                                                                    "tool_calls": [call]},
                                             "finish_reason": None}]})
            self.chunk({**base, "choices": [{"index": 0, "delta": {},
                                             "finish_reason": "tool_calls"}], "usage": usage})
        else:
            self.chunk({**base, "choices": [{"index": 0, "delta": {"role": "assistant",
                                                                    "content": "Round done."},
                                             "finish_reason": None}]})
            self.chunk({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": usage})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
