---
name: wechat-channel
description: 微信 AI 通道（多 Agent 微信桥）运维指南：检查桥接状态、诊断微信收不到 AI 回复的原因、处理 iLink 会话过期并重新扫码绑定、重启桥接服务。当用户提到微信通道、微信收不到回复、AI 在微信里没回话、桥接断了、需要重新绑定或注册微信机器人，或询问微信桥接服务状态时使用。
---

# WeChat Channel（微信通道）

## 核心事实

- 微信 AI 通道：`scripts/wechat_bridge.py` 是「微信桥」，用户微信私聊机器人 → iLink Bot API → 本机 Agent CLI（Codex / Claude / 任意 CLI）生成回复 → 回发微信。
- 微信 bot 只服务扫码绑定的那个微信号。
- 常驻连接：Codex app-server 常驻进程，桥保持 WebSocket 长连接，不每条消息重启 Codex（回复更快，续聊复用同一线程）。
- 关键路径：
  - 桥脚本：`scripts/wechat_bridge.py`
  - 桥数据目录：`weixin_bridge/`（`account.json`、`state.json`、`bridge.log`、`backend.json`）
  - 工作目录：`backend.json` 的 `work_dir`（默认仓库下 `wechat_work/`）
  - 看护：由部署方配置守护脚本保活

## 诊断（用户说「微信收不到回复 / 通道断了」）

1. 运行 `scripts/check_status.ps1`（Windows），检查进程、后端、账号、日志尾部与判定。
2. 看 `bridge.log` 尾部：
   - `❌ 微信会话过期(-14)` → 会话过期，最常见的断连原因，需要重新扫码注册（见下）。
   - `getupdates 异常` 网络超时 → 暂时网络问题，桥会自动重试，一般无需处理。
   - 最近出现 `📤 已回微信` → 通道正常。
3. 检查 `state.json` 的 `pending_replies` 是否有积压。

## 修复：会话过期

1. 后台运行注册：`python scripts/wechat_bridge.py register`，stdout 重定向到文件。
2. 从输出中取二维码链接，用 qrcode + PIL 生成 PNG，展示给用户用微信扫码。
3. 用户扫码确认后（register 进程退出 0，且 `account.json` 的 `saved_at` 更新）：
   - 先编辑 `state.json`：把 `codex_session` 和 `context_token` 置空；若需要补回用户刚发的消息，写入 `pending_replies`。
   - 再重启桥：结束正在运行的 `wechat_bridge.py run` 进程（守护脚本会自动拉起，或手动 `pythonw scripts/wechat_bridge.py run`）。
4. 验证：`bridge.log` 出现「🌉 … 启动」、不再报 -14，且出现「📤 已回微信」；让用户发一条测试消息确认有回复。

## 注意

- 微信回复规则：用户的新消息永远优先处理；消息三级分级——简单消息秒回（问候/确认类直接模板答）、中等消息先确认收到再处理、复杂消息确认后异步执行，并按任务步骤汇报：先发【计划】、每完成一步发【进度】、最后发【总结】（由 Agent 输出标记行，桥实时转发）。
- 干活不许闷头（基础人设，所有任务生效）：动手前一句话说明要做什么；关键步骤/节点主动汇报进展（做了什么、结果如何）；卡住或久等时主动说明状态；完成给总结。宁可多报不可少报。
- 复杂任务不硬扛：Agent 明确告诉用户转交更强的桌面 Agent，简述进度与上下文，由部署方转交。
- 桥记忆：`work_dir/wechat_memory.md` 每轮注入、对话后自动写回摘要（原子写、30 条滚动），线程重启不丢上下文。
- 模型分工可配置：`backend.json` 的 `chat_model`/`chat_effort`/`task_model`/`task_effort`（默认对话/任务均为 `deepseek-v4-flash` + `medium`）；对话快速档、任务质量档，仅 Codex 后端生效。
- 禁止自改：人设明确规定 Agent 不得自行修改桥配置/模型/数据库或重启服务；收到这类请求回复由部署方处理，绝不假装已改。
- 长命令不误杀：助手把工具执行/命令输出/文件变更等事件上报为活动心跳，桥只在真正无动静时才判定卡死。
- 文件互传：用户发来图片/文件 → 桥自动下载并 AES 解密，存到 `work_dir/inbox/`，路径随消息交给 Agent；Agent 产出文件时在最终回复输出【文件】<完整路径>，桥自动上传发回微信（图片按图片消息，其余按文件消息，上限 50MB）。
- 微信指令（中文习惯，斜杠命令仍兼容）：说「停 / 等等 / 等一下 / 算了」打断当前任务；「历史会话 / 会话 / 看历史」列会话；「切回第3个 / 第5个会话 / 回到第2个」切回；「继续 / 接着聊」续上次会话；「新任务 / 换个话题」开新会话；「状态」查通道。
- 二维码几分钟内过期，过期后注册脚本会自动刷新（最多 3 次）；提示用户尽快扫。
- 多后端与架构细节见 [references/architecture.md](references/architecture.md)。
