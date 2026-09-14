"""T03：批次抽取。领批 → 调外部 AI → 校验 → 事务落库。

设计依据：docs/design.md 第 5 节、docs/implementation-plan.md T03。
模型只能引用 M1..Mn 编号；校验把编号映射回 message_key，
跨批次/臆造引用的事项整条丢弃。模型失败不丢原文（批次回 pending）。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .ai import ExternalAI, load_image_b64, parse_json_object
from .storage import Storage

PROMPT_VERSION = "extract-v2"
_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "extract.txt"

MAX_IMAGES_PER_MSG = 2
MAX_IMAGES_PER_BATCH = 10

_CATEGORIES = {
    "notice", "assignment", "exam", "registration",
    "activity", "resource", "discussion", "uncertain",
}
_RELEVANCE = {"relevant", "irrelevant", "unknown"}
_ITEM_STRING_FIELDS = (
    "title", "summary", "audience", "action_text",
    "due_at", "due_date", "event_at", "time_text",
)


def load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


def render_user_prompt(messages: list, active_items: list | None = None) -> str:
    """messages: storage 行（sender_alias/sent_at/text）。返回编号化输入。

    active_items: 进行中事项简报（item_id/title/category/due_date），编号为 E1..En。
    """
    lines = ["以下是需要分析的群消息：", ""]
    for i, m in enumerate(messages, 1):
        text = (m["text"] or "").strip() or "[无文本内容]"
        lines.append(f"M{i} [{m['sent_at']}] {m['sender_alias']}: {text}")
    if active_items:
        lines += ["", "以下是已存在的进行中事项：", ""]
        for i, it in enumerate(active_items, 1):
            due = it["due_date"] or ""
            lines.append(f"E{i} [{it['category']}] {it['title']}（截止 {due or '未知'}）")
    return "\n".join(lines)


def validate_items(
    obj: dict, message_keys: list[str], active_map: dict[str, str] | None = None
) -> list[dict]:
    """校验模型输出。返回可落库的事项列表，每项含 sources(message_key)。

    非法项丢弃而不是整批失败：设计 5.1 要求 source_refs 必须属于输入。
    模型可通过 merge_ref 引用已有事项（E1..En → active_map → item_id）；
    解析失败按新事项处理，不丢数据。
    """
    active_map = active_map or {}
    raw_items = obj.get("items", [])
    if not isinstance(raw_items, list):
        return []
    ref_map = {f"M{i}": key for i, key in enumerate(message_keys, 1)}
    out: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        refs = raw.get("source_refs")
        if not isinstance(refs, list) or not refs:
            continue
        sources = [ref_map[r] for r in refs if isinstance(r, str) and r in ref_map]
        if len(sources) != len(refs):
            continue  # 含臆造/跨批次引用，整条丢弃
        merge_ref = raw.get("merge_ref")
        if isinstance(merge_ref, str) and merge_ref in active_map:
            out.append({"merge_item_id": active_map[merge_ref], "sources": sources})
            continue
        item = {f: str(raw.get(f) or "")[:2000] for f in _ITEM_STRING_FIELDS}
        if not item["title"]:
            continue
        category = str(raw.get("category") or "uncertain")
        item["category"] = category if category in _CATEGORIES else "uncertain"
        relevance = str(raw.get("relevance") or "unknown")
        item["relevance"] = relevance if relevance in _RELEVANCE else "unknown"
        uf = raw.get("uncertain_fields")
        item["uncertain_fields"] = (
            [str(x) for x in uf if isinstance(x, (str, int))] if isinstance(uf, list) else []
        )
        item["item_id"] = uuid.uuid4().hex
        out.append({"item": item, "sources": sources})
    return out


def build_user_content(messages: list, active_items: list | None = None,
                       downloader=load_image_b64):
    """构造 user content：无图批次返回纯文本 str；有图返回多模态段列表。

    图片下载失败降级为文本说明，不阻断批次。downloader 可注入，测试不触网。
    """
    prompt = render_user_prompt(messages, active_items)
    parts: list[dict] = [{"type": "text", "text": prompt}]
    budget = MAX_IMAGES_PER_BATCH
    for i, m in enumerate(messages, 1):
        try:
            urls = json.loads(m["media_json"] or "[]")
        except (TypeError, ValueError, KeyError):
            urls = []
        for url in urls[:MAX_IMAGES_PER_MSG]:
            if budget <= 0:
                break
            try:
                mime, b64 = downloader(url)
            except Exception:
                parts.append({"type": "text", "text": f"消息 M{i} 的配图下载失败，按无图处理。"})
                continue
            budget -= 1
            parts.append({"type": "text", "text": f"消息 M{i} 的配图："})
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return parts if len(parts) > 1 else prompt


def run_extraction_cycle(
    storage: Storage,
    ai: ExternalAI,
    batch_new_message_limit: int,
    daily_call_limit: int,
    downloader=load_image_b64,
) -> str:
    """单轮抽取。返回 skipped-limit / skipped-empty / succeeded / failed。

    失败不丢原文：批次回 pending（含退避），消息随下轮重新领入。
    """
    storage.recover_expired_leases()
    storage.rearm_failed_batches()  # failed 冷却后自愈，API 恢复不需人工介入
    if daily_call_limit > 0 and storage.count_batches_today() >= daily_call_limit:
        return "skipped-limit"

    # 先重试到期的积压批次，再领新批次
    claimable = storage.claimable_batches()
    if claimable:
        row = claimable[0]
        storage.reclaim_batch(row["batch_id"])
        keys = json.loads(row["message_keys"])
        messages = [storage.get_message_row(k) for k in keys]
        messages = [m for m in messages if m is not None]
        batch_id = row["batch_id"]
        message_keys = keys
    else:
        messages = storage.list_unbatched_messages(batch_new_message_limit)
        if not messages:
            return "skipped-empty"
        message_keys = [m["message_key"] for m in messages]
        batch_id = uuid.uuid4().hex
        storage.create_batch(batch_id, message_keys, ai.model, PROMPT_VERSION)

    active_items = storage.list_active_items_brief()
    active_map = {f"E{i}": it["item_id"] for i, it in enumerate(active_items, 1)}

    try:
        content = ai.chat(
            load_system_prompt(), build_user_content(messages, active_items, downloader)
        )
        obj = parse_json_object(content)
    except Exception:
        storage.fail_batch(batch_id)
        return "failed"

    items = validate_items(obj, message_keys, active_map)
    new_items = [e for e in items if "merge_item_id" not in e]
    merges = [e for e in items if "merge_item_id" in e]
    if items:
        try:
            if new_items:
                storage.insert_items(new_items, batch_id)
            for e in merges:
                storage.merge_into_item(e["merge_item_id"], e["sources"], batch_id)
        except Exception:
            storage.fail_batch(batch_id)
            return "failed"
    storage.finish_batch(batch_id)
    return "succeeded"
