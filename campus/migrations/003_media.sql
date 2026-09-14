-- 003: 消息携带的图片 URL（JSON 数组），用于多模态抽取
ALTER TABLE messages ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]';
