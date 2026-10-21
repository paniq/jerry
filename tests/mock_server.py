#!/usr/bin/env python3
"""Mock llama.cpp server for the jerry test suite.

Speaks just enough of the llama.cpp / OpenAI protocol for jerry: GET /v1/models and
POST /v1/chat/completions, either streaming (SSE) or as one JSON document.
Answers are canned and deterministic (so the seed is trivially honoured) and
depend on the prompt:

    "List the numbers 1 to N"   N lines, one token each
    "story"                     a 200 token answer, so the run takes a couple of seconds
    "call the tool"             a tool call, finish_reason "tool_calls"
    anything else               "mock reply for: <prompt>"

Message content is taken either as a plain string or as a multimodal content
array; the array is flattened to text, with one <image> marker per image so the
canned answer can show that the attachments arrived.

Unless the request sets enable_thinking to false, an answer starts with 12
reasoning tokens (reasoning_content). Reasoning counts towards max_tokens, and
an answer that does not fit ends with finish_reason "length", like llama.cpp.

Run it by hand (the test suite starts its own copy on a free port):

    MOCK_PORT=8081 python3 tests/mock_server.py
"""

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("MOCK_HOST", "127.0.0.1")
PORT = int(os.environ.get("MOCK_PORT", "8081"))
TOKEN_DELAY = float(os.environ.get("MOCK_TOKEN_DELAY", "0.01"))   # seconds per streamed token

STORY = "the old key turned in the lock and the door opened onto a quiet garden".split()

# llama.cpp charges a vision slot for every image tile, so prompt_tokens is not
# just the text length divided by four. This is a stand-in, not a real count.
VISION_TOKENS = 256


def prompt_text(content):
    """Flatten message content (a string, or a multimodal content array) to text."""
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if part.get("type") == "text":
            parts.append(part.get("text", ""))
        elif part.get("type") == "image_url":
            parts.append("<image>")
    return " ".join(p for p in parts if p)


def count_images(content):
    """Number of image entries in a content array (0 for plain string content)."""
    if isinstance(content, str):
        return 0
    return sum(1 for part in content or [] if part.get("type") == "image_url")


def plan(prompt, req):
    """-> (reasoning tokens, content tokens, tool call or None)"""
    low = prompt.lower()
    if "call the tool" in low:
        return [], [], {"name": "get_weather", "arguments": '{"city": "Paris"}'}
    reasoning = []
    if req.get("enable_thinking") is not False:
        reasoning = ["pondering the question, step %d. " % i for i in range(1, 13)]
    numbers = re.search(r"list the numbers 1 to (\d+)", low)
    if numbers:
        return reasoning, ["%d\n" % i for i in range(1, int(numbers.group(1)) + 1)], None
    if "story" in low:
        return reasoning, [STORY[i % len(STORY)] + " " for i in range(200)], None
    return reasoning, re.findall(r"\S+\s*", "mock reply for: " + prompt[:100]), None


def answer(prompt, req):
    """-> ([(field, token), ...] within max_tokens, tool call, finish_reason)"""
    reasoning, content, call = plan(prompt, req)
    seq = [("reasoning_content", t) for t in reasoning] + [("content", t) for t in content]
    finish = "tool_calls" if call else "stop"
    limit = req.get("max_tokens")
    if isinstance(limit, int) and 0 < limit < len(seq):
        seq, finish = seq[:limit], "length"
    return seq, call, finish


def stats(prompt, count, images=0):
    usage = {"prompt_tokens": max(1, len(prompt) // 4) + images * VISION_TOKENS,
             "completion_tokens": count}
    usage["total_tokens"] = usage["prompt_tokens"] + count
    timings = {"prompt_ms": 50.0, "predicted_ms": max(1.0, count * TOKEN_DELAY * 1000.0)}
    return usage, timings


class Handler(BaseHTTPRequestHandler):
    """Serve /v1/models and /v1/chat/completions with canned responses."""

    def log_message(self, fmt, *args):
        pass  # silence stderr

    def do_GET(self):
        if self.path == "/v1/models":
            self._json(200, {"data": [{"id": "mock-model", "object": "model"}], "object": "list"})
        else:
            self._json(404, None)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._json(404, None)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) if length else b"{}")
        except ValueError:
            req = {}
        messages = req.get("messages") or [{}]
        content = messages[0].get("content") or ""
        prompt = prompt_text(content)
        seq, call, finish = answer(prompt, req)
        usage, timings = stats(prompt, len(seq), count_images(content))
        if req.get("stream", True):
            self._stream(seq, call, finish, usage, timings)
        else:
            self._whole(seq, call, finish, usage, timings)

    def _json(self, code, body):
        data = json.dumps(body or {}).encode()
        self.send_response_only(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def _whole(self, seq, call, finish, usage, timings):
        message = {"role": "assistant"}
        for field in ("content", "reasoning_content"):
            text = "".join(t for f, t in seq if f == field)
            if text:
                message[field] = text
        if call:
            message["tool_calls"] = [{"type": "function", "function": call}]
        self._json(200, {"choices": [{"index": 0, "message": message, "finish_reason": finish}],
                         "usage": usage, "timings": timings})

    def _stream(self, seq, call, finish, usage, timings):
        self.send_response_only(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(delta, **extra):
            chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": extra.pop("finish", None)}]}
            chunk.update(extra)
            self.wfile.write(("data: %s\n\n" % json.dumps(chunk)).encode("utf-8"))
            self.wfile.flush()

        try:
            send({"role": "assistant", "content": ""})
            for field, token in seq:
                time.sleep(TOKEN_DELAY)
                send({field: token})
            if call:
                send({"tool_calls": [{"type": "function", "function": call}]})
            send({}, finish=finish, usage=usage, timings=timings)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            pass  # the client hung up (broken pipe, SIGINT): nothing more to send


def start(host="127.0.0.1", port=0):
    """Run the mock in a background thread. -> (server, url); port 0 picks a free one."""
    httpd = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://%s:%d" % (host, httpd.server_address[1])


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("mock server on %s:%d" % (HOST, PORT), file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
