"""T02 单测：原文存储与幂等采集。不依赖 AstrBot。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campus.ingest import ingest, normalize
from campus.models import SourceKey
from campus.storage import Storage


@dataclass
class Seg:
    """测试用消息段，类名决定类型。用 type 改名模拟 AstrBot 组件。"""

    text: str = ""
    id: str = ""
    qq: str = ""
    name: str = ""


def _seg(kind: str, **kw):
    return type(kind, (Seg,), {})(**kw)


@dataclass
class Raw:
    source: SourceKey
    remote_id: str
    sender_alias: str
    sent_at: str
    chain: list = field(default_factory=list)


MAIN = SourceKey("campus-main", "100000001", "555666")
OTHER_ACCT = SourceKey("campus-main", "999999999", "555666")
OTHER_GROUP = SourceKey("campus-main", "100000001", "777888")


def make_raw(remote_id="m1", text="hello", source=MAIN, sender="张三", sent_at="2026-09-08T01:00:00+00:00", chain=None):
    return Raw(source, remote_id, sender, sent_at, chain if chain is not None else [_seg("Plain", text=text)])


def no_reply_lookup(source, remote_id):
    return None


class TestIngestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "test.db")
        self.storage = Storage(self.db)
        self.storage.ensure_subscription(MAIN, "测试群")
        self.storage.ensure_subscription(OTHER_ACCT, "测试群")
        self.storage.ensure_subscription(OTHER_GROUP, "别群")

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def test_insert_then_duplicate(self):
        msg = normalize(make_raw(), no_reply_lookup)
        self.assertEqual(ingest(self.storage, msg), "inserted")
        self.assertEqual(ingest(self.storage, msg), "duplicate")

    def test_same_remote_id_different_source_not_confused(self):
        m1 = normalize(make_raw(remote_id="m1", source=MAIN), no_reply_lookup)
        m2 = normalize(make_raw(remote_id="m1", source=OTHER_ACCT), no_reply_lookup)
        m3 = normalize(make_raw(remote_id="m1", source=OTHER_GROUP), no_reply_lookup)
        self.assertEqual(ingest(self.storage, m1), "inserted")
        self.assertEqual(ingest(self.storage, m2), "inserted")
        self.assertEqual(ingest(self.storage, m3), "inserted")
        rows, total = self.storage.list_messages(MAIN.as_str())
        self.assertEqual(total, 1)

    def test_missing_remote_id_weak_identity(self):
        msg = normalize(make_raw(remote_id=""), no_reply_lookup)
        self.assertTrue(msg.weak_identity)
        self.assertIn(":weak:", msg.message_key)
        self.assertEqual(ingest(self.storage, msg), "inserted")
        self.assertEqual(ingest(self.storage, msg), "duplicate")

    def test_reply_resolved_within_source(self):
        first = normalize(make_raw(remote_id="m1", text="原始通知"), no_reply_lookup)
        ingest(self.storage, first)

        def resolver(source, remote_id):
            key = f"{source.as_str()}:{remote_id}"
            return key if self.storage.get_message_text(key) is not None else None

        reply = normalize(
            make_raw(remote_id="m2", text="更正一下", chain=[_seg("Reply", id="m1"), _seg("Plain", text="更正一下")]),
            resolver,
        )
        self.assertEqual(reply.reply_key, first.message_key)
        self.assertFalse(reply.reply_unavailable)
        self.assertEqual(ingest(self.storage, reply), "inserted")

    def test_reply_missing_marked_unavailable(self):
        msg = normalize(
            make_raw(remote_id="m9", chain=[_seg("Reply", id="ghost"), _seg("Plain", text="引用不存在")]),
            no_reply_lookup,
        )
        self.assertTrue(msg.reply_unavailable)
        self.assertEqual(msg.reply_key, "")
        self.assertEqual(ingest(self.storage, msg), "inserted")

    def test_mixed_media_parse_state(self):
        msg = normalize(
            make_raw(chain=[_seg("Plain", text="看这个"), _seg("Image")]),
            no_reply_lookup,
        )
        self.assertEqual(msg.parse_state, "mixed")
        pure_media = normalize(make_raw(chain=[_seg("Image")]), no_reply_lookup)
        self.assertEqual(pure_media.parse_state, "media_unparsed")

    def test_rejected_on_broken_db(self):
        self.storage.close()
        msg = normalize(make_raw(remote_id="mX"), no_reply_lookup)
        self.assertEqual(ingest(self.storage, msg), "rejected")

    def test_persistence_across_reload(self):
        msg = normalize(make_raw(remote_id="m1"), no_reply_lookup)
        ingest(self.storage, msg)
        self.storage.close()
        reopened = Storage(self.db)
        try:
            rows, total = reopened.list_messages(MAIN.as_str())
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["text"], "hello")
        finally:
            reopened.close()

    def test_pagination(self):
        for i in range(25):
            ingest(
                self.storage,
                normalize(make_raw(remote_id=f"m{i}", sent_at=f"2026-09-08T01:{i:02d}:00+00:00"), no_reply_lookup),
            )
        rows1, total = self.storage.list_messages(MAIN.as_str(), page=1, per_page=20)
        rows2, _ = self.storage.list_messages(MAIN.as_str(), page=2, per_page=20)
        self.assertEqual(total, 25)
        self.assertEqual(len(rows1), 20)
        self.assertEqual(len(rows2), 5)
        self.assertEqual(rows1[0]["remote_id"], "m0")

    def test_wake_called_only_on_insert(self):
        calls = []

        def wake():
            calls.append(1)

        msg = normalize(make_raw(remote_id="m1"), no_reply_lookup)
        ingest(self.storage, msg, wake=wake)
        ingest(self.storage, msg, wake=wake)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
