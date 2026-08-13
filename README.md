# wechat-channel · 微信连一切 Agent

**通过微信连接 Codex / Claude Code / Qwen / Gemini / OpenCode / Aider / 任意 CLI Agent，远程实时跟踪任务进度，不用守着电脑。**

> A self-hosted WeChat bridge for ANY CLI agent — send tasks from your phone, watch step-by-step progress in real time, get files back, and switch conversations with plain Chinese commands.

扫码绑定一个微信机器人号 → 手机发消息派活 → 你的 Agent 在电脑上干活 → 进度、结果、文件实时回到微信。全程走腾讯官方 iLink Bot 通道，无第三方服务器，数据都在你自己的机器上。

## 为什么是它

- **真正的 Agent 无关**：不只内置 Codex / Claude Code，任何能把回答打到标准输出的 CLI 都能接（OpenCode、Gemini、Qwen Code、Aider……或你自己的脚本），配置两行命令即可
- **远程实时看进度**：大型任务先报计划，每完成一步微信立刻收到一声，最后总结——不用守着电脑干等
- **常驻连接**：Codex app-server 常驻进程 + 桥保持长连接，不每条消息重启，回复更快
- **多消息并发**：你忙的时候发新消息立即并行处理，不用等前一个任务跑完
- **中文使用习惯**：说「停」「等等」就打断，「历史会话」看记录，「切回第3个」切换，「继续」接着聊
- **文件双向互传**：微信发文件给 Agent，Agent 产出文件自动发回微信
- **为长期运行而生**：熔断、自动重连重绑、发送失败落盘重试、看门狗，掉线自动恢复

## 功能特性

- 多后端：codex（默认）、claude（Claude Code）、generic（任意命令行 Agent，两行配置即可接入）
- 消息分级：简单消息秒回、中等消息先确认再处理、复杂消息异步执行
- 新消息优先：你的最新消息永远排最前，不会被长任务挡住
- 按步骤汇报：大型任务先报计划 → 每完成一步报一声 → 最后总结（不按时间机械刷屏）
- 文件互传：微信发文件/图片给 Agent；Agent 产出的文件自动发回微信（AES-128-ECB，上限 50MB）
- 会话管理：/sessions 列表、/resume N 切回、/new 开新会话
- 稳定性加固：熔断器+指数退避、会话过期自动重出二维码、发送失败落盘重试、轮询看门狗、单实例锁、单一 bot 稳定连接
- 常驻连接：Codex app-server 常驻，桥保持 WebSocket 长连接，不每条消息重启，回复延迟从约 20s 降到约 6s
- 指令：/stop 打断任务、/status 查看通道状态

## 支持的 Agent

| Agent | 新会话 | 续接会话 | 说明 |
|---|---|---|---|
| Codex（内置） | 常驻 app-server（WebSocket） | 同一线程续聊 | 已实测 |
| Claude Code（内置） | `claude -p "{prompt}" --output-format stream-json --include-partial-messages --dangerously-skip-permissions` | 加 `--resume <sid>` | 已实测，流式进度 |
| OpenCode | `opencode run "{prompt}"` | `opencode run -s <sid> "{prompt}"` | 非交互自动放行权限 |
| Qwen Code | `qwen -p "{prompt}"` | `qwen --resume <sid> -p "{prompt}"` | 加 `--yolo` 自动放行工具 |
| Gemini CLI | `gemini -p "{prompt}"` | `gemini -p "{prompt}" -r <sid>` | 参数随版本略有差异 |
| Aider | `aider --message "{prompt}" --yes-always` | 同一仓库自动延续上下文 | 续接方式不同，按版本核对 |
| 任意 CLI / 脚本 | `your_agent {prompt}` | 支持 `{session}` 占位符 | 见下方「接入其他 Agent」 |

> 各 Agent 的 CLI 参数随版本演进，以你安装版本的 `--help` 为准；表中的命令已经过官方文档核对。

## 架构

```text
手机微信 ⇄ iLink Bot API（官方通道） ⇄ 本机桥（wechat_bridge.py） ⇄ Agent CLI
                                                    │
                                                    ├─ codex app-server（常驻 WebSocket）
                                                    ├─ claude -p --output-format stream-json
                                                    └─ generic（自定义命令模板）
```

桥负责：收发消息、扫码绑定、消息分级、进度转发、文件互传、会话管理、稳定性兜底。Agent 只负责干活。

## 快速开始

### 环境要求

- Windows（macOS/Linux 需自行适配路径与守护方式）
- Python 3.10+，`pip install pycryptodome`
- 至少一个 Agent：Codex CLI 或 Claude Code（npm 安装）

### 1. 准备

```bash
git clone https://github.com/li1bai0/wxagent.git
cd wxagent
pip install pycryptodome
```

把 `scripts/backend.example.json` 复制为 `weixin_bridge/backend.json` 并按本机路径修改。路径也可以省略，桥会自动在常见安装位置探测；`work_dir` 不填时默认用脚本旁的 `wechat_work/`。

### 2. 扫码绑定

```bash
python scripts/wechat_bridge.py register
```

终端输出二维码（并生成 PNG），用微信扫码确认，成功后写入 `weixin_bridge/account.json`。

