"""T10 多模态：图片 URL 采集 + 多模态 content 构建。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from campus.ai import AIError
from campus.extract import build_user_content, run_extraction_cycle
from campus.ingest import normalize
from campus.models import SourceKey
from campus.storage import Storage

SRC = SourceKey("campus-main", "1000001", "9000001")


class Image:
    def __init__(self, url):
        self.url = url


class Plain:
    def __init__(self, text):
        self.text = text


def _raw(chain):
    return SimpleNamespace(source=SRC, remote_id="r1", sender_alias="甲",
                           sent_at=datetime.now(timezone.utc).isoformat(), chain=chain)


class TestNormalizeImage(unittest.TestCase):
    def test_image_url_collected(self):
        msg = normalize(_raw([Plain("看通知 "), Image("http://img.qq.com/a.jpg")]),
                        lambda s, r: None)
        self.assertEqual(msg.media, ("http://img.qq.com/a.jpg",))
        self.assertIn("[图片]", msg.text)
        self.assertEqual(msg.parse_state, "text")  # 图片已识别，不算未解析

    def test_image_only(self):
        msg = normalize(_raw([Image("http://img.qq.com/a.jpg")]), lambda s, r: None)
        self.assertEqual(msg.media, ("http://img.qq.com/a.jpg",))
        self.assertEqual(msg.text, "[图片]")

    def test_image_without_url_degrades(self):
        msg = normalize(_raw([Image("")]), lambda s, r: None)
        self.assertEqual(msg.media, ())
        self.assertEqual(msg.parse_state, "media_unparsed")


class TestBuildContent(unittest.TestCase):
    def _rows(self, media_json):
        return [{"sender_alias": "甲", "sent_at": "2026-09-09T01:00:00+00:00",
                 "text": "通知见图", "media_json": media_json,
                 "message_key": "k1"}]

    def test_text_only_returns_str(self):
        content = build_user_content(self._rows("[]"), [])
        self.assertIsInstance(content, str)

    def test_image_becomes_parts(self):
        content = build_user_content(
            self._rows('["http://img.qq.com/a.jpg"]'), [],
            downloader=lambda url: ("image/jpeg", "QUJD"))
        self.assertIsInstance(content, list)
        types = [p["type"] for p in content]
        self.assertIn("image_url", types)
        img = next(p for p in content if p["type"] == "image_url")
        self.assertEqual(img["image_url"]["url"], "data:image/jpeg;base64,QUJD")
        self.assertTrue(any("M1 的配图" in p.get("text", "") for p in content))

    def test_download_failure_degrades(self):
        def boom(url):
            raise AIError("网络不通")
        content = build_user_content(
            self._rows('["http://img.qq.com/a.jpg"]'), [], downloader=boom)
        self.assertIsInstance(content, list)
        self.assertTrue(any("下载失败" in p.get("text", "") for p in content))
        self.assertFalse(any(p["type"] == "image_url" for p in content))


class TestCycleWithImage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")
        self.storage.ensure_subscription(SRC, "群")

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def test_full_cycle_multimodal(self):
        msg = normalize(_raw([Image("http://img.qq.com/notice.png")]), lambda s, r: None)
        self.storage.insert_message(msg)

        captured = {}

        def fake_transport(url, headers, payload, timeout):
            captured["payload"] = payload
            return {"choices": [{"message": {"content":
                '{"items": [{"title": "图中通知", "source_refs": ["M1"]}]}'}}]}

        from campus.ai import ExternalAI
        ai = ExternalAI("http://x/v1", "k", "m", transport=fake_transport)
        result = run_extraction_cycle(
            self.storage, ai, 80, 30, downloader=lambda u: ("image/png", "QUJD"))
        self.assertEqual(result, "succeeded")
        user = captured["payload"]["messages"][1]["content"]
        self.assertIsInstance(user, list)
        self.assertTrue(any(p["type"] == "image_url" for p in user))
        rows, _ = self.storage.list_items()
        self.assertEqual(rows[0]["title"], "图中通知")


if __name__ == "__main__":
    unittest.main()


class TestLocalMedia(unittest.TestCase):
    def test_image_class_with_path(self):
        class Image:
            path = "/data/temp/x.jpg"
            file = ""
            url = ""
        msg = normalize(_raw([Image()]), lambda s, r: None)
        self.assertEqual(msg.media, ("/data/temp/x.jpg",))
        self.assertEqual(msg.text, "[图片]")

    def test_persist_media(self):
        from campus.ingest import persist_media
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "a.jpg"
            src.write_bytes(b"\xff\xd8fake")
            out = persist_media(str(src), Path(td) / "media")
            self.assertTrue(out.endswith(".jpg"))
            self.assertEqual(Path(out).read_bytes(), b"\xff\xd8fake")
            # http 原样
            self.assertEqual(persist_media("http://x/a.jpg", Path(td)), "http://x/a.jpg")
            # 不存在 → 空
            self.assertEqual(persist_media(str(Path(td) / "nope.jpg"), Path(td)), "")

    def test_load_local_b64(self):
        from campus.ai import load_image_b64
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.jpg"
            p.write_bytes(b"\xff\xd8fake")
            mime, b64 = load_image_b64(str(p))
            self.assertEqual(mime, "image/jpeg")
            import base64
            self.assertEqual(base64.b64decode(b64), b"\xff\xd8fake")
