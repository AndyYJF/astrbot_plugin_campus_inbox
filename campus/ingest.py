"""消息规范化与幂等采集入口。

设计依据：docs/design.md 第 4 节。
- 只处理白名单群（路由已保证），保存文本、时间、引用和未解析媒体标记。
- 引用只在同来源内查本地库，查不到标记 reply_unavailable，不跨群猜配。
- 消息落盘后才唤醒 worker。
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol

from .models import NormalizedMessage, SourceKey, build_message_key
from .storage import Storage, StorageError


class RawSegment(Protocol):
    """duck-typing 消息段：AstrBot 组件类名决定类型。"""

    ...


class RawMessage(Protocol):
    source: SourceKey
    remote_id: str
    sender_alias: str
    sent_at: str  # UTC ISO 8601
    chain: Iterable  # AstrBot 消息链或测试用假段


_MEDIA_PLACEHOLDER = {"image": "[图片未解析]", "record": "[语音未解析]", "video": "[视频未解析]"}


def persist_media(ref: str, media_dir: Path) -> str:
    """本地临时图片复制到插件 media 目录（临时文件事件后会被清理），返回新路径。

    http 引用原样返回；复制失败返回空串（调用方丢弃该引用）。
    """
    if ref.startswith("http"):
        return ref
    src = Path(ref)
    try:
        if not src.is_file():
            return ""
        media_dir.mkdir(parents=True, exist_ok=True)
        dst = media_dir / f"{uuid.uuid4().hex}{src.suffix or '.jpg'}"
        shutil.copyfile(src, dst)
        return str(dst)
    except OSError:
        return ""


def parse_group_recall(raw) -> str | None:
    """从 OneBot 原始事件解析群撤回。返回被撤回的 message_id，非撤回事件返回 None。"""
    if not isinstance(raw, dict):
        return None
    if raw.get("post_type") != "notice" or raw.get("notice_type") != "group_recall":
        return None
    message_id = raw.get("message_id")
    return str(message_id) if message_id is not None else None


def normalize(raw: RawMessage, reply_resolver: Callable[[SourceKey, str], str | None]) -> NormalizedMessage:
    """把原始消息规范化。reply_resolver(source, remote_id) → message_key 或 None。"""
    texts: list[str] = []
    media: list[str] = []
    has_media = False
    reply_remote_id = ""
    segments_min: list[dict] = []

    for seg in raw.chain:
        stype = type(seg).__name__.lower()
        if stype == "plain":
            text = getattr(seg, "text", "")
            texts.append(text)
            segments_min.append({"t": "plain", "text": text})
        elif stype == "reply":
            reply_remote_id = str(getattr(seg, "id", "") or "")
            segments_min.append({"t": "reply", "id": reply_remote_id})
        elif stype == "at":
            segments_min.append({"t": "at", "qq": str(getattr(seg, "qq", ""))})
        elif stype == "image":
            # AstrBot 预处理会把图片下载到本地并改写 file/path/url 为本地路径；
            # 预处理失败时 url 保留原始 http 地址。两种引用都接受。
            ref = str(
                getattr(seg, "path", "") or getattr(seg, "file", "")
                or getattr(seg, "url", "") or ""
            )
            if ref:
                media.append(ref)
                texts.append("[图片]")
                segments_min.append({"t": "image", "ref": ref[:200]})
            else:
                has_media = True
                segments_min.append({"t": "image", "marker": "[图片无URL]"})
        elif stype == "file":
            name = getattr(seg, "name", "") or "[文件]"
            has_media = True
            segments_min.append({"t": "file", "name": name})
        else:
            has_media = True
            segments_min.append({"t": stype, "marker": _MEDIA_PLACEHOLDER.get(stype, f"[{stype}未解析]")})

    text = "".join(texts)
    reply_key = ""
    reply_unavailable = False
    if reply_remote_id:
        resolved = reply_resolver(raw.source, reply_remote_id)
        if resolved:
            reply_key = resolved
        else:
            reply_unavailable = True

    if has_media and text:
        parse_state = "mixed"
    elif has_media:
        parse_state = "media_unparsed"
    else:
        parse_state = "text"

    message_key, weak = build_message_key(
        raw.source, raw.remote_id, raw.sender_alias, raw.sent_at, text
    )

    return NormalizedMessage(
        message_key=message_key,
        source=raw.source,
        remote_id=raw.remote_id,
        sender_alias=raw.sender_alias,
        sent_at=raw.sent_at,
        received_at=datetime.now(timezone.utc).isoformat(),
        text=text,
        segments=json.dumps(segments_min, ensure_ascii=False),
        media=tuple(media),
        reply_key=reply_key,
        reply_unavailable=reply_unavailable,
        parse_state=parse_state,
        weak_identity=weak,
    )


def ingest(storage: Storage, msg: NormalizedMessage, wake: Callable[[], None] | None = None) -> str:
    """入库并在成功后唤醒后台 worker。返回 inserted / duplicate / rejected。"""
    try:
        result = storage.insert_message(msg)
    except StorageError:
        return "rejected"
    if result == "inserted" and wake:
        wake()
    return result
