-- T03：批次记录创建时间，用于 daily_call_limit 统计。
ALTER TABLE extraction_batches ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
