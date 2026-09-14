"""AstrBot 生命周期与事件注册入口，业务逻辑委托 campus 包。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .campus.ai import ExternalAI
from .campus.commands import HELP_TEXT, parse
from .campus.config import Subscription, load_settings
from .campus.digest import build_digest_text, local_now
from .campus.extract import run_extraction_cycle
from .campus.ingest import ingest, normalize, parse_group_recall, persist_media
from .campus.models import SourceKey
from .campus.routing import Decision, route
from .campus.storage import Storage
from .campus.web import PanelServer


class _EventView:
    """把 AstrMessageEvent 适配成 routing 需要的最小视图。"""

    def __init__(self, event: AstrMessageEvent):
        self.platform_id = event.get_platform_id()
        self.self_id = str(event.get_self_id())
        self.is_group = event.get_group_id() is not None and event.get_group_id() != ""
        self.group_id = str(event.get_group_id() or "")
        self.sender_id = str(event.get_sender_id())
        self.text = event.get_message_str() or ""


class _RawFromEvent:
    """把 AstrMessageEvent 适配成 ingest 需要的原始消息。"""

    def __init__(self, event: AstrMessageEvent, source: SourceKey):
        obj = event.message_obj
        self.source = source
        self.remote_id = str(getattr(obj, "message_id", "") or "")
        self.sender_alias = event.get_sender_name() or str(event.get_sender_id())
        ts = getattr(obj, "timestamp", 0) or 0
        self.sent_at = (
            datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
            if ts
            else datetime.now(timezone.utc).isoformat()
        )
        self.chain = event.get_messages()


@register(
    "astrbot_plugin_campus_inbox",
    "AndyYan",
    "主号采集白名单大学群消息，AI 汇总后经小号推送",
    "0.1.0",
)
class CampusInbox(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.settings = load_settings(config)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_campus_inbox")
        self.storage = Storage(self.data_dir / "campus.db")
        self._wake_event = asyncio.Event()
        for sub in self.settings.subscriptions:
            if sub.enabled and "collector" not in self.settings.disabled_reasons:
                self.storage.ensure_subscription(
                    SourceKey(
                        self.settings.collector_platform_id,
                        self.settings.collector_self_id,
                        sub.group_id,
                    ),
                    sub.alias,
                )
        logger.info(
            "[campus_inbox] loaded, enabled=%s, disabled=%s",
            self.settings.enabled,
            self.settings.disabled_reasons,
        )
        self.ai: ExternalAI | None = None
        self._worker_task: asyncio.Task | None = None
        if self.settings.enabled and "ai" not in self.settings.disabled_reasons:
            self.ai = ExternalAI(
                self.settings.external_ai_base_url,
                self.settings.external_ai_api_key,
                self.settings.external_ai_model,
            )
        self._digest_task: asyncio.Task | None = None
        self._dest_umo = (
            self.settings.destination_umo
            or f"{self.settings.sender_platform_id}:FriendMessage:{self.settings.owner_qq}"
        )
        self._last_list: list[str] = []  # /校园 列表 的编号 → item_id 快照
        self._effective = self._build_effective()

    async def initialize(self):
        if self.ai is not None:
            self._worker_task = asyncio.create_task(self._extract_loop())
        if self.settings.enabled and "sender" not in self.settings.disabled_reasons:
            self._digest_task = asyncio.create_task(self._digest_loop())
        if self.settings.enabled and self.settings.web_enabled:
            try:
                self._panel = PanelServer(
                    self.storage, "0.0.0.0",
                    self.settings.web_port, self.settings.web_token,
                    media_dir=self.data_dir / "media",
                )
                self._panel.start()
                logger.info("[campus_inbox] Web 面板监听端口 %s", self._panel.port)
            except Exception:
                logger.exception("[campus_inbox] Web 面板启动失败")
                self._panel = None

    async def _extract_loop(self):
        """批次抽取循环：到期跑一轮，或收到新消息唤醒。模型并发=1。"""
        while True:
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self.settings.batch_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass
            self._wake_event.clear()
            try:
                result = await asyncio.to_thread(
                    run_extraction_cycle,
                    self.storage,
                    self.ai,
                    self.settings.batch_new_message_limit,
                    self.settings.daily_call_limit,
                )
                if result not in ("skipped-empty",):
                    logger.info("[campus_inbox] 抽取批次结果: %s", result)
            except Exception:
                logger.exception("[campus_inbox] 抽取循环异常")

    async def terminate(self):
        if self._worker_task is not None:
            self._worker_task.cancel()
        if self._digest_task is not None:
            self._digest_task.cancel()
        if getattr(self, "_panel", None) is not None:
            self._panel.stop()
        self.storage.close()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict) and raw.get("post_type") == "notice":
            await self._handle_notice(event, raw)
            return
        if isinstance(raw, dict):  # T10 排查：临时转储图片段原始结构
            segs = raw.get("message")
            if isinstance(segs, list) and any(
                isinstance(s, dict) and s.get("type") == "image" for s in segs
            ):
                try:
                    with open(self.data_dir / "debug.log", "a", encoding="utf-8") as f:
                        f.write("IMAGE_DUMP " + json.dumps(segs, ensure_ascii=False)[:800] + "\n")
                except Exception:
                    pass
        result = route(_EventView(event), self._effective)
        self._debug_mark(event, result)

        if result.decision is Decision.COLLECT:
            await asyncio.to_thread(self._store_sync, event)
            event.stop_event()
            return

        if result.decision is Decision.COMMAND:
            event.should_call_llm(False)
            async for r in self._handle_command(event):
                yield r
            return

        if result.identity.value == "collector":
            # 主号连接上的其他消息：静默，阻断默认聊天与其他自动响应。
            # 小号连接的普通消息不拦截，避免影响同连接上的其他插件。
            event.stop_event()

    # ---- 日报（T05） ----

    async def _digest_loop(self):
        """每 20 秒检查一次本地时间，到点发当日日报。重启安全：digests 表占位。

        顺带每天做一次保留期清理（T07b）。
        """
        last_cleanup_date = ""
        while True:
            await asyncio.sleep(20)
            try:
                now = local_now(self.settings.timezone)
                today = now.strftime("%Y-%m-%d")
                if today != last_cleanup_date:
                    deleted = await asyncio.to_thread(
                        self.storage.cleanup_messages,
                        now.astimezone(timezone.utc).isoformat(),
                        self.settings.raw_retention_days,
                    )
                    last_cleanup_date = today
                    if deleted:
                        logger.info("[campus_inbox] 保留期清理删除 %s 条消息", deleted)
                if now.strftime("%H:%M") != self.settings.digest_time:
                    continue
                local_date = today
                items = await asyncio.to_thread(self.storage.list_digest_items)
                text = build_digest_text(items, now) or self._empty_digest_text(now)
                claimed = await asyncio.to_thread(
                    self.storage.claim_digest,
                    self.settings.owner_qq, local_date, "scheduled", text,
                )
                if not claimed:
                    continue
                try:
                    await self._push_text(text)
                except Exception:
                    await asyncio.to_thread(
                        self.storage.release_digest,
                        self.settings.owner_qq, local_date, "scheduled",
                    )
                    logger.exception("[campus_inbox] 日报发送失败，已释放占位")
                    continue
                await asyncio.to_thread(
                    self.storage.mark_digest_sent,
                    self.settings.owner_qq, local_date, "scheduled",
                )
                logger.info("[campus_inbox] 日报已推送 %s", local_date)
            except Exception:
                logger.exception("[campus_inbox] 日报循环异常")

    def _empty_digest_text(self, now) -> str:
        return f"📮 校园日报 · {now:%m月%d日}\n\n今日无进行中事项。"

    async def _push_text(self, text: str) -> None:
        """经小号连接主动私聊推送给 owner。发送失败抛异常由调用方处理。"""
        ok = await self.context.send_message(
            self._dest_umo, MessageChain(chain=[Plain(text)])
        )
        if not ok:
            raise RuntimeError(f"send_message 未找到匹配平台: {self._dest_umo}")

    async def _handle_command(self, event: AstrMessageEvent):
        """T06 命令集：状态/日报/列表/完成/撤回/确认/订阅/退订。"""
        cmd = parse(event.get_message_str() or "")
        if cmd is None or cmd.name == "help":
            yield event.plain_result(HELP_TEXT)
            return

        if cmd.name == "status":
            stats = await asyncio.to_thread(self.storage.panel_stats)
            model = self.settings.external_ai_model if self.ai else "未启用"
            groups = len([s for s in self._effective.subscriptions if s.enabled])
            yield event.plain_result(
                "校园收件箱运行中\n"
                f"订阅 {groups} 个群 · 消息 {stats['messages']} · 事项 {stats['items']}\n"
                f"批次 成功{stats['batches_succeeded']}/失败{stats['batches_failed']} · "
                f"今日 AI {stats['batches_today']}/{self.settings.daily_call_limit}\n"
                f"模型 {model} · 日报 {self.settings.digest_time}"
            )
            return

        if cmd.name == "digest":
            now = local_now(self.settings.timezone)
            items = await asyncio.to_thread(self.storage.list_digest_items)
            digest = build_digest_text(items, now) or self._empty_digest_text(now)
            try:
                await self._push_text(digest)
            except Exception:
                logger.exception("[campus_inbox] 手动日报推送失败")
                yield event.plain_result("日报推送失败，详情见日志")
                return
            await asyncio.to_thread(
                self.storage.claim_digest,
                self.settings.owner_qq, now.strftime("%Y-%m-%d"), "manual", digest,
            )
            yield event.plain_result("日报已推送到本私聊 ✅")
            return

        if cmd.name == "list":
            items = await asyncio.to_thread(self.storage.list_digest_items)
            if not items:
                self._last_list = []
                yield event.plain_result("当前无进行中事项")
                return
            self._last_list = [it["item_id"] for it in items]
            lines = ["进行中事项："]
            for i, it in enumerate(items, 1):
                due = it["due_date"] or it["time_text"] or ""
                mark = " ⚠️待确认" if it["status"] == "needs_review" else ""
                lines.append(f"{i}. {it['title']}{f'（截止 {due}）' if due else ''}{mark}")
            lines.append("回复 /校园 完成 N 或 /校园 撤回 N")
            yield event.plain_result("\n".join(lines))
            return

        if cmd.name in ("done", "withdraw", "confirm"):
            item_id = self._resolve_number(cmd.args)
            if item_id is None:
                yield event.plain_result("编号无效，先发 /校园 列表 获取编号")
                return
            new_status = {"done": "done", "withdraw": "withdrawn",
                          "confirm": "active"}[cmd.name]
            ok = await asyncio.to_thread(
                self.storage.set_item_status, item_id, new_status, "私聊命令"
            )
            detail = self.storage.get_item_detail(item_id) if ok else None
            verb = {"done": "已完成", "withdraw": "已撤回", "confirm": "已确认"}[cmd.name]
            yield event.plain_result(
                f"{verb}：{detail['title']}" if detail else "事项不存在"
            )
            return

        if cmd.name == "subscribe":
            gid = cmd.args[0] if cmd.args else ""
            if not gid.isdigit():
                yield event.plain_result("用法：/校园 订阅 群号 [别名]")
                return
            alias = " ".join(cmd.args[1:])
            await asyncio.to_thread(
                self.storage.set_subscription_enabled,
                self.settings.collector_platform_id,
                self.settings.collector_self_id, gid, True, alias,
            )
            self._effective = self._build_effective()
            yield event.plain_result(f"已订阅群 {gid}{f'（{alias}）' if alias else ''}")
            return

        if cmd.name == "unsubscribe":
            gid = cmd.args[0] if cmd.args else ""
            if not gid.isdigit():
                yield event.plain_result("用法：/校园 退订 群号")
                return
            await asyncio.to_thread(
                self.storage.set_subscription_enabled,
                self.settings.collector_platform_id,
                self.settings.collector_self_id, gid, False,
            )
            self._effective = self._build_effective()
            yield event.plain_result(f"已退订群 {gid}")
            return

        yield event.plain_result(HELP_TEXT)

    def _resolve_number(self, args: tuple[str, ...]) -> str | None:
        if not args or not args[0].isdigit():
            return None
        n = int(args[0])
        if 1 <= n <= len(self._last_list):
            return self._last_list[n - 1]
        return None

    def _build_effective(self):
        """订阅以数据库为准（命令可改）；其余字段沿用配置文件。"""
        if "collector" in self.settings.disabled_reasons:
            return self.settings
        rows = self.storage.list_subscriptions(
            self.settings.collector_platform_id, self.settings.collector_self_id
        )
        subs = tuple(
            Subscription(r["group_id"], r["alias"], bool(r["enabled"])) for r in rows
        )
        return replace(self.settings, subscriptions=subs)

    async def _handle_notice(self, event: AstrMessageEvent, raw: dict) -> None:
        """通知事件：采集连接上的群撤回联动事项复核，其余通知静默阻断。

        小号连接的通知不拦截（其他插件如入群审批需要用）。
        """
        view = _EventView(event)
        if view.platform_id != self._effective.collector_platform_id or (
            view.self_id != self._effective.collector_self_id
        ):
            return
        event.stop_event()
        remote_id = parse_group_recall(raw)
        if remote_id is None:
            return
        subscribed = any(
            s.enabled and s.group_id == view.group_id
            for s in self._effective.subscriptions
        )
        if not subscribed:
            return
        await asyncio.to_thread(self._apply_recall, view, remote_id)

    def _apply_recall(self, view: _EventView, remote_id: str) -> None:
        key = f"{view.platform_id}:{view.self_id}:{view.group_id}:{remote_id}"
        if not self.storage.mark_message_revoked(key):
            return
        logger.info("[campus_inbox] 消息被撤回: %s", key)
        for item_id in self.storage.items_sourcing(key):
            if self.storage.all_sources_revoked(item_id):
                self.storage.set_item_status(
                    item_id, "needs_review", "全部来源消息被撤回"
                )
                logger.info("[campus_inbox] 事项 %s 转待确认（来源已撤回）", item_id)

    def _debug_mark(self, event: AstrMessageEvent, result) -> None:
        """临时调试标记：把路由决策写到 data_dir/debug.log，便于无日志环境验证。"""
        try:
            view = _EventView(event)
            line = (
                f"{datetime.now(timezone.utc).isoformat()} "
                f"platform={view.platform_id} self={view.self_id} "
                f"group={view.group_id or '-'} sender={view.sender_id} "
                f"decision={result.decision.value} identity={result.identity.value} "
                f"text={view.text[:60]!r}\n"
            )
            with open(self.data_dir / "debug.log", "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _store_sync(self, event: AstrMessageEvent) -> None:
        """同步段：规范化 + 幂等入库，成功后唤醒后台 worker。"""
        view = _EventView(event)
        source = SourceKey(view.platform_id, view.self_id, view.group_id)
        raw = _RawFromEvent(event, source)
        msg = normalize(raw, self._resolve_reply)
        if msg.media:  # 临时文件事件后会被清理，复制到插件目录
            kept = tuple(
                p for p in
                (persist_media(ref, self.data_dir / "media") for ref in msg.media)
                if p
            )
            msg = replace(msg, media=kept)
        result = ingest(self.storage, msg, wake=self._wake_event.set)
        if result == "rejected":
            logger.error("[campus_inbox] 消息入库失败: %s", msg.message_key)

    def _resolve_reply(self, source: SourceKey, remote_id: str) -> str | None:
        key = f"{source.as_str()}:{remote_id}"
        return key if self.storage.get_message_text(key) is not None else None
