#!/usr/bin/env node
/* Codex app-server 常驻连接助手（支持并发多 turn）。
 *
 * stdin 协议（JSON 行）：
 *   {"type":"turn","id":1,"threadId":null|"uuid","prompt":"...","cwd":"...","effort":"high"|null}
 *   {"type":"interrupt"}
 * stdout 协议（JSON 行）：
 *   {"type":"ready"}
 *   {"type":"progress","text":"...","id":N}   # 【计划】/【进度】标记行
 *   {"type":"delta","text":"...","id":N}       # 流式文本片段
 *   {"type":"activity","id":N}                # 工具执行/命令输出等非文字活动（防误判卡死）
 *   {"type":"result","text":"...","threadId":"...","id":N}
 *   {"type":"error","message":"...","id":N}
 */
import readline from "node:readline";

const WS_URL = process.env.CODEX_WS_URL || "ws://127.0.0.1:38123";
const WS_READY_TIMEOUT_MS = 30000;

let ws = null;
let nextId = 1;
const pending = new Map();
const turns = new Map(); // turnId -> { reqId, threadId, buffer, linebuf, sent }

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function rpc(method, params) {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
  });
}

function connect() {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("WS 连接超时")), WS_READY_TIMEOUT_MS);
    ws = new WebSocket(WS_URL);
    ws.onopen = () => { clearTimeout(timer); resolve(); };
    ws.onerror = (e) => { clearTimeout(timer); reject(new Error(String((e && e.message) || e))); };
    ws.onmessage = (ev) => handle(JSON.parse(ev.data));
    ws.onclose = () => {};
  });
}

function scanProgress(st, text) {
  st.linebuf += text || "";
  const lines = st.linebuf.split("\n");
  st.linebuf = lines.pop();
  for (const raw of lines) {
    const l = raw.trim();
    const m = l.match(/【(计划|进度)】\s*(.+)/);
    if (m && !st.sent.has(l) && !l.includes("步骤描述") && !l.includes("自动转发")) {
      st.sent.add(l);
      out({ type: "progress", text: (m[1] === "计划" ? "📋 " : "✅ ") + m[2].trim(), id: st.reqId });
    }
  }
}

function handle(msg) {
  if (msg.id && pending.has(msg.id)) {
    const p = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) p.reject(new Error(JSON.stringify(msg.error)));
    else p.resolve(msg.result);
    return;
  }
  const method = msg.method || "";
  const params = msg.params || {};
  if (method === "item/agentMessage/delta") {
    const st = turns.get(params.turnId);
    if (st) {
      const d = params.delta || "";
      st.buffer += d;
      out({ type: "delta", text: d, id: st.reqId });
      scanProgress(st, d);
    }
  } else if (method === "turn/completed") {
    const turn = params.turn || {};
    const st = turns.get(turn.id);
    if (st) {
      scanProgress(st, "\n"); // 冲刷缓冲，补扫最后一行
      out({ type: "result", text: st.buffer.trim(), threadId: st.threadId, id: st.reqId });
      turns.delete(turn.id);
    }
  } else if (method === "turn/error" || method === "error") {
    const turnId = (params.turn || {}).id;
    const st = turns.get(turnId);
    if (st) {
      out({ type: "error", message: String((params && (params.message || params.error)) || method), id: st.reqId });
      turns.delete(turnId);
    }
  } else {
    // 工具调用/命令输出/文件变更/推理等事件都算“有活动”：转发心跳，
    // 让桥在长命令执行期间不会因为“没有文字输出”而误判卡死；
    // 同时按 20s 节流把“正在执行操作”作为进度转给微信（代码级保活，不依赖模型自觉）
    const turnId = params.turnId || (params.turn && params.turn.id);
    const st = turns.get(turnId);
    if (st) {
      out({ type: "activity", id: st.reqId });
      const nowMs = Date.now();
      if (!st.lastToolTs || nowMs - st.lastToolTs >= 20000) {
        st.lastToolTs = nowMs;
        out({ type: "progress", text: "▶️ 正在执行命令/工具…", id: st.reqId });
      }
    }
  }
}

async function startTurn(cmd) {
  const reqId = cmd.id ?? null;
  let threadId = cmd.threadId || null;
  try {
    if (!threadId) {
      const started = await rpc("thread/start", {
        cwd: cmd.cwd || process.cwd(),
        approvalPolicy: "never",
        sandbox: "danger-full-access",
        ...(cmd.base ? { baseInstructions: cmd.base } : {}),
      });
      threadId = started.thread.id || started.thread || started.id;
    } else {
      try {
        await rpc("thread/resume", { threadId });
      } catch (e) {
        // 线程不存在/失效：交给 turn/start 报 thread not found，由桥端开新线程并告知用户
      }
    }
    const params = {
      threadId,
      input: [{ type: "text", text: cmd.prompt, text_elements: [] }],
      approvalPolicy: "never",
      sandboxPolicy: { type: "dangerFullAccess" },
    };
    if (cmd.effort) params.effort = cmd.effort;
    if (cmd.model) params.model = cmd.model;
    const res = await rpc("turn/start", params);
    const turnId = res.turn && res.turn.id;
    if (turnId) {
      turns.set(turnId, { reqId, threadId, buffer: "", linebuf: "", sent: new Set(), lastToolTs: 0 });
    } else {
      out({ type: "error", message: "turn/start 未返回 turn.id", id: reqId });
    }
  } catch (e) {
    out({ type: "error", message: String(e.message || e), id: reqId });
  }
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on("line", async (line) => {
  let cmd;
  try { cmd = JSON.parse(line); } catch { return; }
  if (cmd.type === "turn") {
    await startTurn(cmd);
  } else if (cmd.type === "interrupt") {
    for (const [turnId, st] of turns.entries()) {
      if (cmd.reqId && st.reqId !== cmd.reqId) continue;
      try { await rpc("turn/interrupt", { threadId: st.threadId, turnId }); } catch {}
    }
  }
});

connect()
  .then(async () => {
    await rpc("initialize", { clientInfo: { name: "wechat-bridge", version: "1.0.0" }, capabilities: null });
    ws.send(JSON.stringify({ jsonrpc: "2.0", method: "initialized" }));
    out({ type: "ready" });
  })
  .catch((e) => {
    out({ type: "error", message: "初始化失败: " + String(e.message || e) });
    process.exit(1);
  });
