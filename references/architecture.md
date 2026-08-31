# 微信通道架构（多 Agent 微信桥）

## 组件与职责

| 组件 | 路径 | 职责 |
|---|---|---|
| 微信桥 | `scripts/wechat_bridge.py` | 专线：微信 ↔ Agent CLI，点对点 |
| 桥数据 | `scripts/weixin_bridge/` | account.json（凭据）、state.json（游标/会话）、bridge.log（日志）、backend.json（后端配置） |
| 工作目录 | `backend.json` 的 `work_dir` | Agent 执行目录（默认仓库下 `wechat_work/`） |
| 守护 | 部署方配置 | 监控并自动拉起桥进程 |

## 消息流

用户微信 → iLink Bot API（getupdates 轮询，45s 超时）→ 桥分级处理 → 回复线程调 Agent（Codex 走常驻 app-server 长连接；`claude -p --output-format stream-json`；generic 命令模板；600s 超时，失败退避重试）→ 回发微信（sendmessage，每条 ≤800 字符分段）。

## 消息分级与优先级

- 新消息优先：待处理队列为 LIFO（`deque` + `appendleft`），最新消息排最前；每条新消息都会先收到确认回复，不会被长任务挡住。
- 任务/聊天车道：`handle_update` 四级路由——`casual` → `reply_q` 低推理模型；`medium` → `reply_q` 标准即时模型；`complex`/`long_task` → `task_q` 后台任务线程，先按消息内容自然确认再执行。
- 打断恢复：聊天到达且后台任务在跑 → 置位 + 按 reqId 定向 `turn/interrupt`；任务线程检测到预占后保存恢复标记（sid+desc），等聊天清空后放回队首，用「继续执行刚才被打断的任务，从断点接着做」续做。
- 简单消息：问候/确认/状态类（`FAST_REPLIES` 模板）在轮询线程直接秒回，不排队、不调模型。
- 中等消息：立即回「收到，我看看，马上给你结果。」，再单轮处理。
- 复杂消息：含任务关键词或超 80 字 → 立即回「收到，开始处理」，异步执行；Agent 按协议输出【计划】/【进度】/【总结】标记行，桥实时转发；产出文件输出【文件】<路径>，桥自动发回。
- 停止：`STOP_PHRASES`（停/等等/等一下/算了/别做了…）终止当前 Agent 子进程（`_cancel_event` + `Popen.terminate`）。
- 指令：`/new`（新会话）、`/status`（状态）、`/sessions`（历史）、`/resume N`（切回），及中文等价说法（见 SKILL.md）。

## 会话与状态

- `state.json`：`sync_buf`（getupdates 游标）、`context_token`、`boss_chat`（绑定用户 chat id）、`codex_session`（Agent 会话 ID）、`seen`（已处理消息 ID）、`pending_replies`（待回队列，落盘防丢）、`sessions`（最近 10 条会话记录）。
- 错误码：`-14` 会话过期（需重新扫码）；`-2` 限流。

## 身份与人设

- `PRIMER` / `SYSTEM_HINT` 注入身份（`BACKEND_IDENTITY`：Codex / Claude / AI 助手），微信私聊以配置的身份回答。
- 基础人设包含三条硬规则（2026-08-14）：① 禁止自改模型/配置/数据库或重启服务，收到这类请求回复由部署方处理；② 干活不许闷头——动手前说明要做什么、关键节点主动报进展、卡住报状态、完成给总结；③ 复杂任务不硬扛，明确转交更强的桌面 Agent。其中②的“保证”是代码级：单轮 25s 无任何输出，`codex_reply`/`claude_reply`/`generic_reply` 自动发「⏳ 还在处理中…」（60s 节流），与模型是否遵守人设无关。
- 桥记忆：`work_dir/wechat_memory.md` 每轮注入提示词（`【微信桥记忆】`），对话后追加一行摘要（原子写 + `_state_lock`，`## 最近对话` 段滚动保留 30 条），线程重启不丢上下文。

## 注册（register）流程细节

- `register` 调 `get_bot_qrcode?bot_type=3` 获取二维码（qrcode 值 + qrcode_img_content）。
- 输出二维码 URL 到 stdout；若装有 qrcode 库会同时打印 ASCII 二维码。
- 轮询 `get_qrcode_status`：wait → scaned → confirmed；expired 自动刷新最多 3 次。
- confirmed 后写 account.json；注册期间旧桥仍在跑旧凭据，确认后必须重启桥生效。

## 多后端适配层

