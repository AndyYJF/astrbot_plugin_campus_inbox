# AstrBot 校园收件箱（campus_inbox）

一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件：**监听校园群通知，AI 提取成结构化待办事项**，通过 QQ 私聊推送每日摘要，并提供只读 Web 面板和 Todo API 供自建待办应用对接。

## 为什么需要它

年级群、宿舍群的通知混在几百条闲聊里，错过一条可能就是一次迟交。本插件用**双 QQ 账号**架构解决这个问题：

- **主号（收集端）**：只读。进群收消息，绝不发言、绝不回复，不会在群里暴露任何 bot 行为
- **小号（发送端）**：只写。负责私聊推送日报、响应 `/校园` 命令，同时不影响它作为其他 AstrBot 插件的正常 bot

## 功能

- 📥 **白名单群收集**：只监听配置的群，文本/图片都收，支持引用消息关联
- 🤖 **AI 结构化提取**：OpenAI 兼容接口（支持多模态，群里的通知截图也能读），输出标题/摘要/截止时间/行动/分类
- 🔀 **智能合并**：同一事项的补充通知自动并入已有事项，不产生重复卡片
- 📋 **事项生命周期**：active → done/withdrawn，每次变更留版本快照（revision），消息被撤回时事项自动转「待确认」
- 🌙 **每日摘要**：定时（默认 21:30）私聊推送当日待办
- 💬 **私聊命令**：`/校园 状态/日报/列表/订阅/退订/完成/撤回/帮助`
- 🖥️ **Web 面板**：单页应用，事项列表/筛选/详情抽屉/来源消息与配图，移动端适配
- 🔌 **Todo API**：REST 接口供自建待办应用拉取任务、回写完成状态（带乐观锁版本校验）
- 🗜️ **数据保留期**：原始消息 30 天、已撤回 7 天自动清理；事项与版本历史永久保留

## 安装

1. 把本目录放入 AstrBot 的 `data/plugins/` 下，重启 AstrBot
2. 在 AstrBot 里建两个 OneBot v11（aiocqhttp）平台连接：一个接主号、一个接小号
3. 在插件配置里填入：主号/小号的 platform_id 与 QQ 号、你的 owner_qq、外部 AI 的 base_url/api_key/model
4. 配置 `subscriptions`（`群号:别名` 每行一条），或进群后用 `/校园 订阅 群号 别名` 动态添加

## Todo API（对接自己的待办应用）

```
GET  /api/todo/v1/tasks?token=...&status=open&updated_since=<ISO>
POST /api/todo/v1/tasks/<task_id>/status?token=...   {"status":"completed","version":5}
GET  /media/<文件名>?token=...
```

- `status`：`open`（默认）/ `completed` / `cancelled` / `all`
- 回写带 `version`（乐观锁）：与当前 revision 不一致返回 412
- 来源含群别名、发送时间、原文前 500 字和配图路径；**不含群号与 QQ 号**

## 隐私与可靠性设计

- 主号绝对静默：所有路由决策在插件入口完成，非白名单消息直接 `stop_event`
- AI 提取失败不丢消息：批次制 + 退避重试，连续失败进入冷却队列后自动重试
- 密钥只存在于 AstrBot 服务端配置文件，不进插件数据库、不进本仓库
- Web 面板与 API 全部要求 token 鉴权，图片路由防路径穿越

## 测试

```bash
python -m unittest discover -s tests -v   # 111 个用例，无外部依赖，全程本地假件
```

## 技术栈

纯 Python 标准库 + AstrBot 插件 API（sqlite3 WAL / ThreadingHTTPServer / asyncio worker），无第三方依赖。

## License

MIT
