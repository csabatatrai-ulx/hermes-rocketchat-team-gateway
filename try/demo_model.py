#!/usr/bin/env python3
"""A scripted stand-in for a language model, so the try-it stack works without any account or API key.

It is not AI: it answers with fixed text. What it lets you see is everything around the model, which is what
this plugin does: who gets which answer, the command guard, and the approval buttons. It speaks the
OpenAI-compatible chat API that Hermes uses, streaming included.

- A message that asks to delete, remove or clean up something gets a terminal tool call
  (`rm -rf /tmp/hermes-demo-old-build`), which Hermes flags and holds for an approval.
- After the tool ran, it reports the command's output.
- Anything else gets a short fixed answer that repeats the request.

GET /seen?text=WORD tells how many requests so far contained WORD in a person's message, so try/check.py can
prove that a refused command never reached the model. Standard library only.
"""
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COMMAND = "rm -rf /tmp/hermes-demo-old-build && echo removed"
SEEN = []  # the person's text of every request


def text_of(content):
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def request_text(messages):
    """The person's own words: the last user message, after any room context the adapter put in front."""
    users = [m for m in messages if m.get("role") == "user"]
    last = text_of(users[-1]["content"]) if users else ""
    return last.rsplit("[New message]", 1)[-1].strip()


def chunk(delta, finish=None):
    return {"id": "demo", "object": "chat.completion.chunk", "created": 0, "model": "demo",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/seen":
            word = urllib.parse.parse_qs(url.query).get("text", [""])[0].lower()
            return self._send(200, json.dumps({"count": sum(word in t.lower() for t in SEEN)}).encode())
        self._send(200, json.dumps({"object": "list", "data": [
            {"id": "demo", "object": "model", "context_length": 128000}]}).encode())

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        messages = req.get("messages", [])
        asked = request_text(messages)
        SEEN.append(asked)
        # A tool result since the person's last message means the command already ran (Hermes may add a note
        # after the result, so the tool message is not always the last one).
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        tool_results = [m for m in messages[last_user + 1:] if m.get("role") == "tool"]
        wants_delete = any(w in asked.lower() for w in ("delete", "remove", "clean up", "rm "))

        if wants_delete and not tool_results:
            call = {"index": 0, "id": "call_demo", "type": "function",
                    "function": {"name": "terminal", "arguments": json.dumps({"command": COMMAND})}}
            frames = [chunk({"role": "assistant", "content": None, "tool_calls": [call]}),
                      chunk({}, "tool_calls")]
            return self._stream(req, frames, tool_call=call)

        if tool_results:
            output = text_of(tool_results[-1].get("content"))
            try:  # Hermes's terminal tool returns {"output": ..., "exit_code": ...}
                output = json.loads(output).get("output", output)
            except (ValueError, AttributeError):
                pass
            output = str(output).strip()[:200]
            answer = f"Done. The command ran and printed: {output}"
        else:
            answer = (f"Demo model (scripted, not AI): you asked \"{asked[:120]}\". The plugin routed your message "
                      "to Hermes and this answer back to you. Ask me to delete something to see the approval buttons.")
        return self._stream(req, [chunk({"role": "assistant", "content": answer}), chunk({}, "stop")], text=answer)

    def _stream(self, req, frames, text=None, tool_call=None):
        if req.get("stream"):
            frames[-1]["usage"] = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
            return self._send(200, body.encode(), "text/event-stream")
        message = {"role": "assistant", "content": text}
        if tool_call:
            message["tool_calls"] = [{k: v for k, v in tool_call.items() if k != "index"}]
        finish = "tool_calls" if tool_call else "stop"
        self._send(200, json.dumps({"id": "demo", "object": "chat.completion", "created": 0, "model": "demo",
                                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode())


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
