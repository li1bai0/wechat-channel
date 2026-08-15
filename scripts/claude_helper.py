#!/usr/bin/env python3
"""Claude Code 常驻连接助手（与 codex_ws_helper.mjs 对等的常驻方案）。

启动后保持一个常驻 claude 子进程（ClaudeSDKClient），多轮对话复用同一进程，
CLAUDE.md / MCP 只在启动时加载一次，不再每条消息冷启动。

stdin 协议（JSON 行）：
  {"type":"turn","id":"<reqid>","session":null|"<sid>","prompt":"...","base":"..."}
  {"type":"interrupt","reqId":"<reqid>"}
  {"type":"reset"}

stdout 协议（JSON 行）：
  {"type":"ready"}
  {"type":"progress","text":"...","id":"<reqid>"}
  {"type":"delta","text":"...","id":"<reqid>"}
  {"type":"activity","id":"<reqid>"}
  {"type":"result","text":"...","session":"<sid>","id":"<reqid>"}
  {"type":"error","message":"...","id":"<reqid>"}
"""
import asyncio
import json
import os
import re
import sys
import threading

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

WORK_DIR = os.environ.get("CLAUDE_WORK_DIR") or os.getcwd()
CLI_PATH = os.environ.get("CLAUDE_CLI") or None
MODEL = os.environ.get("CLAUDE_MODEL") or None

loop = None
client = None
client_sid = None            # 当前常驻子进程绑定的会话 id（None=新会话）
cmd_queue = None
turn_task = None             # 当前正在跑的 turn 任务（串行）
pending_turns = []           # 排队等待的 turn
cur_req = None
cur_buf = ""
cur_linebuf = ""
cur_sent = set()


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def scan_progress(text):
    """按行扫描【计划】/【进度】标记，转发去重后的进度事件。"""
    global cur_linebuf
    cur_linebuf += text or ""
    lines = cur_linebuf.split("\n")
    cur_linebuf = lines.pop()
    for raw in lines:
        l = raw.strip()
        m = re.search(r"【(计划|进度)】\s*(.+)", l)
        if m and l not in cur_sent and "步骤描述" not in l and "自动转发" not in l:
            cur_sent.add(l)
            out({"type": "progress",
                 "text": ("📋 " if m.group(1) == "计划" else "✅ ") + m.group(2).strip(),
                 "id": cur_req})


def stdin_reader():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except Exception:
            continue
        asyncio.run_coroutine_threadsafe(cmd_queue.put(cmd), loop)


async def start_client(resume=None):
    """启动（或重启）常驻 claude 子进程。resume 指定会话时加载磁盘 transcript。"""
    global client, client_sid
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass
        client = None
    opts = ClaudeAgentOptions(cwd=WORK_DIR, permission_mode="bypassPermissions")
    if CLI_PATH:
        opts.cli_path = CLI_PATH
    if MODEL:
        opts.model = MODEL
    if resume:
        opts.resume = resume
    client = ClaudeSDKClient(options=opts)
    await client.connect()
    client_sid = resume


async def do_turn(cmd):
    global turn_task, client, client_sid, cur_req, cur_buf, cur_linebuf, cur_sent
    req_id = cmd.get("id")
    sid = cmd.get("session") or None
    prompt = (cmd.get("prompt") or "").strip()
    base = cmd.get("base") or ""
    cur_req = req_id
    cur_buf = ""
    cur_linebuf = ""
    cur_sent = set()
    try:
        if not prompt:
            out({"type": "error", "message": "空 prompt", "id": req_id})
            return
        # 会话不一致（或没有就绪的常驻进程）→ 重启子进程续接/开新会话
        need_new = (client is None
                    or (sid is not None and client_sid != sid)
                    or (sid is None and client_sid is not None))
        if need_new:
            await start_client(resume=sid)
        full_prompt = prompt
        if base and not sid:
            full_prompt = base.strip() + "\n\n" + prompt
        await client.query(full_prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for blk in msg.content or []:
                    if isinstance(blk, TextBlock):
                        d = blk.text or ""
                        if d:
                            cur_buf += d
                            out({"type": "delta", "text": d, "id": req_id})
                            scan_progress(d)
                    elif isinstance(blk, ToolUseBlock):
                        out({"type": "activity", "id": req_id})
            elif isinstance(msg, ResultMessage):
                text = (msg.result or "").strip() or cur_buf.strip()
                out({"type": "result", "text": text, "session": msg.session_id, "id": req_id})
                client_sid = msg.session_id
                return
    except Exception as e:
        out({"type": "error", "message": str(e)[:300], "id": req_id})
        # 子进程可能已失效：标记待重启，下次 turn 自动拉起
        try:
            if client is not None:
                await client.disconnect()
        except Exception:
            pass
        client = None
        client_sid = None
    finally:
        turn_task = None
        if pending_turns:
            nxt = pending_turns.pop(0)
            turn_task = asyncio.create_task(do_turn(nxt))


async def main():
    global loop, cmd_queue, client, turn_task
    for stream in (sys.stdout, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    loop = asyncio.get_running_loop()
    cmd_queue = asyncio.Queue()
    threading.Thread(target=stdin_reader, daemon=True).start()
    try:
        await start_client(resume=None)
    except Exception as e:
        out({"type": "error", "message": "初始化失败: " + str(e)[:200]})
        sys.exit(1)
    out({"type": "ready"})
    while True:
        cmd = await cmd_queue.get()
        t = cmd.get("type")
        if t == "turn":
            if turn_task is None:
                turn_task = asyncio.create_task(do_turn(cmd))
            else:
                pending_turns.append(cmd)
        elif t == "interrupt":
            rid = cmd.get("reqId")
            if turn_task is not None and (not rid or rid == cur_req) and client is not None:
                try:
                    await client.interrupt()
                except Exception:
                    pass
        elif t == "reset":
            await start_client(resume=None)
        elif t == "shutdown":
            break
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
