"""T06：私聊命令解析。纯函数，不依赖 AstrBot。

命令形如「/校园 订阅 123456 年级群」（QQ 会吃掉斜杠，两种都接受）。
parse 只负责拆词；执行在 main.py。
"""

from __future__ import annotations

from dataclasses import dataclass

PREFIX = "校园"


@dataclass(frozen=True)
class Command:
    name: str           # status/digest/list/subscribe/unsubscribe/done/withdraw/confirm/help
    args: tuple[str, ...] = ()


_ALIASES = {
    "状态": "status",
    "日报": "digest",
    "列表": "list",
    "订阅": "subscribe",
    "退订": "unsubscribe",
    "完成": "done",
    "撤回": "withdraw",
    "确认": "confirm",
    "帮助": "help",
}

HELP_TEXT = (
    "校园收件箱命令：\n"
    "/校园 状态 — 运行概览\n"
    "/校园 日报 — 立即推送今日日报\n"
    "/校园 列表 — 进行中事项（带编号）\n"
    "/校园 完成 N — 把第 N 条标为完成\n"
    "/校园 撤回 N — 作废第 N 条\n"
    "/校园 确认 N — 待确认事项确认无误\n"
    "/校园 订阅 群号 [别名] — 开始采集某群\n"
    "/校园 退订 群号 — 停止采集某群"
)


def parse(text: str) -> Command | None:
    """非本插件命令返回 None。"""
    body = (text or "").strip().lstrip("/").strip()
    if not body.startswith(PREFIX):
        return None
    parts = body[len(PREFIX):].split()
    if not parts:
        return Command("status")
    name = _ALIASES.get(parts[0])
    if name is None:
        return Command("help")
    return Command(name, tuple(parts[1:]))
