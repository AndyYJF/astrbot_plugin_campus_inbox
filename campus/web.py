"""内嵌只读 Web 面板（stdlib http.server，独立线程）。

只提供 GET：单页 HTML + items JSON API。全部请求要 token。
不做任何写接口；DB 只读查询（WAL 并发读安全）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_INDEX_HTML = Path(__file__).parent.parent / "web" / "index.html"

_LOGIN_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>校园收件箱 · 登录</title><style>
body{background:#101012;color:#e6e6e8;font-family:system-ui,sans-serif;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{border:1px solid #26262b;border-radius:12px;padding:32px 28px;width:280px}
h1{font-size:15px;font-weight:600;margin:0 0 4px}p{font-size:12px;color:#8b8b93;margin:0 0 18px}
input{width:100%;box-sizing:border-box;background:#16161a;border:1px solid #2c2c32;color:#e6e6e8;
border-radius:8px;padding:9px 12px;font-size:14px;outline:none}
input:focus{border-color:#4c4c56}
button{width:100%;margin-top:12px;background:#e6e6e8;color:#101012;border:0;border-radius:8px;
padding:9px 0;font-size:14px;font-weight:600;cursor:pointer}
.err{color:#e5484d;font-size:12px;min-height:16px;margin-top:10px}
</style></head><body><div class="card"><h1>校园收件箱</h1><p>输入你的姓名查看班级通知</p>
<input id="name" placeholder="姓名" autocomplete="off"><button id="go">进入</button>
<div class="err" id="err"></div></div><script>
const inp=document.getElementById("name"),err=document.getElementById("err");
async function go(){err.textContent="";
const r=await fetch("/login",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({name:inp.value})});
if(r.ok){location.reload()}else{err.textContent=(await r.json()).error||"登录失败"}}
document.getElementById("go").onclick=go;
inp.addEventListener("keydown",e=>{if(e.key==="Enter")go()});inp.focus();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "CampusInboxPanel/0.1"

    def _handle_login(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "bad body"}, 400)
            return
        name = str(body.get("name") or "").strip()
        names = getattr(self.server, "viewer_names", ())
        if not name or name not in names:
            self._send_json({"error": "名字不在名单里"}, 403)
            return
        cookie = self._viewer_cookie(name, self.server.web_token)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie",
                         f"campus_viewer={cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
        payload = json.dumps({"ok": True, "name": name}).encode()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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

    def _auth_role(self, query: dict) -> str | None:
        """full=owner token（可写）；viewer=名单 cookie（只读）；None=未授权。"""
        token = self.server.web_token
        if not token:
            return None
        if (query.get("token") or [""])[0] and (query.get("token") or [""])[0] == token:
            return "full"
        if self.headers.get("Authorization", "") == f"Bearer {token}":
            return "full"
        return self._viewer_from_cookie()

    def _viewer_from_cookie(self) -> str | None:
        names = getattr(self.server, "viewer_names", ())
        if not names:
            return None
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if not part.startswith("campus_viewer="):
                continue
            value = part[len("campus_viewer="):]
            try:
                b64name, sig = value.rsplit(".", 1)
                name = base64.urlsafe_b64decode(b64name.encode()).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None
            expect = hmac.new(self.server.web_token.encode(),
                              name.encode(), hashlib.sha256).hexdigest()[:32]
            if hmac.compare_digest(sig, expect) and name in names:
                admins = getattr(self.server, "viewer_admins", ())
                return "admin" if name in admins else "viewer"
        return None

    @staticmethod
    def _viewer_cookie(name: str, secret: str) -> str:
        b64name = base64.urlsafe_b64encode(name.encode()).decode()
        sig = hmac.new(secret.encode(), name.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{b64name}.{sig}"

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
        path = parsed.path.rstrip("/")

        if path == "/login":  # 名单登录，不需要预先鉴权
            self._handle_login()
            return

        role = self._auth_role(query)
        if role not in ("full", "admin"):  # 写操作：owner token 或管理员
            self._send_json({"error": "unauthorized"},
                            401 if role is None else 403)
            return
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
        role = self._auth_role(query)

        path = parsed.path.rstrip("/") or "/"
        storage = self.server.storage

        if path == "/logout":  # 清除 viewer cookie，回到登录页
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie",
                             "campus_viewer=; Path=/; HttpOnly; Max-Age=0")
            self.end_headers()
            return

        if path == "/":
            if role is None:
                self._send_html(_LOGIN_HTML)
                return
            try:
                self._send_html(_INDEX_HTML.read_text(encoding="utf-8"))
            except OSError:
                self._send_json({"error": "index.html missing"}, 500)
            return

        if role is None:
            self._send_json({"error": "unauthorized"}, 401)
            return

        if path == "/api/stats":
            self._send_json(storage.panel_stats())
            return

        if path == "/api/me":
            self._send_json({"role": role})
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
            if role == "viewer" and status in ("", "withdrawn"):
                status = "visible"  # 同学视角：进行中+待确认+已完成，不含撤回
            category = (query.get("category") or [""])[0]
            try:
                page = max(int((query.get("page") or ["1"])[0]), 1)
            except ValueError:
                page = 1
            rows, total = storage.list_items(status=status, category=category, page=page)
            mask_done = role == "viewer"  # 同学视角：不暴露完成状态，一律显示未完成
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
                            "status": "active" if mask_done and r["status"] == "done" else r["status"],
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
                if role == "viewer" and detail.get("status") == "done":
                    detail["status"] = "active"  # 同学视角：不暴露完成状态
                self._send_json(detail)
            return

        self._send_json({"error": "not found"}, 404)


class PanelServer:
    """后台线程里的只读面板。start() 非阻塞，stop() 幂等。"""

    def __init__(self, storage, host: str, port: int, token: str, media_dir=None,
                 viewer_names=(), viewer_admins=()):
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.storage = storage
        self._httpd.web_token = token
        self._httpd.media_dir = media_dir
        self._httpd.viewer_names = tuple(viewer_names)
        self._httpd.viewer_admins = tuple(viewer_admins)
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
