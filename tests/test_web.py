"""Web 面板测试：token 鉴权、items API、详情、统计。起真实 HTTP 服务在随机端口。"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from campus.extract import validate_items
from campus.models import NormalizedMessage, SourceKey, build_message_key
from campus.storage import Storage
from campus.web import PanelServer

SRC = SourceKey("campus-main", "1000001", "9000001")
TOKEN = "test-token-123"

# 本机 HTTP_PROXY 会劫持 localhost 请求导致卡死，测试一律绕过代理
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def make_server(tmp: str):
    storage = Storage(Path(tmp) / "test.db")
    storage.ensure_subscription(SRC, "测试群")
    server = PanelServer(storage, "127.0.0.1", 0, TOKEN)  # 端口 0 = 随机
    server.start()
    return storage, server


class WebTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage, self.server = make_server(self.tmp.name)
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        self.server.stop()
        self.storage.close()
        self.tmp.cleanup()

    def get(self, path: str, token: str | None = TOKEN):
        url = f"{self.base}{path}"
        sep = "&" if "?" in url else "?"
        if token is not None:
            url += f"{sep}token={token}"
        try:
            with _NO_PROXY.open(url, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, {}


    def post(self, path: str, body: dict, token: str | None = TOKEN):
        url = f"{self.base}{path}"
        sep = "&" if "?" in url else "?"
        if token is not None:
            url += f"{sep}token={token}"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with _NO_PROXY.open(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(body_raw)
            except json.JSONDecodeError:
                return e.code, {}


class TestAuth(WebTestBase):
    def test_no_token_rejected(self):
        status, _ = self.get("/api/items", token=None)
        self.assertEqual(status, 401)

    def test_wrong_token_rejected(self):
        status, _ = self.get("/api/items", token="wrong")
        self.assertEqual(status, 401)

    def test_empty_server_token_rejects_all(self):
        server = PanelServer(self.storage, "127.0.0.1", 0, "")
        server.start()
        try:
            url = f"http://127.0.0.1:{server.port}/api/stats"
            with self.assertRaises(urllib.error.HTTPError) as cm:
                _NO_PROXY.open(url, timeout=5)
            self.assertEqual(cm.exception.code, 401)
        finally:
            server.stop()

    def test_index_requires_token(self):
        status, _ = self.get("/", token=None)
        self.assertEqual(status, 401)


class TestApi(WebTestBase):
    def _seed_item(self) -> str:
        sent_at = datetime.now(timezone.utc).isoformat()
        key, _ = build_message_key(SRC, "r1", "同学甲", sent_at, "周五交报告")
        self.storage.insert_message(
            NormalizedMessage(
                message_key=key, source=SRC, remote_id="r1",
                sender_alias="同学甲", sent_at=sent_at, received_at=sent_at,
                text="周五交报告",
            )
        )
        items = validate_items(
            {
                "items": [
                    {
                        "title": "交实验报告",
                        "summary": "周五截止",
                        "category": "assignment",
                        "relevance": "relevant",
                        "due_date": "2026-09-11",
                        "source_refs": ["M1"],
                    }
                ]
            },
            [key],
        )
        self.storage.create_batch("b1", [key], "m", "v1")
        self.storage.insert_items(items, "b1")
        self.storage.finish_batch("b1")
        return items[0]["item"]["item_id"]

    def test_stats(self):
        self._seed_item()
        status, data = self.get("/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(data["items"], 1)
        self.assertEqual(data["messages"], 1)
        self.assertEqual(data["batches_succeeded"], 1)

    def test_items_list_and_filter(self):
        self._seed_item()
        status, data = self.get("/api/items")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["title"], "交实验报告")
        # 分类过滤
        _, data = self.get("/api/items?category=exam")
        self.assertEqual(data["total"], 0)
        _, data = self.get("/api/items?category=assignment")
        self.assertEqual(data["total"], 1)

    def test_item_detail_with_sources(self):
        item_id = self._seed_item()
        status, d = self.get(f"/api/item/{item_id}")
        self.assertEqual(status, 200)
        self.assertEqual(d["title"], "交实验报告")
        self.assertEqual(len(d["sources"]), 1)
        self.assertEqual(d["sources"][0]["text"], "周五交报告")
        self.assertEqual(len(d["revisions"]), 1)

    def test_item_404(self):
        status, _ = self.get("/api/item/nonexistent")
        self.assertEqual(status, 404)

    def test_set_status_done(self):
        item_id = self._seed_item()
        status, data = self.post(f"/api/item/{item_id}/status", {"status": "done"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        _, d = self.get(f"/api/item/{item_id}")
        self.assertEqual(d["status"], "done")
        self.assertEqual(len(d["revisions"]), 2)

    def test_set_status_bad_value(self):
        item_id = self._seed_item()
        status, _ = self.post(f"/api/item/{item_id}/status", {"status": "bogus"})
        self.assertEqual(status, 400)

    def test_set_status_unknown_item(self):
        status, _ = self.post("/api/item/nope/status", {"status": "done"})
        self.assertEqual(status, 404)

    def test_set_status_requires_token(self):
        item_id = self._seed_item()
        status, _ = self.post(f"/api/item/{item_id}/status", {"status": "done"}, token=None)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()


class TestMedia(WebTestBase):
    def setUp(self):
        super().setUp()
        self.media_dir = Path(self.tmp.name) / "media"
        self.media_dir.mkdir()
        (self.media_dir / "pic.jpg").write_bytes(b"\xff\xd8fake-jpeg")
        self.server._httpd.media_dir = self.media_dir

    def test_serve_ok(self):
        with _NO_PROXY.open(f"{self.base}/media/pic.jpg?token={TOKEN}") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get_content_type(), "image/jpeg")
            self.assertEqual(resp.read(), b"\xff\xd8fake-jpeg")

    def test_no_token_401(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            _NO_PROXY.open(f"{self.base}/media/pic.jpg")
        self.assertEqual(cm.exception.code, 401)

    def test_traversal_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            _NO_PROXY.open(f"{self.base}/media/..%2Ftest.db?token={TOKEN}")
        self.assertEqual(cm.exception.code, 404)

    def test_missing_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            _NO_PROXY.open(f"{self.base}/media/nope.jpg?token={TOKEN}")
        self.assertEqual(cm.exception.code, 404)
