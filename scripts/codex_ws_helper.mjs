#!/usr/bin/env node
/* Codex app-server 常驻连接助手：桥保持一条 WS 长连接，避免每条消息重启 Codex。
 *
 * stdin 协议（JSON 行）：
 *   {"type":"turn","id":1,"threadId":null|"uuid","prompt":"...","cwd":"...","effort":"high"|null}
 *   {"type":"interrupt"}
 * stdout 协议（JSON 行）：
 *   {"type":"ready"}
 *   {"type":"progress","text":"..."}      # 【计划】/【进度】标记行
 *   {"type":"delta","text":"..."}          # 流式文本片段（可选）
 *   {"type":"result","text":"...","threadId":"...","id":1}
 *   {"type":"error","message":"...","id":1}
 */
import readline from "node:readline";

const WS_URL = process.env.CODEX_WS_URL || "ws://127.0.0.1:38123";
const WS_READY_TIMEOUT_MS = 30000;

let ws = null;
let nextId = 1;
const pending = new Map();
let cur = { threadId: null, turnId: null, buffer: "", sent: new Set(), reqId: null };

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
    ws.onclose = () => { /* 进程退出由桥侧守护处理 */ };
  });
}

function scanProgress(text) {
  for (const line of text.split("\n")) {
    const l = line.trim();
    const m = l.match(/【(计划|进度)】\s*(.+)/);
    if (m && !cur.sent.has(l) && !l.includes("步骤描述") && !l.includes("自动转发")) {
      cur.sent.add(l);
      out({ type: "progress", text: (m[1] === "计划" ? "📋 " : "✅ ") + m[2].trim() });
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
    const d = params.delta || "";
    cur.buffer += d;
    out({ type: "delta", text: d });
    scanProgress(d);
  } else if (method === "turn/started") {
    cur.turnId = params.turnId || null;
  } else if (method === "turn/completed") {
    const text = cur.buffer.trim();
    const reqId = cur.reqId;
    cur.buffer = "";
    cur.sent = new Set();
    cur.turnId = null;
    cur.reqId = null;
    out({ type: "result", text, threadId: cur.threadId, id: reqId });
  } else if (method === "turn/error" || method === "error") {
    const reqId = cur.reqId;
    cur.buffer = "";
    cur.reqId = null;
    out({ type: "error", message: String((params && (params.message || params.error)) || method), id: reqId });
  }
}

async function startTurn(cmd) {
  cur.sent = new Set();
  cur.buffer = "";
  cur.reqId = cmd.id ?? null;
  let threadId = cmd.threadId || null;
  try {
    if (!threadId) {
      const started = await rpc("thread/start", {
        cwd: cmd.cwd || process.cwd(),
        approvalPolicy: "never",
        sandbox: "danger-full-access",
      });
      threadId = started.thread.id || started.thread || started.id;
      cur.threadId = threadId;
    } else {
      cur.threadId = threadId;
    }
    const params = {
      threadId,
      input: [{ type: "text", text: cmd.prompt, text_elements: [] }],
      approvalPolicy: "never",
      sandboxPolicy: { type: "dangerFullAccess" },
    };
    if (cmd.effort) params.effort = cmd.effort;
    await rpc("turn/start", params);
  } catch (e) {
    out({ type: "error", message: String(e.message || e), id: cmd.id ?? null });
  }
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on("line", async (line) => {
  let cmd;
  try { cmd = JSON.parse(line); } catch { return; }
  if (cmd.type === "turn") await startTurn(cmd);
  else if (cmd.type === "interrupt") {
    if (cur.threadId && cur.turnId) {
      try { await rpc("turn/interrupt", { threadId: cur.threadId, turnId: cur.turnId }); } catch {}
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
