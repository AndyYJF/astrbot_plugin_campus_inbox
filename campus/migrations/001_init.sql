-- v0.1 初始表结构。设计依据：docs/design.md 第 7 节。
-- 所有内部 ID 用 UUID 文本；时间一律 UTC ISO 8601 文本。

CREATE TABLE IF NOT EXISTS subscriptions (
  source_key   TEXT PRIMARY KEY,  -- platform_id:self_id:group_id
  platform_id  TEXT NOT NULL,
  self_id      TEXT NOT NULL,
  group_id     TEXT NOT NULL,
  alias        TEXT NOT NULL DEFAULT '',
  enabled      INTEGER NOT NULL DEFAULT 1,
  UNIQUE (platform_id, self_id, group_id)
);

CREATE TABLE IF NOT EXISTS messages (
  message_key   TEXT PRIMARY KEY,  -- source_key:remote_id 或弱散列
  source_key    TEXT NOT NULL REFERENCES subscriptions(source_key),
  remote_id     TEXT NOT NULL DEFAULT '',
  sender_alias  TEXT NOT NULL DEFAULT '',
  sent_at       TEXT NOT NULL,
  received_at   TEXT NOT NULL,
  text          TEXT NOT NULL DEFAULT '',
  segments      TEXT NOT NULL DEFAULT '[]',
  reply_key     TEXT NOT NULL DEFAULT '',
  reply_unavailable INTEGER NOT NULL DEFAULT 0,
  parse_state   TEXT NOT NULL DEFAULT 'text',
  weak_identity INTEGER NOT NULL DEFAULT 0,
  revoked       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_source_sent ON messages (source_key, sent_at);

CREATE TABLE IF NOT EXISTS extraction_batches (
  batch_id        TEXT PRIMARY KEY,
  message_keys    TEXT NOT NULL,  -- JSON 数组，冻结输入
  state           TEXT NOT NULL DEFAULT 'pending',  -- pending/running/succeeded/failed
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL DEFAULT '',
  lease_until     TEXT NOT NULL DEFAULT '',
  model_ref       TEXT NOT NULL DEFAULT '',
  prompt_version  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_batches_state ON extraction_batches (state, next_attempt_at);

CREATE TABLE IF NOT EXISTS items (
  item_id          TEXT PRIMARY KEY,
  revision         INTEGER NOT NULL DEFAULT 1,
  category         TEXT NOT NULL DEFAULT 'uncertain',
  title            TEXT NOT NULL DEFAULT '',
  summary          TEXT NOT NULL DEFAULT '',
  audience         TEXT NOT NULL DEFAULT '',
  relevance        TEXT NOT NULL DEFAULT 'unknown',
  action_text      TEXT NOT NULL DEFAULT '',
  due_at           TEXT NOT NULL DEFAULT '',
  due_date         TEXT NOT NULL DEFAULT '',
  event_at         TEXT NOT NULL DEFAULT '',
  time_text        TEXT NOT NULL DEFAULT '',
  uncertain_fields TEXT NOT NULL DEFAULT '[]',
  status           TEXT NOT NULL DEFAULT 'active'  -- active/needs_review/withdrawn
);

CREATE TABLE IF NOT EXISTS item_revisions (
  item_id    TEXT NOT NULL REFERENCES items(item_id),
  revision   INTEGER NOT NULL,
  snapshot   TEXT NOT NULL,
  reason     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (item_id, revision)
);

CREATE TABLE IF NOT EXISTS item_sources (
  item_id     TEXT NOT NULL REFERENCES items(item_id),
  message_key TEXT NOT NULL REFERENCES messages(message_key),
  relation    TEXT NOT NULL DEFAULT 'original',  -- original/forward/correction/revoke
  PRIMARY KEY (item_id, message_key, relation)
);

CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY,
  item_id      TEXT NOT NULL REFERENCES items(item_id),
  action_key   TEXT NOT NULL,
  revision     INTEGER NOT NULL DEFAULT 1,
  state        TEXT NOT NULL DEFAULT 'proposed',  -- proposed/confirmed/ignored/unavailable
  payload      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS task_bindings (
  candidate_id          TEXT NOT NULL REFERENCES candidates(candidate_id),
  connector_id          TEXT NOT NULL,
  external_task_id      TEXT NOT NULL DEFAULT '',
  external_version      TEXT NOT NULL DEFAULT '',
  last_synced_revision  INTEGER NOT NULL DEFAULT 0,
  last_synced_payload   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (candidate_id, connector_id)
);

CREATE TABLE IF NOT EXISTS outbox (
  event_id        TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,  -- digest/todo
  idempotency_key TEXT NOT NULL UNIQUE,
  payload         TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'pending',  -- pending/sending/succeeded/failed/unknown
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL DEFAULT '',
  lease_until     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_outbox_state ON outbox (state, next_attempt_at);

CREATE TABLE IF NOT EXISTS digests (
  digest_id       TEXT PRIMARY KEY,
  owner           TEXT NOT NULL,
  local_date      TEXT NOT NULL,
  kind            TEXT NOT NULL,  -- scheduled/manual/supplement
  source_snapshot TEXT NOT NULL DEFAULT '[]',
  content         TEXT NOT NULL DEFAULT '',
  state           TEXT NOT NULL DEFAULT 'frozen',
  UNIQUE (owner, local_date, kind)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
