"""插件配置验证与运行快照。

设计依据：docs/design.md 第 8 节。所有校验失败都不抛异常打断框架，
而是关闭对应通道并记录原因，供 /校园 状态 展示。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_TODO_MODES = ("export", "http")


@dataclass(frozen=True)
class Subscription:
    group_id: str
    alias: str
    enabled: bool = True


@dataclass(frozen=True)
class Settings:
    """配置快照。routing 只依赖本类型，不直接读框架配置对象。"""

    enabled: bool = False
    owner_qq: str = ""
    collector_platform_id: str = ""
    collector_self_id: str = ""
    sender_platform_id: str = ""
    sender_self_id: str = ""
    destination_umo: str = ""
    subscriptions: tuple[Subscription, ...] = ()
    timezone: str = "Asia/Shanghai"
    digest_time: str = "21:30"
    instant_push_enabled: bool = False
    external_ai_enabled: bool = False
    provider_id: str = ""
    external_ai_base_url: str = ""
    external_ai_api_key: str = ""
    external_ai_model: str = "gemini3.8flash"
    web_enabled: bool = False
    web_port: int = 8620
    web_token: str = ""
    batch_interval_seconds: int = 300
    batch_new_message_limit: int = 80
    context_message_limit: int = 20
    llm_concurrency: int = 1
    daily_call_limit: int = 200
    raw_retention_days: int = 30
    item_retention_days: int = 180
    todo_mode: str = "export"
    todo_base_url: str = ""
    todo_token_env: str = "CAMPUS_TODO_TOKEN"
    # 校验产生的通道关闭原因，键为通道名（collector/sender/ai/todo）
    disabled_reasons: dict[str, str] = field(default_factory=dict)


def parse_subscriptions(raw: list | None) -> tuple[Subscription, ...]:
    """解析配置里的 "群号:别名" 列表，忽略格式错误的行。"""
    subs: list[Subscription] = []
    for line in raw or []:
        if not isinstance(line, str):
            continue
        parts = line.strip().split(":", 1)
        group_id = parts[0].strip()
        if not group_id.isdigit():
            continue
        alias = parts[1].strip() if len(parts) > 1 and parts[1].strip() else group_id
        subs.append(Subscription(group_id=group_id, alias=alias))
    return tuple(subs)


def load_settings(cfg: dict) -> Settings:
    """从框架配置字典构建快照并做安全校验。

    规则（docs/design.md 第 3、8 节）：
    - 主号与小号 self_id 必须不同，否则两个通道都关闭；
    - owner / 平台实例 ID / self_id 留空则关闭对应通道；
    - digest_time 不合法回退 21:30；todo_mode 未知回退 export。
    """
    get = cfg.get if hasattr(cfg, "get") else lambda k, d=None: d

    def _s(key: str, default: str = "") -> str:
        v = get(key, default)
        return str(v).strip() if v is not None else default

    def _b(key: str, default: bool = False) -> bool:
        return bool(get(key, default))

    def _i(key: str, default: int) -> int:
        try:
            return int(get(key, default))
        except (TypeError, ValueError):
            return default

    reasons: dict[str, str] = {}

    owner = _s("owner_qq")
    col_pid = _s("collector_platform_id")
    col_sid = _s("collector_self_id")
    snd_pid = _s("sender_platform_id")
    snd_sid = _s("sender_self_id")

    if not owner:
        reasons["command"] = "未配置 owner_qq，管理命令关闭"

    if not col_pid or not col_sid:
        reasons["collector"] = "采集连接未配置完整（collector_platform_id / collector_self_id）"
    if not snd_pid or not snd_sid:
        reasons["sender"] = "发送连接未配置完整（sender_platform_id / sender_self_id）"

    if col_sid and snd_sid and col_sid == snd_sid:
        reasons["collector"] = "主号与小号 self_id 相同，采集通道关闭"
        reasons["sender"] = "主号与小号 self_id 相同，发送通道关闭"

    digest_time = _s("digest_time", "21:30") or "21:30"
    if not _TIME_RE.match(digest_time):
        digest_time = "21:30"

    todo_mode = _s("todo_mode", "export") or "export"
    if todo_mode not in _TODO_MODES:
        todo_mode = "export"

    if not _b("external_ai_enabled"):
        reasons["ai"] = "external_ai_enabled=false，AI 抽取关闭"
    elif not _s("external_ai_base_url") or not _s("external_ai_api_key"):
        reasons["ai"] = "外部 AI 未配置完整（external_ai_base_url / external_ai_api_key）"

    return Settings(
        enabled=_b("enabled"),
        owner_qq=owner,
        collector_platform_id=col_pid,
        collector_self_id=col_sid,
        sender_platform_id=snd_pid,
        sender_self_id=snd_sid,
        destination_umo=_s("destination_umo"),
        subscriptions=parse_subscriptions(get("subscriptions")),
        timezone=_s("timezone", "Asia/Shanghai") or "Asia/Shanghai",
        digest_time=digest_time,
        instant_push_enabled=_b("instant_push_enabled"),
        external_ai_enabled=_b("external_ai_enabled"),
        provider_id=_s("provider_id"),
        external_ai_base_url=_s("external_ai_base_url"),
        external_ai_api_key=_s("external_ai_api_key"),
        external_ai_model=_s("external_ai_model", "gemini3.8flash") or "gemini3.8flash",
        web_enabled=_b("web_enabled"),
        web_port=min(max(_i("web_port", 8620), 1024), 65535),
        web_token=_s("web_token"),
        batch_interval_seconds=max(_i("batch_interval_seconds", 300), 30),
        batch_new_message_limit=max(_i("batch_new_message_limit", 80), 1),
        context_message_limit=max(_i("context_message_limit", 20), 0),
        llm_concurrency=max(_i("llm_concurrency", 1), 1),
        daily_call_limit=max(_i("daily_call_limit", 200), 0),
        raw_retention_days=max(_i("raw_retention_days", 30), 1),
        item_retention_days=max(_i("item_retention_days", 180), 1),
        todo_mode=todo_mode,
        todo_base_url=_s("todo_base_url"),
        todo_token_env=_s("todo_token_env", "CAMPUS_TODO_TOKEN") or "CAMPUS_TODO_TOKEN",
        disabled_reasons=reasons,
    )
