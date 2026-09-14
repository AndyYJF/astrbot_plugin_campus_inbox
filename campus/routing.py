"""采集/指令/忽略路由决策与身份校验。

设计依据：docs/design.md 第 3 节（双账号接入、主号静默）。
本模块不依赖 AstrBot，事件以 duck-typing 传入，便于单测。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol

from .config import Settings


class Decision(enum.Enum):
    COLLECT = "collect"  # 主号白名单群消息，入库采集
    COMMAND = "command"  # owner 私聊小号的管理命令
    IGNORE = "ignore"  # 其余一律静默忽略


class Identity(enum.Enum):
    COLLECTOR = "collector"  # 事件来自采集连接（主号）
    SENDER = "sender"  # 事件来自发送连接（小号）
    UNKNOWN = "unknown"  # 无法识别的连接


class EventView(Protocol):
    """路由判定所需的最小事件视图。"""

    platform_id: str  # 平台实例 ID
    self_id: str  # 收到事件的账号
    is_group: bool  # 群消息为 True，私聊为 False
    group_id: str  # 群号，私聊为空串
    sender_id: str  # 发送者 QQ 号
    text: str  # 纯文本内容


COMMAND_PREFIX = "/校园"


@dataclass(frozen=True)
class RouteResult:
    decision: Decision
    identity: Identity
    reason: str = ""


def route(event: EventView, settings: Settings) -> RouteResult:
    """判定事件去向。任何不匹配的通道都返回 IGNORE，由调用方保证静默。"""
    if not settings.enabled:
        return RouteResult(Decision.IGNORE, Identity.UNKNOWN, "插件未启用")

    reasons = settings.disabled_reasons

    # 采集连接（主号）
    if (
        "collector" not in reasons
        and event.platform_id == settings.collector_platform_id
    ):
        if event.self_id != settings.collector_self_id:
            return RouteResult(
                Decision.IGNORE,
                Identity.COLLECTOR,
                "采集连接 self_id 与配置不符，通道暂停",
            )
        if event.is_group and _is_subscribed(event.group_id, settings):
            return RouteResult(Decision.COLLECT, Identity.COLLECTOR)
        return RouteResult(Decision.IGNORE, Identity.COLLECTOR, "主号非白名单消息，静默")

    # 发送连接（小号）。只拦截 owner 的 /校园 命令，其余消息放行给其他插件。
    if "sender" not in reasons and event.platform_id == settings.sender_platform_id:
        if event.self_id != settings.sender_self_id:
            return RouteResult(
                Decision.IGNORE,
                Identity.SENDER,
                "发送连接 self_id 与配置不符，通道暂停",
            )
        if (
            not event.is_group
            and "command" not in reasons
            and event.sender_id == settings.owner_qq
            and _is_command_text(event.text)
        ):
            return RouteResult(Decision.COMMAND, Identity.SENDER)
        return RouteResult(Decision.IGNORE, Identity.SENDER, "小号非命令消息，放行")

    return RouteResult(Decision.IGNORE, Identity.UNKNOWN, "非本插件管理的连接")


def _is_command_text(text: str) -> bool:
    """QQ 会剥掉开头的 /，AstrBot 的唤醒词配置也会剥前缀，两种形态都接受。"""
    t = text.strip().lstrip("/")
    return t.startswith(COMMAND_PREFIX.lstrip("/"))


def _is_subscribed(group_id: str, settings: Settings) -> bool:
    return any(
        sub.enabled and sub.group_id == group_id for sub in settings.subscriptions
    )
