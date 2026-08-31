#!/usr/bin/env bash
# wechat-channel setup for macOS/Linux
# Usage: bash scripts/install.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)/weixin_bridge"
WORK_DIR="$REPO/wechat_work"
BACKEND_PATH="$BRIDGE_DIR/backend.json"

echo "== wechat-channel setup (macOS/Linux) =="
echo "repo: $REPO"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.10+ first." >&2
  exit 1
fi
PY="$(command -v python3)"
$PY -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'

echo "python: $PY"

if ! command -v node >/dev/null 2>&1; then
  echo "node not found. Codex backend needs Node 18+." >&2
fi

if command -v codex >/dev/null 2>&1; then
  CODEX="$(command -v codex)"
  echo "codex: $CODEX"
fi

mkdir -p "$BRIDGE_DIR"
mkdir -p "$WORK_DIR"

if [ ! -f "$BACKEND_PATH" ]; then
  cat > "$BACKEND_PATH" <<EOF
{
  "backend": "codex",
  "node_exe": "",
  "codex_js": "",
  "claude_exe": "",
  "work_dir": "$WORK_DIR",
  "chat_model": "deepseek-v4-flash",
  "chat_effort": "medium",
  "casual_effort": "low",
  "task_model": "deepseek-v4-flash",
  "task_effort": "medium",
  "generic": {
    "new_cmd": ["myagent", "-p", "{prompt}"],
    "resume_cmd": ["myagent", "-p", "{prompt}", "-s", "{session}"],
    "session_regex": "session[=_ ]([0-9a-fA-F-]{8,})"
  }
}
EOF
  echo "created backend.json: $BACKEND_PATH"
else
  echo "backend.json already exists: $BACKEND_PATH"
fi

$PY -m pip install pycryptodome

if [ ! -f "$HOME/.codex/auth.json" ]; then
  echo "WARNING: Codex not logged in (~/.codex/auth.json missing)."
  echo "  Run: codex login  (or set provider/api key in ~/.codex/config.toml)"
fi


# codex-proxy (required for Codex + DeepSeek: Responses API -> Chat Completions)
if ! command -v codex-proxy >/dev/null 2>&1; then
  echo "WARNING: codex-proxy not found. Codex + DeepSeek needs it: npm install -g @lininn/codex-proxy"
else
  codex-proxy start >/dev/null 2>&1 || echo "codex-proxy start failed (check ~/.codexproxy/config.json)"
fi
echo
echo "Setup complete."
echo "Next steps:"
echo "  python scripts/wechat_bridge.py register   # scan QR with the bot WeChat account"
echo "  python scripts/wechat_bridge.py run        # persistent bridge"
echo "  python scripts/wechat_bridge.py status     # check status"
echo