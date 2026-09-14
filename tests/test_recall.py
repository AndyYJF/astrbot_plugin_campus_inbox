"""T07a 撤回联动测试：parse_group_recall + 撤回后事项转待确认。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from campus.extract import validate_items
from campus.ingest import parse_group_recall
from campus.models import NormalizedMessage, SourceKey, build_message_key
from campus.storage import Storage

SRC = SourceKey("campus-main", "1000001", "9000001")


class TestParseRecall(unittest.TestCase):
    def test_group_recall(self):
        raw = {"post_type": "notice", "notice_type": "group_recall",
               "group_id": 9000001, "message_id": 123456}
        self.assertEqual(parse_group_recall(raw), "123456")

    def test_other_notice_ignored(self):
        self.assertIsNone(parse_group_recall(
            {"post_type": "notice", "notice_type": "group_increase"}))
        self.assertIsNone(parse_group_recall({"post_type": "message"}))
        self.assertIsNone(parse_group_recall(None))
        self.assertIsNone(parse_group_recall("x"))


class TestRecallFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")
        self.storage.ensure_subscription(SRC, "群")

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def _msg(self, rid: str, text: str) -> str:
        sent_at = datetime.now(timezone.utc).isoformat()
        key, _ = build_message_key(SRC, rid, "甲", sent_at, text)
        self.storage.insert_message(
            NormalizedMessage(message_key=key, source=SRC, remote_id=rid,
                              sender_alias="甲", sent_at=sent_at,
                              received_at=sent_at, text=text)
        )
        return key

    def _seed_item(self, keys: list[str]) -> str:
        refs = [f"M{i}" for i in range(1, len(keys) + 1)]
        items = validate_items(
            {"items": [{"title": "事项", "source_refs": refs}]}, keys)
        self.storage.create_batch("b0", keys, "m", "v1")
        self.storage.insert_items(items, "b0")
        return items[0]["item"]["item_id"]

    def test_revoke_only_source_flags_item(self):
        key = self._msg("r1", "通知")
        item_id = self._seed_item([key])
        self.assertTrue(self.storage.mark_message_revoked(key))
        self.assertEqual(self.storage.items_sourcing(key), [item_id])
        self.assertTrue(self.storage.all_sources_revoked(item_id))
        # 模拟 main._apply_recall 的状态迁移
        self.storage.set_item_status(item_id, "needs_review", "全部来源消息被撤回")
        self.assertEqual(
            self.storage.get_item_detail(item_id)["status"], "needs_review")

    def test_revoke_unknown_key_returns_false(self):
        self.assertFalse(self.storage.mark_message_revoked("p:s:g:nope"))

    def test_item_with_remaining_source_stays(self):
        k1 = self._msg("r1", "通知")
        k2 = self._msg("r2", "补充")
        item_id = self._seed_item([k1, k2])
        self.storage.mark_message_revoked(k1)
        self.assertFalse(self.storage.all_sources_revoked(item_id))
        self.storage.mark_message_revoked(k2)
        self.assertTrue(self.storage.all_sources_revoked(item_id))

    def test_revoked_message_not_rebatched(self):
        key = self._msg("r1", "通知")
        self.storage.mark_message_revoked(key)
        self.assertEqual(self.storage.list_unbatched_messages(80), [])


if __name__ == "__main__":
    unittest.main()
