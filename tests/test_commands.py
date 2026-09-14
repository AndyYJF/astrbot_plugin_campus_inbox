"""T06 命令解析 + 订阅切换测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from campus.commands import parse
from campus.storage import Storage


class TestParse(unittest.TestCase):
    def test_plain_status(self):
        cmd = parse("/校园 状态")
        self.assertEqual(cmd.name, "status")

    def test_slash_stripped_by_qq(self):
        cmd = parse("校园 日报")
        self.assertEqual(cmd.name, "digest")

    def test_bare_prefix_is_status(self):
        self.assertEqual(parse("/校园").name, "status")

    def test_subscribe_with_args(self):
        cmd = parse("/校园 订阅 123456 年级群 二班")
        self.assertEqual(cmd.name, "subscribe")
        self.assertEqual(cmd.args, ("123456", "年级群", "二班"))

    def test_done_number(self):
        cmd = parse("/校园 完成 3")
        self.assertEqual((cmd.name, cmd.args), ("done", ("3",)))

    def test_unknown_returns_help(self):
        self.assertEqual(parse("/校园 干啥").name, "help")

    def test_unrelated_returns_none(self):
        self.assertIsNone(parse("今天天气怎么样"))
        self.assertIsNone(parse("/帮助"))


class TestSubscriptionToggle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def test_insert_and_disable(self):
        s = self.storage
        s.set_subscription_enabled("campus-main", "1000001", "111", True, "新群")
        rows = s.list_subscriptions("campus-main", "1000001")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["alias"], "新群")
        self.assertEqual(rows[0]["enabled"], 1)
        # 退订保留别名
        s.set_subscription_enabled("campus-main", "1000001", "111", False)
        rows = s.list_subscriptions("campus-main", "1000001")
        self.assertEqual(rows[0]["enabled"], 0)
        self.assertEqual(rows[0]["alias"], "新群")
        # 重订阅
        s.set_subscription_enabled("campus-main", "1000001", "111", True)
        self.assertEqual(s.list_subscriptions("campus-main", "1000001")[0]["enabled"], 1)

    def test_isolation_by_account(self):
        s = self.storage
        s.set_subscription_enabled("campus-main", "1000001", "111", True)
        self.assertEqual(s.list_subscriptions("default", "1000002"), [])


if __name__ == "__main__":
    unittest.main()
