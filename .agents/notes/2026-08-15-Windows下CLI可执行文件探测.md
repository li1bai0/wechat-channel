# Windows 下 CLI 可执行文件探测必须覆盖 .cmd 壳与 claude.js

## Problem
Claude Code 在 Windows 上通过 npm 安装时，真实入口是 npm 全局目录的 `claude.cmd` 壳（Node 的 spawn 直接执行会 ENOENT）；npm 包内 `bin/` 只有 `claude.js`（需 node 启动）。桥原来的探测只找 `node_modules\@anthropic-ai\claude-code\bin\claude.exe`——Windows 上不存在，示例配置还让用户填这个路径 → 开箱即用直接失败。

## Decision
`_resolve_tool_paths` 的 claude 候选扩展为：`shutil.which("claude")` → npm 全局 `claude.cmd`/`claude` → 原生 `~/.local/bin/claude.exe` → 包内 `claude.js`（兜底）。`claude_reply` 对 `.js` 入口自动前置 node、对不存在路径给出明确安装提示；generic 后端同样对 `cmd[0]` 做 `shutil.which` 解析。

## Alternatives considered
- 要求用户必须手填 `claude_exe`：不是开箱即用，不采用。
- `shell: true` 跑 .cmd：Python 直接 Popen `.cmd` 已实测可用（rc 0），无需 shell；JS/Node 项目才需要该补丁。
- 只修探测不改 generic：同类问题会换一个后端再炸，一并修。

## Consequences
Windows/macOS/Linux 均自动探测可用，示例配置 `claude_exe` 改为留空。代价：探测多几个路径（一次性，可忽略）。
