"""Jev 预过滤：TypeSafe System One 决策模型，把纯闲聊拦在 LLM 提取之外。

设计依据：生产数据评测（837 条真实群消息）——阈值 0.3 时通知召回率 100%，
LLM 提取调用量降约 37%。
- 只裁决无媒体的纯文本消息；含图/文件/未解析媒体的消息直通（Jev 看不了图）。
- 任何调用失败一律放行（fail-open）：过滤是省钱的优化，绝不能丢通知。
- 被拦消息归档为 filtered 批次留痕，原始消息本就已落盘，可追溯。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from .ai import AIError, _default_transport

logger = logging.getLogger("campus_inbox.jev")

# 预过滤问法版本，写入 filtered 批次的 prompt_version 供追溯
_INSTRUCTIONS = (
    "这条大学校园群消息是否包含与学业或校园事务相关的信息？"
    "算1的包括：正式通知、作业布置、截止日期、活动安排、官方公告，"
    "以及对这类事项的补充说明、要求提醒、更正（即使语气随意像聊天）。"
    "完全无关的纯闲聊、表情包、寒暄、灌水、晒图才算0。"
)


class JevFilter:
    """对一批消息做 noul 判定：score >= threshold 送 LLM 提取，否则拦截。"""

    QUESTION_VERSION = "jev-prefilter-v1"

    def __init__(
        self,
        api_key: str,
        threshold: float = 0.3,
        base_url: str = "https://api.typesafe.ai",
        model: str = "jev-latest",
        timeout: float = 10.0,
        max_workers: int = 4,
        transport=None,
    ):
        self.api_key = api_key
        self.threshold = min(max(threshold, 0.0), 1.0)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_workers = max(1, max_workers)
        self._transport = transport or _default_transport

    def _score(self, text: str) -> float:
        """单条消息的 noul 分数；任何失败返回 1.0（放行，fail-open）。"""
        payload = {
            "model": self.model,
            "state": text[:3000],
            "questions": {
                "is_notice": {"type": "noul", "instructions": _INSTRUCTIONS}
            },
        }
        try:
            data = self._transport(
                f"{self.base_url}/v1/systemone",
                {"Authorization": f"Bearer {self.api_key}"},
                payload,
                self.timeout,
            )
            return float(data["answers"]["is_notice"]["noul"])
        except Exception as e:
            logger.warning("[campus_inbox] Jev 判定失败，放行该消息: %s", e)
            return 1.0

    def split(self, messages: list) -> tuple[list, list]:
        """把 storage 消息行分为（送提取, 被拦截），保持原始时间顺序。

        直通规则：parse_state != 'text'（含媒体）或空文本的消息不裁决、直接送提取，
        与现状行为一致；只有纯文本消息消耗 Jev 调用。
        """
        judged = [
            m for m in messages
            if m["parse_state"] == "text" and (m["text"] or "").strip()
        ]
        scores: dict[str, float] = {}
        if judged:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                results = list(pool.map(lambda m: self._score(m["text"].strip()), judged))
            scores = {m["message_key"]: s for m, s in zip(judged, results)}

        passed: list = []
        filtered: list = []
        for m in messages:
            s = scores.get(m["message_key"])
            if s is None or s >= self.threshold:
                passed.append(m)
            else:
                filtered.append(m)
        return passed, filtered
