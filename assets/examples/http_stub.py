"""可选：把记忆挂成本地 HTTP 接口，供非 Python 宿主调用。

仅用于开发/本机集成，不是生产服务：没有鉴权、没有并发控制。
生产请用宿主进程内调用，或在外面加一层你自己的网关。
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_core import MemorySystem, Scope  # noqa: E402
from memory_core.tools import dispatch  # noqa: E402

SYSTEM = MemorySystem.open(Path(__file__).resolve().parent.parent)


def _scope(payload: dict) -> Scope:
    return Scope(
        folder_id=payload.get("folder_id", "default"),
        person_id=payload.get("person_id", ""),
        chat_type=payload.get("chat_type", ""),
    )


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        scope = _scope(payload)
        if self.path == "/prefetch":
            result = SYSTEM.prefetch(scope, payload.get("query", ""))
        elif self.path == "/reminder":
            result = SYSTEM.reminder(scope)
        elif self.path == "/tool":
            result = dispatch(SYSTEM, scope, payload.get("name", ""), payload.get("arguments", {}))
        elif self.path == "/extract":
            result = SYSTEM.extract_after_turn(scope, payload.get("user_text", ""), payload.get("message_id", ""))
        else:
            self.send_error(404)
            return
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默
        return


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 8765), Handler)
    print("listening on http://127.0.0.1:8765")
    server.serve_forever()
