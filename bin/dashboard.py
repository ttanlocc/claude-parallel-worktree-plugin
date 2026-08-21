#!/usr/bin/env python3
"""Live status/log dashboard for parallel-task.sh copies."""

import json


def _flatten_tool_result(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(parts)
    return ""


def render_transcript_line(obj: dict) -> list[str]:
    """Return zero or more human-readable log lines for one transcript JSON record."""
    if obj.get("type") not in ("user", "assistant"):
        return []
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    lines = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text", "")
            if text:
                lines.append(text)
        elif item_type == "tool_use":
            name = item.get("name", "?")
            args = json.dumps(item.get("input", {}))[:80]
            lines.append(f"→ {name}({args})")
        elif item_type == "tool_result":
            lines.append(f"← {_flatten_tool_result(item.get('content'))[:200]}")
    return lines


def render_task_log(transcript_path: str, limit: int = 200) -> list[str]:
    """Read a session transcript JSONL file and return the last `limit` rendered log lines."""
    lines: list[str] = []
    with open(transcript_path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            lines.extend(render_transcript_line(obj))
    return lines[-limit:]


import http.server
import os
import subprocess
import sys
from urllib.parse import urlparse

PLUGIN_BIN = os.path.dirname(os.path.abspath(__file__))
PARALLEL_TASK_SH = os.path.join(PLUGIN_BIN, "parallel-task.sh")


def get_tasks() -> list[dict]:
    result = subprocess.run([PARALLEL_TASK_SH, "list", "--json"], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _slugify_cwd(path: str) -> str:
    return path.replace("/", "-").replace(".", "-")


def transcript_path_for(task: dict) -> str | None:
    session_id = task.get("session_id")
    wt_path = task.get("path")
    if not session_id or not wt_path:
        return None
    home = os.path.expanduser("~")
    return os.path.join(home, ".claude", "projects", _slugify_cwd(wt_path), f"{session_id}.jsonl")


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/tasks":
            self._json(get_tasks())
            return
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/log"):
            name = parsed.path[len("/api/tasks/") : -len("/log")]
            tasks = {t["task"]: t for t in get_tasks()}
            task = tasks.get(name)
            path = transcript_path_for(task) if task else None
            if not path or not os.path.exists(path):
                self._json({"status": "not-available"})
                return
            self._json({"lines": render_task_log(path)})
            return
        if parsed.path == "/":
            html_path = os.path.join(PLUGIN_BIN, "dashboard.html")
            if not os.path.exists(html_path):
                self.send_response(404)
                self.end_headers()
                return
            with open(html_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # ponytail: quiet by default; add real logging if this needs debugging later


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4400
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
