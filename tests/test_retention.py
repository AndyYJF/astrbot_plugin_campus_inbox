"""T07b 保留期清理测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from campus.extract import validate_items
from campus.models import NormalizedMessage, SourceKey, build_message_key
from campus.storage import Storage

SRC = SourceKey("campus-main", "1000001", "9000001")
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")
        self.storage.ensure_subscription(SRC, "群")

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def _msg(self, rid: str, age_days: int, revoked: bool = False) -> str:
        received = (NOW - timedelta(days=age_days)).isoformat()
        key, _ = build_message_key(SRC, rid, "甲", received, "x")
        self.storage.insert_message(
            NormalizedMessage(message_key=key, source=SRC, remote_id=rid,
                              sender_alias="甲", sent_at=received,
                              received_at=received, text="x")
        )
        if revoked:
            self.storage.mark_message_revoked(key)
        return key

    def _count(self) -> int:
        return self.storage._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def test_recent_kept_old_deleted(self):
        self._msg("fresh", 5)
        self._msg("old", 40)
        deleted = self.storage.cleanup_messages(NOW.isoformat(), raw_days=30)
        self.assertEqual(deleted, 1)
        self.assertEqual(self._count(), 1)

    def test_revoked_shorter_window(self):
        self._msg("r1", 10, revoked=True)   # 撤回 10 天 > 7 → 删
        self._msg("r2", 3, revoked=True)    # 撤回 3 天 → 留
        deleted = self.storage.cleanup_messages(NOW.isoformat())
        self.assertEqual(deleted, 1)
        self.assertEqual(self._count(), 1)

    def test_evidence_message_kept(self):
        key = self._msg("evidence", 100)  # 超龄但被事项引用
        items = validate_items({"items": [{"title": "T", "source_refs": ["M1"]}]}, [key])
        self.storage.create_batch("b0", [key], "m", "v1")
        self.storage.insert_items(items, "b0")
        deleted = self.storage.cleanup_messages(NOW.isoformat())
        self.assertEqual(deleted, 0)
        self.assertEqual(self._count(), 1)

    def test_empty_db_no_error(self):
        self.assertEqual(self.storage.cleanup_messages(NOW.isoformat()), 0)


if __name__ == "__main__":
    unittest.main()
