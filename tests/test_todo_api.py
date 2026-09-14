"""T09 Todo API：契约 v1 形状的任务拉取 + 状态回写。"""

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

TOKEN = "test-token"
SRC = SourceKey("campus-main", "1000001", "9000001")
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class TodoApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")
        self.storage.ensure_subscription(SRC, "西园七舍")
        sent = datetime.now(timezone.utc).isoformat()
        key, _ = build_message_key(SRC, "m1", "甲", sent, "周五前交实验报告")
        self.storage.insert_message(NormalizedMessage(
            message_key=key, source=SRC, remote_id="m1", sender_alias="甲",
            sent_at=sent, received_at=sent, text="周五前交实验报告"))
        items = validate_items({"items": [{
            "title": "交实验报告", "summary": "周五前提交", "category": "assignment",
            "action_text": "提交", "due_date": "2026-09-12",
            "source_refs": ["M1"]}]}, [key])
        self.storage.create_batch("b0", [key], "m", "v1")
        self.storage.insert_items(items, "b0")
        self.item_id = items[0]["item"]["item_id"]
        self.server = PanelServer(self.storage, "127.0.0.1", 0, TOKEN)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        self.server.stop()
        self.storage.close()
        self.tmp.cleanup()

    def _get(self, path, token=TOKEN):
        req = urllib.request.Request(f"{self.base}{path}?token={token}" if "?" not in path
                                     else f"{self.base}{path}&token={token}")
        with _NO_PROXY.open(req) as resp:
            return json.loads(resp.read())

    def _post(self, path, body, token=TOKEN):
        req = urllib.request.Request(
            f"{self.base}{path}?token={token}", method="POST",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with _NO_PROXY.open(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_tasks_shape(self):
        data = self._get("/api/todo/v1/tasks")
        self.assertEqual(data["schema_version"], 1)
        self.assertIn("generated_at", data)
        t = data["tasks"][0]
        self.assertEqual(t["task_id"], self.item_id)
        self.assertEqual(t["title"], "交实验报告")
        self.assertEqual(t["status"], "open")
        self.assertEqual(t["due_date"], "2026-09-12")
        self.assertIsNone(t["due_at"])
        self.assertEqual(t["timezone"], "Asia/Shanghai")
        self.assertEqual(t["sources"][0]["group_alias"], "西园七舍")
        self.assertIn("周五前交实验报告", t["sources"][0]["text"])
        self.assertEqual(t["sources"][0]["media"], [])
        # 隐私：仍不含群号/QQ
        blob = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("9000001", blob)
        self.assertNotIn("1000001", blob)

    def test_status_filter(self):
        self.assertEqual(len(self._get("/api/todo/v1/tasks?status=completed")["tasks"]), 0)
        self.storage.set_item_status(self.item_id, "done", "测试")
        self.assertEqual(len(self._get("/api/todo/v1/tasks")["tasks"]), 0)
        self.assertEqual(len(self._get("/api/todo/v1/tasks?status=completed")["tasks"]), 1)
        self.assertEqual(len(self._get("/api/todo/v1/tasks?status=all")["tasks"]), 1)

    def test_updated_since(self):
        future = "2099-01-01T00:00:00+00:00"
        self.assertEqual(len(self._get(
            f"/api/todo/v1/tasks?updated_since={future}")["tasks"]), 0)
        self.assertEqual(len(self._get(
            "/api/todo/v1/tasks?updated_since=2000-01-01")["tasks"]), 1)

    def test_writeback_with_version(self):
        code, data = self._post(f"/api/todo/v1/tasks/{self.item_id}/status",
                                {"status": "completed", "version": 1})
        self.assertEqual(code, 200)
        self.assertEqual(data["version"], 2)
        self.assertEqual(self.storage.get_item_detail(self.item_id)["status"], "done")

    def test_writeback_stale_version_412(self):
        code, data = self._post(f"/api/todo/v1/tasks/{self.item_id}/status",
                                {"status": "completed", "version": 99})
        self.assertEqual(code, 412)
        self.assertEqual(data["current_revision"], 1)

    def test_writeback_errors(self):
        code, _ = self._post(f"/api/todo/v1/tasks/{self.item_id}/status", {"status": "bogus"})
        self.assertEqual(code, 400)
        code, _ = self._post("/api/todo/v1/tasks/nope/status", {"status": "completed"})
        self.assertEqual(code, 404)
        code, _ = self._post(f"/api/todo/v1/tasks/{self.item_id}/status",
                             {"status": "completed"}, token="wrong")
        self.assertEqual(code, 401)


if __name__ == "__main__":
    unittest.main()