- 后端由 `scripts/weixin_bridge/backend.json` 选择：`codex`（默认）/ `claude` / `generic`；改后重启桥生效。
- `codex_reply`：**常驻连接**——桥启动 `codex app-server --listen ws://127.0.0.1:38123`（常驻进程），并通过 `scripts/codex_ws_helper.mjs`（node WebSocket 助手）保持长连接；协议为 JSON-RPC：`initialize` → `thread/start`（首条，可带 `baseInstructions` 人设）→ `turn/start`（含 `approvalPolicy: never`、`sandboxPolicy: {type: dangerFullAccess}`，可带 `model`/`effort` 覆盖档位）→ 订阅 `item/agentMessage/delta` 流式转发行进标记 → `turn/completed` 取结果；续聊复用同一 `threadId`，不再每条消息重启 Codex；**多消息并发**（读线程+事件队列，空闲续主会话、忙时并行开新线程）。`/stop` 走 `turn/interrupt`。会话过期先静默重试（约 1 小时），持续失效才自动重绑并通过 Windows Toast（右下角非阻塞）提醒。
- 事件按 turn 路由（2026-08-15）：`_turn_queues[req_id]` 每个 turn 一个独立队列，读线程按 `id` 派发（`ready`/`exit` 走共享队列），`codex_reply` try/finally 注册/注销；修复了多 worker 抢共享队列导致“结果被别的 turn 吞掉、3 分钟送不到”的饿死问题。
- 长命令不误杀（纯代码）：助手对工具调用/命令输出/文件变更/推理等非文字事件上报 `{"type":"activity","id":N}` 心跳，桥据此刷新单轮超时，只有真正长时间无任何动静才判定卡死；另有单轮 25s 无输出自动发「⏳ 还在处理中…」（60s 节流）作为代码级保活提示（不依赖模型输出）。
- `claude_reply`：`claude.exe -p ... --output-format stream-json --include-partial-messages --verbose --dangerously-skip-permissions`，按行解析 JSON：`stream_event` 的 text_delta 累积文本并扫【计划】/【进度】；`type=result` 取 `result` 与 `session_id`；`--resume <sid>` 续接。
- `generic_reply`：按 `new_cmd` / `resume_cmd` 模板（`{prompt}`/`{session}`）Popen 任意 CLI，stdout 拼接为回复，`session_regex` 提取会话 id，stdout/stderr 均扫进度标记。
- `agent_reply` 统一分派；路径由 `_resolve_tool_paths` 从 backend.json / 环境变量 / 常见安装位置解析。

## 通用 Agent 接入示例（generic 后端）

- **OpenCode**：`new_cmd: ["opencode", "run", "{prompt}"]`；`resume_cmd: ["opencode", "run", "-s", "{session}", "{prompt}"]`；非交互模式自动放行权限。
- **Gemini CLI**：`gemini -p "{prompt}"` 一次性回答；续接用 `-r/--resume <session>`（参数随版本略有差异，按安装版本核对）。
- **Qwen Code**：`qwen -p "{prompt}"`；`--resume <id>` 续接；`--yolo` 自动放行工具。
- **任意脚本/工具**：参考 `scripts/example_agent.py`（输出 `session=` 与【计划】【进度】【总结】标记即可接入）。

## 文件互传（iLink CDN + AES-128-ECB）

- 接收：消息 `item_list` 中 `type` 为 2/3/4/5（图片/语音/文件/视频）的项，取 `image_item/file_item/video_item/voice_item.media` 的 `full_url`（或 `encrypt_query_param` 拼下载地址）与 `aes_key`；下载后 AES-128-ECB 解密（key 支持 hex32 / base64(hex32) / base64(16B)），去掉 PKCS7 填充，文件名清洗后存 `work_dir/inbox/`，路径拼入提示词。
- 发送：随机 filekey + aeskey(hex32) → `ilink/bot/getuploadurl`（media_type 1=图片 3=文件）→ AES-128-ECB 加密后 POST `novac2c.cdn.weixin.qq.com/c2c/upload`（取响应头 `x-encrypted-param`）→ `sendmessage` 发 `image_item`（图片）或 `file_item`（其余）。上限 50MB。
- 触发：Agent 最终回复含【文件】<完整路径> 标记行（每行一个），桥自动发送并剔除标记行。

## 稳定性设计

- 单一 bot 稳定连接，不自动轮换账号
- 熔断器 + 指数退避 + 抖动，瞬时错误不傻等
- 会话过期（-14）自动暂停 → 生成新二维码 → 热加载，无需人工干预
- 发送失败落盘重试队列，重启不丢消息
- 单实例锁，防止双进程轮询被服务端踢
- 轮询悬挂看门狗，卡死自动自杀并交守护重启
- 活动心跳防误杀：长命令执行期间有工具事件即不算沉默，对话 90s / 任务 300s 超时只在真无动静时触发
