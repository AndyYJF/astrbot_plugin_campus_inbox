"""Jev 预过滤单测：判定分流、直通规则、fail-open、filtered 批次归档。

全部用假 transport，不触网、不调真实模型。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from campus.ai import AIError, ExternalAI
from campus.config import load_settings
from campus.extract import run_extraction_cycle
from campus.jev import JevFilter
from campus.models import NormalizedMessage, SourceKey, build_message_key
from campus.storage import Storage

SRC = SourceKey("campus-main", "1000001", "9000001")


def make_storage(tmp: str) -> Storage:
    s = Storage(Path(tmp) / "test.db")
    s.ensure_subscription(SRC, "测试群")
    return s


def add_message(
    storage: Storage, text: str, remote_id: str, parse_state: str = "text"
) -> str:
    sent_at = datetime.now(timezone.utc).isoformat()
    key, _weak = build_message_key(SRC, remote_id, "同学甲", sent_at, text)
    storage.insert_message(
        NormalizedMessage(
            message_key=key,
            source=SRC,
            remote_id=remote_id,
            sender_alias="同学甲",
            sent_at=sent_at,
            received_at=sent_at,
            text=text,
            parse_state=parse_state,
        )
    )
    return key


def fake_jev(scores: dict[str, float], calls: list | None = None) -> JevFilter:
    """scores 以消息文本前缀为键；未命中的文本返回 1.0。"""

    def transport(url, headers, body, timeout):
        if calls is not None:
            calls.append(body)
        state = body["state"]
        for prefix, score in scores.items():
            if state.startswith(prefix):
                noul = score
                break
        else:
            noul = 1.0
        return {"answers": {"is_notice": {"noul": noul}}, "usage": {}}

    return JevFilter("fake-key", threshold=0.3, transport=transport)


FAKE_AI = ExternalAI(
    "http://fake/v1",
    "key",
    "gemini3.8flash",
    transport=lambda url, headers, body, timeout: {
        "choices": [{"message": {"content": '{"items": []}'}}]
    },
)


class TestJevSplit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = make_storage(self.tmp.name)

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def _rows(self, *texts: str):
        for i, t in enumerate(texts):
            add_message(self.storage, t, f"m{i}")
        return self.storage.list_unbatched_messages(80)

    def test_low_score_filtered_high_score_passed(self):
        rows = self._rows("明天下午三点开班会", "哈哈哈笑死")
        jev = fake_jev({"哈哈哈": 0.05, "明天": 0.9})
        passed, filtered = jev.split(rows)
        self.assertEqual([m["text"] for m in passed], ["明天下午三点开班会"])
        self.assertEqual([m["text"] for m in filtered], ["哈哈哈笑死"])

    def test_media_message_bypasses_without_call(self):
        add_message(self.storage, "[图片]", "img1", parse_state="mixed")
        rows = self.storage.list_unbatched_messages(80)
        calls: list = []
        passed, filtered = fake_jev({}, calls).split(rows)
        self.assertEqual(len(passed), 1)
        self.assertEqual(filtered, [])
        self.assertEqual(calls, [])  # 含媒体消息不消耗 Jev 调用

    def test_empty_text_bypasses(self):
        add_message(self.storage, "", "e1")
        rows = self.storage.list_unbatched_messages(80)
        calls: list = []
        passed, _ = fake_jev({}, calls).split(rows)
        self.assertEqual(len(passed), 1)
        self.assertEqual(calls, [])

    def test_transport_error_passes_failopen(self):
        def boom(url, headers, body, timeout):
            raise AIError("网络错误: 炸了")

        jev = JevFilter("fake-key", threshold=0.3, transport=boom)
        passed, filtered = jev.split(self._rows("任何消息"))
        self.assertEqual(len(passed), 1)
        self.assertEqual(filtered, [])

    def test_threshold_boundary_passes(self):
        jev = fake_jev({"踩线": 0.3})
        passed, filtered = jev.split(self._rows("踩线消息"))
        self.assertEqual(len(passed), 1)
        self.assertEqual(filtered, [])

    def test_order_preserved(self):
        rows = self._rows("闲聊一", "通知一", "闲聊二", "通知二")
        jev = fake_jev({"闲聊": 0.0, "通知": 0.9})
        passed, filtered = jev.split(rows)
        self.assertEqual([m["text"] for m in passed], ["通知一", "通知二"])
        self.assertEqual([m["text"] for m in filtered], ["闲聊一", "闲聊二"])


class TestFilteredBatchArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = make_storage(self.tmp.name)

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def test_filtered_messages_archived_not_reclaimed(self):
        add_message(self.storage, "明天交实验报告", "n1")
        add_message(self.storage, "笑死我了哈哈哈", "c1")
        jev = fake_jev({"笑死": 0.02, "明天": 0.95})

        result = run_extraction_cycle(self.storage, FAKE_AI, 80, 200, jev=jev)
        self.assertEqual(result, "succeeded")

        # 闲聊进了 filtered 批次，不再被领入；通知走了正常提取批次
        self.assertEqual(self.storage.list_unbatched_messages(80), [])
        row = self.storage._conn.execute(
            "SELECT state, message_keys, model_ref FROM extraction_batches "
            "WHERE state='filtered'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["model_ref"], "jev-prefilter")
        keys = json.loads(row["message_keys"])
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].endswith(":c1"))
        # filtered 批次不占当日模型调用额度
        self.assertEqual(self.storage.count_batches_today(), 1)

    def test_all_filtered_returns_filtered_without_llm(self):
        add_message(self.storage, "水群消息", "w1")
        ai_calls: list = []
        ai = ExternalAI(
            "http://fake/v1", "key", "m",
            transport=lambda u, h, b, t: ai_calls.append(b) or {
                "choices": [{"message": {"content": '{"items": []}'}}]
            },
        )
        jev = fake_jev({"水群": 0.01})
        result = run_extraction_cycle(self.storage, ai, 80, 200, jev=jev)
        self.assertEqual(result, "filtered")
        self.assertEqual(ai_calls, [])  # 全被拦截时不调 LLM

    def test_cycle_without_jev_unchanged(self):
        add_message(self.storage, "普通消息", "p1")
        result = run_extraction_cycle(self.storage, FAKE_AI, 80, 200)
        self.assertEqual(result, "succeeded")


class TestJevConfig(unittest.TestCase):
    def test_enabled_without_key_disables_channel(self):
        s = load_settings({"enabled": True, "jev_enabled": True})
        self.assertIn("jev", s.disabled_reasons)
        self.assertTrue(s.jev_enabled)

    def test_defaults(self):
        s = load_settings({})
        self.assertFalse(s.jev_enabled)
        self.assertEqual(s.jev_threshold, 0.3)
        self.assertEqual(s.jev_base_url, "https://api.typesafe.ai")
        self.assertEqual(s.jev_model, "jev-latest")
        self.assertNotIn("jev", s.disabled_reasons)

    def test_threshold_clamped(self):
        s = load_settings({"jev_threshold": "5"})
        self.assertEqual(s.jev_threshold, 1.0)
        s = load_settings({"jev_threshold": "abc"})
        self.assertEqual(s.jev_threshold, 0.3)


if __name__ == "__main__":
    unittest.main()
