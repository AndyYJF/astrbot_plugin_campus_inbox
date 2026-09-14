"""T05 日报测试：文本构造、占位幂等、失败释放。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from campus.digest import build_digest_text, local_now
from campus.storage import Storage

NOW = datetime(2026, 9, 8, 21, 30)


def _item(**kw):
    base = dict(
        item_id="i1", revision=1, category="notice", title="通知A", summary="",
        audience="", relevance="relevant", action_text="", due_at="",
        due_date="", event_at="", time_text="", uncertain_fields="[]",
        status="active",
    )
    base.update(kw)
    return base


class TestDigestText(unittest.TestCase):
    def test_empty_items_returns_empty(self):
        self.assertEqual(build_digest_text([], NOW), "")

    def test_groups_by_category_with_due(self):
        text = build_digest_text(
            [
                _item(item_id="a", category="assignment", title="交报告",
                      due_date="2026-09-11", action_text="提交到邮箱"),
                _item(item_id="b", category="notice", title="军训调整"),
                _item(item_id="c", category="notice", title="待确认事项",
                      status="needs_review"),
            ],
            NOW,
        )
        self.assertIn("09月08日", text)
        self.assertLess(text.index("【作业】"), text.index("【通知】"))  # 作业排通知前
        self.assertIn("1. 交报告（截止 09-11）", text)
        self.assertIn("👉 提交到邮箱", text)
        self.assertIn("⚠️待确认", text)

    def test_irrelevant_filtered_upstream(self):
        # relevance=irrelevant 由 SQL 过滤，文本函数只负责渲染
        text = build_digest_text([_item(title="X")], NOW)
        self.assertIn("1. X", text)


class TestDigestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def test_claim_once_per_day_kind(self):
        self.assertTrue(self.storage.claim_digest("o1", "2026-09-08", "scheduled", "x"))
        self.assertFalse(self.storage.claim_digest("o1", "2026-09-08", "scheduled", "x"))
        # 换一天/换一种类可再占
        self.assertTrue(self.storage.claim_digest("o1", "2026-09-09", "scheduled", "x"))
        self.assertTrue(self.storage.claim_digest("o1", "2026-09-08", "manual", "x"))

    def test_release_allows_retry(self):
        self.storage.claim_digest("o1", "2026-09-08", "scheduled", "x")
        self.storage.release_digest("o1", "2026-09-08", "scheduled")
        self.assertTrue(self.storage.claim_digest("o1", "2026-09-08", "scheduled", "x"))

    def test_release_does_not_delete_sent(self):
        self.storage.claim_digest("o1", "2026-09-08", "scheduled", "x")
        self.storage.mark_digest_sent("o1", "2026-09-08", "scheduled")
        self.storage.release_digest("o1", "2026-09-08", "scheduled")
        self.assertFalse(self.storage.claim_digest("o1", "2026-09-08", "scheduled", "x"))

    def test_list_digest_items_filters(self):
        from campus.extract import validate_items
        from campus.models import NormalizedMessage, SourceKey, build_message_key
        from datetime import timezone

        src = SourceKey("p", "s", "g")
        self.storage.ensure_subscription(src, "群")
        sent_at = datetime.now(timezone.utc).isoformat()
        key, _ = build_message_key(src, "r1", "甲", sent_at, "t")
        self.storage.insert_message(
            NormalizedMessage(message_key=key, source=src, remote_id="r1",
                              sender_alias="甲", sent_at=sent_at,
                              received_at=sent_at, text="t")
        )
        items = validate_items(
            {"items": [
                {"title": "相关", "relevance": "relevant", "source_refs": ["M1"]},
                {"title": "无关", "relevance": "irrelevant", "source_refs": ["M1"]},
            ]},
            [key],
        )
        self.storage.create_batch("b1", [key], "m", "v1")
        self.storage.insert_items(items, "b1")
        rows = self.storage.list_digest_items()
        titles = [r["title"] for r in rows]
        self.assertIn("相关", titles)
        self.assertNotIn("无关", titles)


class TestLocalNow(unittest.TestCase):
    def test_valid_tz(self):
        self.assertEqual(local_now("Asia/Shanghai").utcoffset().total_seconds(), 28800)

    def test_bad_tz_falls_back(self):
        self.assertEqual(local_now("Mars/Olympus").utcoffset().total_seconds(), 28800)


if __name__ == "__main__":
    unittest.main()
