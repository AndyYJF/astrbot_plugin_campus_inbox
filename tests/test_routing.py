"""路由判定单测。不依赖 AstrBot，直接构造 EventView 协议对象。

覆盖 implementation-plan T01 要求的五种场景及 self_id 校验。
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campus.config import Settings, Subscription, load_settings
from campus.routing import Decision, Identity, route


@dataclass
class FakeEvent:
    platform_id: str
    self_id: str
    is_group: bool
    group_id: str = ""
    sender_id: str = ""
    text: str = ""


def make_settings(**overrides) -> Settings:
    base = dict(
        enabled=True,
        owner_qq="100000001",
        collector_platform_id="campus-main",
        collector_self_id="100000001",
        sender_platform_id="campus-bot",
        sender_self_id="100000002",
        subscriptions=(Subscription(group_id="555666", alias="测试群"),),
    )
    base.update(overrides)
    return Settings(**base)


class TestRoute(unittest.TestCase):
    def test_collector_whitelist_group_collects(self):
        ev = FakeEvent("campus-main", "100000001", True, group_id="555666", sender_id="999")
        r = route(ev, make_settings())
        self.assertIs(r.decision, Decision.COLLECT)
        self.assertIs(r.identity, Identity.COLLECTOR)

    def test_collector_other_group_ignored(self):
        ev = FakeEvent("campus-main", "100000001", True, group_id="12345", sender_id="999")
        r = route(ev, make_settings())
        self.assertIs(r.decision, Decision.IGNORE)

    def test_collector_private_ignored(self):
        ev = FakeEvent("campus-main", "100000001", False, sender_id="999")
        r = route(ev, make_settings())
        self.assertIs(r.decision, Decision.IGNORE)
        self.assertIs(r.identity, Identity.COLLECTOR)

    def test_owner_private_to_sender_is_command(self):
        ev = FakeEvent("campus-bot", "100000002", False, sender_id="100000001", text="/校园 状态")
        r = route(ev, make_settings())
        self.assertIs(r.decision, Decision.COMMAND)
        self.assertIs(r.identity, Identity.SENDER)

    def test_owner_private_slash_stripped_is_command(self):
        # QQ/AstrBot 会把开头的 / 剥掉，命令必须仍然命中。
        ev = FakeEvent("campus-bot", "100000002", False, sender_id="100000001", text="校园 状态")
        r = route(ev, make_settings())
        self.assertIs(r.decision, Decision.COMMAND)

    def test_owner_private_without_prefix_is_ignored(self):
        ev = FakeEvent("campus-bot", "100000002", False, sender_id="100000001", text="你好")
        r = route(ev, make_settings())
        self.assertIs(r.decision, Decision.IGNORE)

    def test_stranger_private_to_sender_ignored(self):
        ev = FakeEvent("campus-bot", "100000002", False, sender_id="777777")
        r = route(ev, make_settings())
        self.assertIs(r.decision, Decision.IGNORE)

    def test_sender_group_ignored_even_for_owner(self):
        ev = FakeEvent("campus-bot", "100000002", True, group_id="555666", sender_id="100000001")
        r = route(ev, make_settings())
        self.assertIs(r.decision, Decision.IGNORE)

    def test_collector_self_id_mismatch_silences_channel(self):
        ev = FakeEvent("campus-main", "999999999", True, group_id="555666", sender_id="999")
        r = route(ev, make_settings())
        self.assertIs(r.decision, Decision.IGNORE)
        self.assertIn("self_id", r.reason)

    def test_sender_self_id_mismatch_blocks_command(self):
        ev = FakeEvent("campus-bot", "999999999", False, sender_id="100000001", text="/校园 状态")
        r = route(ev, make_settings())
        self.assertIs(r.decision, Decision.IGNORE)

    def test_unknown_platform_ignored(self):
        ev = FakeEvent("wechat", "abc", False, sender_id="100000001")
        r = route(ev, make_settings())
        self.assertIs(r.decision, Decision.IGNORE)
        self.assertIs(r.identity, Identity.UNKNOWN)

    def test_disabled_plugin_ignores_everything(self):
        ev = FakeEvent("campus-main", "100000001", True, group_id="555666", sender_id="999")
        r = route(ev, make_settings(enabled=False))
        self.assertIs(r.decision, Decision.IGNORE)


class TestLoadSettings(unittest.TestCase):
    def test_same_self_id_disables_both_channels(self):
        s = load_settings(
            {
                "enabled": True,
                "owner_qq": "100000001",
                "collector_platform_id": "a",
                "collector_self_id": "111",
                "sender_platform_id": "b",
                "sender_self_id": "111",
            }
        )
        self.assertIn("collector", s.disabled_reasons)
        self.assertIn("sender", s.disabled_reasons)
        ev = FakeEvent("a", "111", True, group_id="555666", sender_id="9")
        self.assertIs(route(ev, s).decision, Decision.IGNORE)

    def test_empty_owner_disables_command(self):
        s = load_settings(
            {
                "enabled": True,
                "owner_qq": "",
                "collector_platform_id": "a",
                "collector_self_id": "111",
                "sender_platform_id": "b",
                "sender_self_id": "222",
            }
        )
        self.assertIn("command", s.disabled_reasons)
        ev = FakeEvent("b", "222", False, sender_id="111", text="/校园 状态")
        self.assertIs(route(ev, s).decision, Decision.IGNORE)

    def test_parse_subscriptions_skips_bad_lines(self):
        s = load_settings({"subscriptions": ["555666:计科班", "bad-line", "123:", "  ", 42]})
        self.assertEqual(len(s.subscriptions), 2)
        self.assertEqual(s.subscriptions[0].alias, "计科班")
        self.assertEqual(s.subscriptions[1].alias, "123")

    def test_defaults(self):
        s = load_settings({})
        self.assertFalse(s.enabled)
        self.assertEqual(s.digest_time, "21:30")
        self.assertEqual(s.todo_mode, "export")
        self.assertIn("ai", s.disabled_reasons)

    def test_bad_digest_time_falls_back(self):
        s = load_settings({"digest_time": "25:99"})
        self.assertEqual(s.digest_time, "21:30")


if __name__ == "__main__":
    unittest.main()
