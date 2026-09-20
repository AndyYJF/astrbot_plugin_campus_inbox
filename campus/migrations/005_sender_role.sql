-- 005: 发送者在群内的身份（owner/admin/member），用于提取时区分官方通知与闲聊
ALTER TABLE messages ADD COLUMN sender_role TEXT NOT NULL DEFAULT '';
