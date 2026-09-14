"""T05：每日汇总。构造日报文本；发送与调度在 main.py。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_FALLBACK_CN = timezone(timedelta(hours=8))  # Windows 无 tzdata 时的兜底

_CAT_ORDER = ["exam", "assignment", "registration", "notice", "activity",
              "resource", "discussion", "uncertain"]
_CAT_LABEL = {
    "exam": "考试", "assignment": "作业", "registration": "报名",
    "notice": "通知", "activity": "活动", "resource": "资源",
    "discussion": "讨论", "uncertain": "其他",
}


def local_now(tz_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        # 容器有完整 tzdata；Windows 本地没有。本插件只服务国内场景，兜底 UTC+8。
        return datetime.now(_FALLBACK_CN)


def build_digest_text(items: list, now: datetime) -> str:
    """items: list_digest_items 行。空列表返回空串（调用方决定不发）。"""
    if not items:
        return ""
    lines = [f"📮 校园日报 · {now:%m月%d日}", ""]
    by_cat: dict[str, list] = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)
    idx = 0
    for cat in _CAT_ORDER:
        group = by_cat.get(cat)
        if not group:
            continue
        lines.append(f"【{_CAT_LABEL[cat]}】")
        for it in group:
            idx += 1
            due = ""
            if it["due_at"]:
                due = f"（截止 {it['due_at'][5:16].replace('T', ' ')}）"
            elif it["due_date"]:
                due = f"（截止 {it['due_date'][5:]}）"
            elif it["time_text"]:
                due = f"（{it['time_text'][:20]}）"
            mark = " ⚠️待确认" if it["status"] == "needs_review" else ""
            lines.append(f"{idx}. {it['title']}{due}{mark}")
            if it["action_text"]:
                lines.append(f"   👉 {it['action_text'][:60]}")
        lines.append("")
    lines.append("—— 面板查看完整详情与来源")
    return "\n".join(lines).strip()
