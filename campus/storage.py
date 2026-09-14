"""SQLite 持久化：连接管理、迁移、消息写入与查询。

设计依据：docs/design.md 第 7 节。
WAL、foreign_keys=ON、busy_timeout；单写者由内部锁协调。
同步 API（stdlib sqlite3），异步调用方用 asyncio.to_thread 包装。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import NormalizedMessage, SourceKey

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class StorageError(Exception):
    pass


class Storage:
    FAILED_COOLDOWN_SECONDS = 3600  # failed 批次冷却 1 小时后自动重新排队

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def migrate(self) -> None:
        """按文件名顺序应用 migrations/*.sql，已应用的跳过。"""
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row[0]
                for row in self._conn.execute("SELECT version FROM schema_migrations")
            }
            files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
            for f in files:
                version = int(f.name.split("_")[0])
                if version in applied:
                    continue
                try:
                    with self._conn:
                        self._conn.executescript(f.read_text(encoding="utf-8"))
                        self._conn.execute(
                            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                            (version, _utcnow()),
                        )
                except sqlite3.Error as e:
                    raise StorageError(f"迁移 {f.name} 失败: {e}") from e

    # ---- 订阅 ----

    def ensure_subscription(self, source: SourceKey, alias: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO subscriptions (source_key, platform_id, self_id, group_id, alias)"
                " VALUES (?, ?, ?, ?, ?)",
                (source.as_str(), source.platform_id, source.self_id, source.group_id, alias or source.group_id),
            )

    # ---- 消息 ----

    def insert_message(self, msg: NormalizedMessage) -> str:
        """幂等写入。返回 inserted / duplicate。"""
        try:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO messages ("
                    "message_key, source_key, remote_id, sender_alias, sent_at, received_at,"
                    " text, segments, media_json, reply_key, reply_unavailable, parse_state, weak_identity)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        msg.message_key,
                        msg.source.as_str(),
                        msg.remote_id,
                        msg.sender_alias,
                        msg.sent_at,
                        msg.received_at,
                        msg.text,
                        msg.segments,
                        json.dumps(list(msg.media), ensure_ascii=False),
                        msg.reply_key,
                        int(msg.reply_unavailable),
                        msg.parse_state,
                        int(msg.weak_identity),
                    ),
                )
                return "inserted" if cur.rowcount == 1 else "duplicate"
        except sqlite3.Error as e:
            raise StorageError(f"消息写入失败: {e}") from e

    def get_message_text(self, message_key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT text FROM messages WHERE message_key = ?", (message_key,)
            ).fetchone()
            return row["text"] if row else None

    def get_message_row(self, message_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM messages WHERE message_key = ?", (message_key,)
            ).fetchone()

    def list_messages(
        self, source_key: str, date_from: str = "", date_to: str = "", page: int = 1, per_page: int = 20
    ) -> tuple[list[sqlite3.Row], int]:
        """按来源分页回查，按 sent_at 升序。返回 (rows, total)。"""
        where = "source_key = ?"
        params: list = [source_key]
        if date_from:
            where += " AND sent_at >= ?"
            params.append(date_from)
        if date_to:
            where += " AND sent_at < ?"
            params.append(date_to)
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE {where}", params
            ).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY sent_at, message_key"
                " LIMIT ? OFFSET ?",
                params + [per_page, max(page - 1, 0) * per_page],
            ).fetchall()
            return rows, total

    def count_pending_messages(self, source_key: str | None = None) -> int:
        """未进入任何批次的待处理消息数（T03 使用）。"""
        sql = (
            "SELECT COUNT(*) FROM messages m WHERE m.revoked = 0 AND NOT EXISTS ("
            "SELECT 1 FROM extraction_batches b WHERE b.state != 'failed'"
            " AND m.message_key IN (SELECT value FROM json_each(b.message_keys)))"
        )
        params: list = []
        if source_key:
            sql += " AND m.source_key = ?"
            params.append(source_key)
        with self._lock:
            return self._conn.execute(sql, params).fetchone()[0]

    # ---- T03 抽取批次 ----

    def list_unbatched_messages(self, limit: int) -> list[sqlite3.Row]:
        """未进入任何有效批次的消息，按时间升序。"""
        sql = (
            "SELECT m.* FROM messages m WHERE m.revoked = 0 AND NOT EXISTS ("
            "SELECT 1 FROM extraction_batches b WHERE b.state != 'failed'"
            " AND m.message_key IN (SELECT value FROM json_each(b.message_keys)))"
            " ORDER BY m.sent_at, m.message_key LIMIT ?"
        )
        with self._lock:
            return self._conn.execute(sql, (limit,)).fetchall()

    def create_batch(
        self, batch_id: str, message_keys: list[str], model_ref: str,
        prompt_version: str, lease_seconds: int = 300,
    ) -> None:
        now = datetime.now(timezone.utc)
        lease = now.timestamp() + lease_seconds
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO extraction_batches "
                "(batch_id, message_keys, state, attempts, next_attempt_at, lease_until, "
                "model_ref, prompt_version, created_at) "
                "VALUES (?, ?, 'running', 1, '', ?, ?, ?, ?)",
                (
                    batch_id,
                    json.dumps(message_keys, ensure_ascii=False),
                    datetime.fromtimestamp(lease, timezone.utc).isoformat(),
                    model_ref,
                    prompt_version,
                    now.isoformat(),
                ),
            )

    def finish_batch(self, batch_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE extraction_batches SET state='succeeded', lease_until='' "
                "WHERE batch_id = ?",
                (batch_id,),
            )

    def fail_batch(
        self, batch_id: str, max_attempts: int = 5, backoff_seconds: int = 300
    ) -> str:
        """attempts+1；未超限回到 pending 并设退避，超限转 failed。返回新状态。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT attempts FROM extraction_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                return "missing"
            attempts = row["attempts"] + 1
            if attempts >= max_attempts:
                # failed 不是终点：记录冷却时间，rearm_failed_batches 会重新排队
                cool_at = datetime.fromtimestamp(
                    datetime.now(timezone.utc).timestamp() + self.FAILED_COOLDOWN_SECONDS,
                    timezone.utc,
                ).isoformat()
                self._conn.execute(
                    "UPDATE extraction_batches SET state='failed', attempts=?, "
                    "next_attempt_at=?, lease_until='' WHERE batch_id = ?",
                    (attempts, cool_at, batch_id),
                )
                return "failed"
            next_at = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + backoff_seconds * attempts,
                timezone.utc,
            ).isoformat()
            self._conn.execute(
                "UPDATE extraction_batches SET state='pending', attempts=?, "
                "next_attempt_at=?, lease_until='' WHERE batch_id = ?",
                (attempts, next_at, batch_id),
            )
            return "pending"

    def rearm_failed_batches(self) -> int:
        """failed 批次冷却结束后自动重新排队（AI 故障恢复后自愈）。返回重新排队数。"""
        now = _utcnow()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE extraction_batches SET state='pending' "
                "WHERE state='failed' AND (next_attempt_at = '' OR next_attempt_at <= ?)",
                (now,),
            )
            return cur.rowcount

    def recover_expired_leases(self) -> int:
        """把租约过期的 running 批次放回 pending（重启后继续积压）。返回回收数。"""
        now = _utcnow()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE extraction_batches SET state='pending', lease_until='' "
                "WHERE state='running' AND lease_until != '' AND lease_until < ?",
                (now,),
            )
            return cur.rowcount

    def reclaim_batch(self, batch_id: str, lease_seconds: int = 300) -> None:
        """重试时重新占用租约：pending → running。"""
        lease = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + lease_seconds, timezone.utc
        ).isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE extraction_batches SET state='running', lease_until=? "
                "WHERE batch_id = ?",
                (lease, batch_id),
            )

    def claimable_batches(self) -> list[sqlite3.Row]:
        """到期可重试的 pending 批次。"""
        now = _utcnow()
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM extraction_batches WHERE state='pending' "
                "AND (next_attempt_at = '' OR next_attempt_at <= ?) ORDER BY created_at",
                (now,),
            ).fetchall()

    def count_batches_today(self) -> int:
        """今天（UTC）创建的批次数，用于 daily_call_limit。"""
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM extraction_batches WHERE created_at >= ?", (today,)
            ).fetchone()[0]

    def insert_items(self, items: list[dict], batch_id: str) -> None:
        """同事务写入 items + item_revisions + item_sources。

        items 元素结构：{"item": {...列值...}, "sources": [message_key, ...]}。
        """
        now = _utcnow()
        try:
            with self._lock, self._conn:
                for entry in items:
                    it = entry["item"]
                    self._conn.execute(
                        "INSERT INTO items (item_id, revision, category, title, summary, "
                        "audience, relevance, action_text, due_at, due_date, event_at, "
                        "time_text, uncertain_fields, status, updated_at) "
                        "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                        (
                            it["item_id"], it["category"], it["title"], it["summary"],
                            it["audience"], it["relevance"], it["action_text"],
                            it["due_at"], it["due_date"], it["event_at"],
                            it["time_text"], json.dumps(it["uncertain_fields"],
                                                         ensure_ascii=False),
                            _utcnow(),
                        ),
                    )
                    snapshot = {k: v for k, v in it.items() if k != "item_id"}
                    snapshot["item_id"] = it["item_id"]
                    self._conn.execute(
                        "INSERT INTO item_revisions (item_id, revision, snapshot, reason, created_at) "
                        "VALUES (?, 1, ?, ?, ?)",
                        (it["item_id"], json.dumps(snapshot, ensure_ascii=False),
                         f"批次 {batch_id} 抽取", now),
                    )
                    for mk in entry["sources"]:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO item_sources (item_id, message_key, relation) "
                            "VALUES (?, ?, 'original')",
                            (it["item_id"], mk),
                        )
        except sqlite3.Error as e:
            raise StorageError(f"事项写入失败: {e}") from e


    # ---- 合并去重（T04） ----

    def list_active_items_brief(self, limit: int = 50) -> list[sqlite3.Row]:
        """进行中事项的简报（供提取 prompt 引用）。"""
        with self._lock:
            return self._conn.execute(
                "SELECT item_id, title, category, due_date FROM items "
                "WHERE status = 'active' ORDER BY due_date DESC, item_id LIMIT ?",
                (limit,),
            ).fetchall()

    def _next_revision(self, item_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM item_revisions WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        return row[0] + 1

    def merge_into_item(self, item_id: str, sources: list[str], batch_id: str) -> bool:
        """把新来源挂到已有事项并记一条 revision。item 不存在返回 False。"""
        now = _utcnow()
        try:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "SELECT * FROM items WHERE item_id = ? AND status = 'active'", (item_id,)
                )
                row = cur.fetchone()
                if row is None:
                    return False
                for mk in sources:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO item_sources (item_id, message_key, relation) "
                        "VALUES (?, ?, 'original')",
                        (item_id, mk),
                    )
                rev = self._next_revision(item_id)
                snapshot = dict(row)
                self._conn.execute(
                    "INSERT INTO item_revisions (item_id, revision, snapshot, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (item_id, rev, json.dumps(snapshot, ensure_ascii=False),
                     f"合并批次 {batch_id} 来源", now),
                )
                self._conn.execute(
                    "UPDATE items SET revision = ?, updated_at = ? WHERE item_id = ?",
                    (rev, now, item_id)
                )
                return True
        except sqlite3.Error as e:
            raise StorageError(f"事项合并失败: {e}") from e

    def set_item_status(self, item_id: str, status: str, reason: str = "手动操作") -> bool:
        """更新事项状态（active/needs_review/withdrawn/done）并记 revision。"""
        if status not in ("active", "needs_review", "withdrawn", "done"):
            return False
        now = _utcnow()
        try:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "SELECT * FROM items WHERE item_id = ?", (item_id,)
                )
                row = cur.fetchone()
                if row is None:
                    return False
                self._conn.execute(
                    "UPDATE items SET status = ?, updated_at = ? WHERE item_id = ?",
                    (status, now, item_id)
                )
                rev = self._next_revision(item_id)
                snapshot = dict(row)
                snapshot["status"] = status
                self._conn.execute(
                    "INSERT INTO item_revisions (item_id, revision, snapshot, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (item_id, rev, json.dumps(snapshot, ensure_ascii=False), reason, now),
                )
                self._conn.execute(
                    "UPDATE items SET revision = ?, updated_at = ? WHERE item_id = ?",
                    (rev, now, item_id)
                )
                return True
        except sqlite3.Error as e:
            raise StorageError(f"状态更新失败: {e}") from e

    # ---- Todo API（T09） ----

    _TODO_STATUS_MAP = {"open": ("active", "needs_review"),
                        "completed": ("done",), "cancelled": ("withdrawn",)}

    def todo_tasks(self, status: str = "open", updated_since: str = "") -> list[dict]:
        """契约 v1 形状的待办任务。默认只给 open（active+needs_review）。

        隐私：不含群号和 QQ 号；原文截取前 500 字（链接等信息需要回显），
        本地图片转成 /media/<文件名> 相对路径（调用方带 token 访问）。
        """
        where, params = "1=1", []
        if status != "all":
            states = self._TODO_STATUS_MAP.get(status, ("active", "needs_review"))
            where += f" AND status IN ({','.join('?' * len(states))})"
            params += list(states)
        if updated_since:
            where += " AND updated_at > ?"
            params.append(updated_since)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM items WHERE {where} ORDER BY updated_at", params
            ).fetchall()
            tasks = []
            for r in rows:
                sources = self._conn.execute(
                    "SELECT sub.alias AS group_alias, m.sent_at, "
                    "substr(m.text, 1, 500) AS text, m.media_json "
                    "FROM item_sources s JOIN messages m ON m.message_key = s.message_key "
                    "LEFT JOIN subscriptions sub ON sub.source_key = m.source_key "
                    "WHERE s.item_id = ?",
                    (r["item_id"],),
                ).fetchall()
                src_list = []
                for s in sources:
                    media = []
                    for m in json.loads(s["media_json"] or "[]"):
                        media.append(m if m.startswith("http")
                                     else f"/media/{m.rsplit('/', 1)[-1]}")
                    src_list.append({
                        "group_alias": s["group_alias"] or "",
                        "sent_at": s["sent_at"],
                        "text": s["text"] or "",
                        "media": media,
                    })
                tasks.append({
                    "task_id": r["item_id"],
                    "revision": r["revision"],
                    "title": r["title"],
                    "description": r["summary"],
                    "action_text": r["action_text"],
                    "category": r["category"],
                    "status": {"active": "open", "needs_review": "open",
                               "done": "completed", "withdrawn": "cancelled"}.get(
                                   r["status"], "open"),
                    "due_at": r["due_at"] or None,
                    "due_date": r["due_date"] or None,
                    "event_at": r["event_at"] or None,
                    "timezone": "Asia/Shanghai",
                    "time_text": r["time_text"],
                    "uncertain_fields": json.loads(r["uncertain_fields"] or "[]"),
                    "updated_at": r["updated_at"],
                    "sources": src_list,
                })
            return tasks

    def get_item_revision(self, item_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT revision FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
            return row[0] if row else None

    # ---- 保留期清理（T07b） ----

    def cleanup_messages(self, now_iso: str, raw_days: int = 30,
                         revoked_days: int = 7) -> int:
        """删除过期消息；返回删除条数。

        规则：被事项引用的消息永远保留（证据）；其余消息按 received_at 过期，
        已撤回消息用更短的 revoked_days。
        """
        try:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "DELETE FROM messages WHERE message_key NOT IN "
                    "(SELECT message_key FROM item_sources) AND ("
                    "  (revoked = 1 AND received_at < datetime(?, '-' || ? || ' days')) "
                    "  OR (revoked = 0 AND received_at < datetime(?, '-' || ? || ' days'))"
                    ")",
                    (now_iso, revoked_days, now_iso, raw_days),
                )
                return cur.rowcount
        except sqlite3.Error as e:
            raise StorageError(f"保留期清理失败: {e}") from e

    # ---- 撤回联动（T07a） ----

    def mark_message_revoked(self, message_key: str) -> bool:
        """标记消息已撤回。未知或已撤回返回 False。"""
        try:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "UPDATE messages SET revoked = 1 WHERE message_key = ? AND revoked = 0",
                    (message_key,),
                )
                return cur.rowcount > 0
        except sqlite3.Error as e:
            raise StorageError(f"撤回标记失败: {e}") from e

    def items_sourcing(self, message_key: str) -> list[str]:
        """引用了该消息的进行中/待确认事项 id。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.item_id FROM item_sources s JOIN items i ON i.item_id = s.item_id "
                "WHERE s.message_key = ? AND i.status IN ('active','needs_review')",
                (message_key,),
            ).fetchall()
            return [r[0] for r in rows]

    def all_sources_revoked(self, item_id: str) -> bool:
        """该事项的所有来源消息都已撤回。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM item_sources s JOIN messages m "
                "ON m.message_key = s.message_key "
                "WHERE s.item_id = ? AND m.revoked = 0",
                (item_id,),
            ).fetchone()
            return row[0] == 0

    # ---- 订阅管理（T06） ----

    def list_subscriptions(self, platform_id: str, self_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM subscriptions WHERE platform_id = ? AND self_id = ? "
                "ORDER BY group_id",
                (platform_id, self_id),
            ).fetchall()

    def set_subscription_enabled(
        self, platform_id: str, self_id: str, group_id: str, enabled: bool, alias: str = ""
    ) -> None:
        """启用/停用订阅；不存在则插入（别名随后可更新）。"""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO subscriptions (source_key, platform_id, self_id, group_id, "
                    "alias, enabled) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(platform_id, self_id, group_id) DO UPDATE SET "
                    "enabled = excluded.enabled, "
                    "alias = CASE WHEN excluded.alias != '' THEN excluded.alias "
                    "ELSE subscriptions.alias END",
                    (f"{platform_id}:{self_id}:{group_id}", platform_id, self_id,
                     group_id, alias, int(enabled)),
                )
        except sqlite3.Error as e:
            raise StorageError(f"订阅更新失败: {e}") from e

    # ---- 日报（T05） ----

    def list_digest_items(self) -> list[sqlite3.Row]:
        """日报候选：进行中/待确认且未标无关的事项，按截止时间排序。"""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM items WHERE status IN ('active','needs_review') "
                "AND relevance != 'irrelevant' "
                "ORDER BY CASE WHEN due_date = '' THEN 1 ELSE 0 END, due_date, due_at, item_id"
            ).fetchall()

    def claim_digest(self, owner: str, local_date: str, kind: str, content: str) -> bool:
        """占住当天该种类的日报位（防重发）。已占用返回 False。"""
        try:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO digests (digest_id, owner, local_date, kind, "
                    "content, state) VALUES (?, ?, ?, ?, ?, 'frozen')",
                    (uuid.uuid4().hex, owner, local_date, kind, content),
                )
                return cur.rowcount > 0
        except sqlite3.Error as e:
            raise StorageError(f"日报占位失败: {e}") from e

    def mark_digest_sent(self, owner: str, local_date: str, kind: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE digests SET state = 'sent' "
                "WHERE owner = ? AND local_date = ? AND kind = ?",
                (owner, local_date, kind),
            )

    def release_digest(self, owner: str, local_date: str, kind: str) -> None:
        """发送失败时释放占位，允许重试。"""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM digests WHERE owner = ? AND local_date = ? AND kind = ? "
                "AND state = 'frozen'",
                (owner, local_date, kind),
            )

    # ---- 面板只读查询 ----

    def list_items(
        self, status: str = "", category: str = "", page: int = 1, per_page: int = 50
    ) -> tuple[list[sqlite3.Row], int]:
        where, params = "1=1", []
        if status:
            where += " AND status = ?"
            params.append(status)
        if category:
            where += " AND category = ?"
            params.append(category)
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM items WHERE {where}", params
            ).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT * FROM items WHERE {where} "
                "ORDER BY updated_at DESC, item_id LIMIT ? OFFSET ?",
                params + [per_page, max(page - 1, 0) * per_page],
            ).fetchall()
            return rows, total

    def get_item_detail(self, item_id: str) -> dict | None:
        with self._lock:
            item = self._conn.execute(
                "SELECT * FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if item is None:
                return None
            d = dict(item)
            d["uncertain_fields"] = json.loads(d["uncertain_fields"] or "[]")
            d["sources"] = [
                {**dict(r), "media": json.loads(r["media_json"] or "[]")}
                for r in self._conn.execute(
                    "SELECT s.message_key, s.relation, m.sender_alias, m.sent_at, "
                    "substr(m.text, 1, 200) AS text, m.source_key, m.media_json "
                    "FROM item_sources s JOIN messages m ON m.message_key = s.message_key "
                    "WHERE s.item_id = ?",
                    (item_id,),
                ).fetchall()
            ]
            d["revisions"] = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT revision, reason, created_at FROM item_revisions "
                    "WHERE item_id = ? ORDER BY revision",
                    (item_id,),
                ).fetchall()
            ]
            return d

    def panel_stats(self) -> dict:
        with self._lock:
            q = lambda sql: self._conn.execute(sql).fetchone()[0]
            return {
                "messages": q("SELECT COUNT(*) FROM messages"),
                "items": q("SELECT COUNT(*) FROM items"),
                "batches_succeeded": q(
                    "SELECT COUNT(*) FROM extraction_batches WHERE state='succeeded'"
                ),
                "batches_failed": q(
                    "SELECT COUNT(*) FROM extraction_batches WHERE state='failed'"
                ),
                "batches_today": self.count_batches_today(),
            }


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
