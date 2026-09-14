"""规范化消息模型。

设计依据：docs/design.md 第 4、7 节。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceKey:
    """消息来源三元组：平台实例 + 账号 + 群。"""

    platform_id: str
    self_id: str
    group_id: str

    def as_str(self) -> str:
        return f"{self.platform_id}:{self.self_id}:{self.group_id}"


@dataclass(frozen=True)
class NormalizedMessage:
    """入库前的规范化消息。

    message_key 优先由 platform_id/self_id/group_id/remote_id 组成；
    remote_id 缺失时用群+发送者+时间+内容散列作弱去重键，并置 weak_identity。
    """

    message_key: str
    source: SourceKey
    remote_id: str
    sender_alias: str
    sent_at: str  # UTC ISO 8601
    received_at: str  # UTC ISO 8601
    text: str
    segments: str = "[]"  # JSON：消息段最小结构
    media: tuple[str, ...] = ()  # 图片 URL，多模态抽取用
    reply_key: str = ""  # 引用消息的 message_key，查不到时为空
    reply_unavailable: bool = False  # 存在引用但本地查不到原文
    parse_state: str = "text"  # text / media_unparsed / mixed
    weak_identity: bool = False


def build_message_key(
    source: SourceKey,
    remote_id: str,
    sender_alias: str,
    sent_at: str,
    text: str,
) -> tuple[str, bool]:
    """返回 (message_key, weak_identity)。"""
    if remote_id:
        return f"{source.as_str()}:{remote_id}", False
    digest = hashlib.sha256(
        f"{source.as_str()}|{sender_alias}|{sent_at}|{text}".encode("utf-8")
    ).hexdigest()[:32]
    return f"{source.as_str()}:weak:{digest}", True