### 3. 启动

```bash
python scripts/wechat_bridge.py run        # 常驻桥
python scripts/wechat_bridge.py status     # 查看状态
```

Windows 建议用 `pythonw` 后台运行，并配合任务计划/守护脚本保活。

### 切换后端

编辑 `weixin_bridge/backend.json` 的 `backend` 字段，重启桥：

- `"backend": "codex"` → Codex
- `"backend": "claude"` → Claude Code（需要 Claude 登录或 ANTHROPIC_API_KEY）
- `"backend": "generic"` → 任意 CLI Agent，配置 `generic.new_cmd` / `generic.resume_cmd`（`{prompt}`、`{session}` 占位符）与 `session_regex`

## 微信命令

中文习惯，斜杠命令仍兼容：

| 发送 | 作用 |
|---|---|
| 任意消息 | 派活 |
| 停 / 等等 / 等一下 / 算了 / 别做了 | 打断当前任务 |
| 状态 | 查看通道状态 |
| 新任务 / 换个话题 / 重开 | 开启全新会话 |
| 历史 / 历史会话 / 看历史 | 列出历史会话 |
| 切回第3个 / 第5个会话 / 回到第2个 | 切回指定会话 |
| 继续 / 接着聊 | 续上次会话 |

## 接入其他 Agent（generic 后端）

任何能把回答打到标准输出的 CLI Agent 都能接。在 `backend.json` 里配置命令模板即可：

```json
{
  "backend": "generic",
  "generic": {
    "new_cmd": ["opencode", "run", "{prompt}"],
    "resume_cmd": ["opencode", "run", "-s", "{session}", "{prompt}"],
    "session_regex": "[0-9a-fA-F-]{36}"
  }
}
```

常用示例：

- **OpenCode**：`opencode run "{prompt}"` 非交互执行；`-s/--session <id>` 续接；非交互模式自动放行权限。
- **Gemini CLI**：`gemini -p "{prompt}"` 一次性回答；续接用 `-r/--resume <session>`（参数随版本略有差异，按你安装的版本核对）。
- **任意脚本**：参考 [scripts/example_agent.py](scripts/example_agent.py) —— 输出 `session=<id>` 与【计划】【进度】【总结】标记行即可接入，桥会自动转发进度、管理会话。该机制已实测：新会话、续接、步骤进度转发全部跑通。

## 消息分级与进度汇报

- 简单消息（问候/确认/状态）→ 秒回
- 中等消息 → 先回「收到，我看看，马上给你结果」再处理
- 复杂消息 → 先回「收到，开始处理」，异步执行；Agent 按协议输出 `【计划】`、`【进度】第N步完成`、`【总结】` 标记行，桥实时转发到微信；产出文件时输出 `【文件】<路径>`，桥自动发回

## 稳定性设计

- 单一 bot 稳定连接，不自动轮换账号
- 熔断器 + 指数退避 + 抖动
- 会话过期（-14）自动暂停 → 生成新二维码 → 热加载
- 发送失败落盘重试队列，重启不丢消息
- 单实例锁，防止双进程轮询被服务端踢
- 轮询悬挂看门狗，卡死自动自杀并交守护重启

## 安全说明

- 只响应扫码绑定的那个微信号（`account.json` 的 `user_id`）
- 桥以完全权限运行 Agent（可读写文件、联网执行命令）——只建议给信得过的人开
- 提示注入风险：微信里可能收到来自陌生人的恶意内容，注意分辨
- 文件互传会下载微信发来的文件并解密到本地 `work_dir/inbox/`，注意来源

## 与现有方案对比

- **Agent 支持**：本方案任意 CLI（内置 Codex/Claude，附 OpenCode/Qwen/Gemini/Aider 配置）；WEXX 仅 Codex；CLI-WeChat-Bridge 支持 Codex/Claude/OpenCode；wechat-ai-bridge 支持 Claude/Codex/Gemini
- **远程实时看进度**：本方案按步骤汇报（计划→每步→总结）；其余均无步骤级汇报
- **中文指令**：本方案完整（停/等等/历史/切回第N个/继续）；其余以英文指令为主
- **文件互传**：本方案双向 50MB；WEXX 尚在计划中；其余双向
- **稳定性加固**：本方案熔断/自动重绑/重试队列/看门狗/单实例锁；其余为基础或部分覆盖
- **协议**：本方案 MIT；WEXX MIT；CLI-WeChat-Bridge AGPL-3.0；wechat-ai-bridge MIT

定位差异：WEXX 是「单 Agent 轻量接入」，CLI-WeChat-Bridge 是「本地终端会话增强」，本方案主打 **Agent 无关 + 中文使用习惯 + 远程实时进度**。

## 路线图 / 贡献

- 更多 Agent 适配配置（欢迎 PR：补一行 `new_cmd`/`resume_cmd` 就是一份适配）
- 多用户/多微信支持（目前单 bot 单用户，安全性优先）
- 群聊支持（目前私聊为主）
- 语音消息转文字
- Linux/macOS 安装脚本与 systemd 守护

提 Issue、提 PR、点 Star 都是对项目最好的支持。

## License

MIT
