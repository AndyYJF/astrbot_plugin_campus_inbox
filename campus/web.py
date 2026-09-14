"""内嵌只读 Web 面板（stdlib http.server，独立线程）。

只提供 GET：单页 HTML + items JSON API。全部请求要 token。
不做任何写接口；DB 只读查询（WAL 并发读安全）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_INDEX_HTML = Path(__file__).parent.parent / "web" / "index.html"


class _Handler(BaseHTTPRequestHandler):
    server_version = "CampusInboxPanel/0.1"

    # ---- 工具 ----

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query: dict) -> bool:
        token = self.server.web_token
        if not token:
            return False  # 未配置 token 一律拒绝
        if (query.get("token") or [""])[0] == token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {token}"

    def _send_media(self, name: str) -> None:
        """提供插件 media 目录里的图片；只允许目录内文件，防路径穿越。"""
        media_dir = getattr(self.server, "media_dir", None)
        if media_dir is None:
            self._send_json({"error": "media disabled"}, 404)
            return
        try:
            target = (Path(media_dir) / name).resolve()
            if target.parent != Path(media_dir).resolve() or not target.is_file():
                raise ValueError
        except (OSError, ValueError):
            self._send_json({"error": "not found"}, 404)
            return
        mime = "image/png" if target.suffix.lower() == ".png" else "image/jpeg"
        try:
            data = target.read_bytes()
        except OSError:
            self._send_json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    # ---- 写操作：仅此一个 ----

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._send_json({"error": "unauthorized"}, 401)
            return
        path = parsed.path.rstrip("/")
        if path.startswith("/api/item/") and path.endswith("/status"):
            item_id = path[len("/api/item/"):-len("/status")].rstrip("/")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"error": "bad body"}, 400)
                return
            status = str(body.get("status") or "")
            if status not in ("active", "needs_review", "withdrawn", "done"):
                self._send_json({"error": "bad status"}, 400)
                return
            ok = self.server.storage.set_item_status(item_id, status, "面板手动标记")
            self._send_json({"ok": ok} if ok else {"error": "not found"}, 200 if ok else 404)
            return
        if path.startswith("/api/todo/v1/tasks/") and path.endswith("/status"):
            self._todo_set_status(path)
            return
        self._send_json({"error": "not found"}, 404)

    def _todo_set_status(self, path: str) -> None:
        """POST /api/todo/v1/tasks/<id>/status  {status, version?}

        version 提供时必须与当前 revision 一致，否则 412（契约 If-Match 语义）。
        """
        task_id = path[len("/api/todo/v1/tasks/"):-len("/status")].rstrip("/")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "bad body"}, 400)
            return
        mapping = {"open": "active", "completed": "done", "cancelled": "withdrawn"}
        target = mapping.get(str(body.get("status") or ""))
        if target is None:
            self._send_json({"error": "bad status, 用 open/completed/cancelled"}, 400)
            return
        storage = self.server.storage
        current = storage.get_item_revision(task_id)
        if current is None:
            self._send_json({"error": "not found"}, 404)
            return
        version = body.get("version")
        if version is not None and int(version) != current:
            self._send_json({"error": "version conflict", "current_revision": current}, 412)
            return
        storage.set_item_status(task_id, target, "Todo API 回写")
        self._send_json({"ok": True, "task_id": task_id,
                         "version": storage.get_item_revision(task_id)})

    # ---- 路由 ----

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._send_json({"error": "unauthorized"}, 401)
            return

        path = parsed.path.rstrip("/") or "/"
        storage = self.server.storage

        if path == "/":
            try:
                self._send_html(_INDEX_HTML.read_text(encoding="utf-8"))
            except OSError:
                self._send_json({"error": "index.html missing"}, 500)
            return

        if path == "/api/stats":
            self._send_json(storage.panel_stats())
            return

        if path == "/api/todo/v1/tasks":
            self._send_json({
                "schema_version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "tasks": storage.todo_tasks(
                    status=(query.get("status") or ["open"])[0],
                    updated_since=(query.get("updated_since") or [""])[0],
                ),
            })
            return

        if path.startswith("/media/"):
            self._send_media(path[len("/media/"):])
            return

        if path == "/api/items":
            status = (query.get("status") or [""])[0]
            category = (query.get("category") or [""])[0]
            try:
                page = max(int((query.get("page") or ["1"])[0]), 1)
            except ValueError:
                page = 1
            rows, total = storage.list_items(status=status, category=category, page=page)
            self._send_json(
                {
                    "total": total,
                    "page": page,
                    "items": [
                        {
                            "item_id": r["item_id"],
                            "title": r["title"],
                            "summary": r["summary"],
                            "category": r["category"],
                            "relevance": r["relevance"],
                            "status": r["status"],
                            "due_at": r["due_at"],
                            "due_date": r["due_date"],
                            "event_at": r["event_at"],
                            "time_text": r["time_text"],
                            "revision": r["revision"],
                            "updated_at": r["updated_at"],
                            "uncertain_fields": json.loads(r["uncertain_fields"] or "[]"),
                        }
                        for r in rows
                    ],
                }
            )
            return

        if path.startswith("/api/item/"):
            item_id = path[len("/api/item/"):]
            detail = storage.get_item_detail(item_id)
            if detail is None:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json(detail)
            return

        self._send_json({"error": "not found"}, 404)


class PanelServer:
    """后台线程里的只读面板。start() 非阻塞，stop() 幂等。"""

    def __init__(self, storage, host: str, port: int, token: str, media_dir=None):
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.storage = storage
        self._httpd.web_token = token
        self._httpd.media_dir = media_dir
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="campus-panel", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
