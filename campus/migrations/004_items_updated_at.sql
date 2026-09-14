-- 004: 事项最后活动时间，用于面板按更新排序和「有更新」提示
ALTER TABLE items ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
UPDATE items SET updated_at = (
  SELECT max(created_at) FROM item_revisions WHERE item_id = items.item_id
) WHERE updated_at = '';
