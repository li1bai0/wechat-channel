---
name: wechat-channel
description: 微信 AI 通道（多 Agent 微信桥）运维指南：检查桥接状态、诊断微信收不到 AI 回复的原因、处理 iLink 会话过期并重新扫码绑定、重启桥接服务。当用户提到微信通道、微信收不到回复、AI 在微信里没回话、桥接断了、需要重新绑定或注册微信机器人，或询问微信桥接服务状态时使用。
---

# WeChat Channel（微信通道）

## 新机器 / 新 Agent 安装（先看这里）

拿到新电脑或让另一个 Agent 部署时，按仓库 README「快速开始（开箱即用）」做，不要自由发挥：

1. **装**：Windows 跑 `powershell -ExecutionPolicy Bypass -File scripts\Install.ps1`，macOS/Linux 跑 `bash scripts/install.sh`。脚本自动检测 Python / Node / Codex、装 `pycryptodome`、生成 `scripts/weixin_bridge/backend.json`。
2. **登录 Codex**：先确认 `~/.codex/auth.json` 存在（或跑 `codex login`、在 `~/.codex/config.toml` 配好 provider/key）。未登录时桥能启动但 Codex 后端会报认证错误。
3. **扫码**：`python scripts\wechat_bridge.py register`（用机器人微信号，不要用主号）。register 会打印终端 ASCII 二维码，并保存 PNG 到 `scripts/weixin_bridge/qrcode.png`——把这张图展示给用户扫码确认即可；终端乱码/显示不下时同样用 PNG。
4. **启动**：`pythonw scripts\wechat_bridge.py run`（Windows）或 `python scripts/wechat_bridge.py run`（macOS/Linux）。
5. **排障**：先跑 `python scripts/wechat_bridge.py doctor` 看环境，再跑 `status`；有问题看 `scripts/weixin_bridge/bridge.log` 尾部。

**硬性反模式（新 Agent 最容易踩的坑）：**

- 不要安装/使用 `wechat-acp-codex`、`@zed-industries/codex-acp`、`codex-acp`；本仓库是自研 `scripts/wechat_bridge.py`，不需要 ACP。
- 不要启用或依赖 `openclaw` 微信插件；它和本桥如果共用同一个 iLink 机器人账号会双 poller 抢消息、互踢登录。
- 不要直接运行桌面版 `codex.exe`（WindowsApps 目录可能 `Access is denied`）；用 npm 全局 `codex` 或 `~/.codex/bin/codex`。
- 遇到 `unknown variant 'max'` / `models.json` 解析失败，是 Codex CLI 版本与 `models.json` 里的 `effort:max` 不兼容；按 README 处理或用 `model_catalog_json` 指到去掉 `max` 的副本。

## 核心事实

- 微信 AI 通道：`scripts/wechat_bridge.py` 是「微信桥」，用户微信私聊机器人 → iLink Bot API → 本机 Agent CLI（Codex / Claude / 任意 CLI）生成回复 → 回发微信。
- 微信 bot 只服务扫码绑定的那个微信号。
- 常驻连接（2026-08-15 起 Codex / Claude 均常驻）：Codex 走 app-server + WebSocket 长连接；Claude 走 `scripts/claude_helper.py`（官方 Agent SDK 保持常驻 claude 子进程，`pip install claude-agent-sdk`）。两者都不再每条消息冷启动，多轮对话复用同一进程/线程，续聊不丢上下文。
- 关键路径：
  - 桥脚本：`scripts/wechat_bridge.py`
  - 桥数据目录：`scripts/weixin_bridge/`（`account.json`、`state.json`、`bridge.log`、`backend.json`）
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

- 微信回复规则：用户的新消息永远优先处理；消息四级分流——寒暄/确认走低推理模型快速理解上下文，普通问答走即时通道，复杂任务和超长任务先按内容确认再异步执行，并按实际步骤汇报计划、进度和总结。
- 干活不许闷头（人设 + 代码兜底）：复杂任务先给自然确认；普通问答单轮 12 秒无事件会发柔和状态提示，之后 60 秒节流，工具事件作为活动心跳防误判卡死。
- 任务/聊天车道（2026-08-15）：复杂任务进后台 FIFO 队列顺序执行（收到即回「任务已入队」），聊天走即时通道；任务执行中收到聊天 → 定向打断当前任务 turn（按 reqId）→ 聊天处理完 → 任务自动放回队首「从断点接着做」。
- 事件按 turn 路由（2026-08-15，纯代码）：每个 turn 独立事件队列，读线程按 id 派发，`ready`/`exit` 才走共享队列——多任务并发结果/进度不互抢、不吞。
- Claude 常驻（2026-08-15）：claude 后端优先走 `claude_helper.py` 常驻进程（CLAUDE.md/MCP 只在启动时加载一次），会话可续接、可切换（新会话自动重启子进程并 resume 磁盘 transcript）；helper 卡死由桥 HANG 检测 + 自动重启自愈；helper 不可用自动回退旧 spawn 模式保底。依赖：`pip install claude-agent-sdk`（与 Claude Code CLI 同时安装）。
- 复杂任务不硬扛：Agent 明确告诉用户转交更强的桌面 Agent，简述进度与上下文，由部署方转交。
- 桥记忆：`work_dir/wechat_memory.md` 每轮注入、对话后自动写回摘要（原子写、30 条滚动），线程重启不丢上下文。
- 模型分工可配置：`backend.json` 的 `chat_model`/`chat_effort`/`task_model`/`task_effort`/`casual_effort`（默认对话/任务 `deepseek-v4-flash` + `medium`，简单消息 `casual_effort=low` 降推理省 token）；复杂任务 task 档失败自动降级 chat 档重试一次（不被打断/未停止时），仅 Codex 后端生效。
- 禁止自改：人设明确规定 Agent 不得自行修改桥配置/模型/数据库或重启服务；收到这类请求回复由部署方处理，绝不假装已改。
- 长命令不误杀（纯代码）：助手把工具执行/命令输出/文件变更等事件上报为活动心跳，桥只在真正无动静时才判定卡死。
- 文件互传：用户发来图片/文件 → 桥自动下载并 AES 解密，存到 `work_dir/inbox/`，路径随消息交给 Agent；Agent 产出文件时在最终回复输出【文件】<完整路径>，桥自动上传发回微信（图片按图片消息，其余按文件消息，上限 50MB）。
- 微信指令（中文习惯，斜杠命令仍兼容）：说「停 / 等等 / 等一下 / 算了」打断当前任务；「历史会话 / 会话 / 看历史」列会话；「切回第3个 / 第5个会话 / 回到第2个」切回；「继续 / 接着聊」续上次会话；「新任务 / 换个话题」开新会话；「状态」查通道。
- 二维码几分钟内过期，过期后注册脚本会自动刷新（最多 3 次）；提示用户尽快扫。
- 多后端与架构细节见 [references/architecture.md](references/architecture.md)。
