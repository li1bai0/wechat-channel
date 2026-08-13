#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Agent Bridge（多后端稳定性加固版）— iLink Bot API ↔ Agent CLI

微信 bot 只服务扫码绑定的那个微信用户，回复由本机 Agent CLI（Codex / Claude /
任意命令行 Agent）生成。稳定性设计要点：
  1. 熔断器 + 指数退避 + 抖动：瞬时错误不再线性傻等，连续失败跳闸 OPEN
  2. 会话过期(-14 / ret=-2+"unknown error")：
     - 暂停 10 分钟 → 重置熔断 → 自动重试
     - 发送时先去掉 context_token 重试一次
     - 自动生成新二维码并保存 PNG + 可选总线通告，等用户扫码；不自动切换其他账号
     - 检测到 account.json 被重新注册覆盖后自动热加载新账号
  3. 发送失败持久化 retry_queue.jsonl，启动自动排空重发（防丢消息）
  4. 单实例锁（防双进程并发轮询同一 bot 被服务端踢）
  5. 轮询悬挂看门狗：同步轮询整段卡死超时后自杀，交给守护拉起
  6. 重绑后自动清空旧账号的 codex_session/context_token 残留

用法：
  python wechat_bridge.py register   # 首次注册：出二维码，微信扫码绑定
  python wechat_bridge.py run        # 常驻桥（配合守护脚本保活）
  python wechat_bridge.py status     # 查看当前账号/队列/熔断状态
"""
import ctypes
import base64
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from Crypto.Cipher import AES

HERE = Path(__file__).parent
DATA_DIR = HERE / "weixin_bridge"
ACCOUNT_FILE = DATA_DIR / "account.json"
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "bridge.log"
LOCK_FILE = DATA_DIR / "bridge.lock"
RETRY_QUEUE_FILE = DATA_DIR / "retry_queue.jsonl"
QR_PNG = DATA_DIR / "qrcode.png"
ACCOUNTS_DIR = DATA_DIR / "accounts"

ILINK_DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"

BUS_SEND = ""  # 可选：桥状态通告地址，默认关闭（仅状态通告，不传私聊内容）

ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
MSG_TYPE_USER, MSG_TYPE_BOT, MSG_STATE_FINISH = 1, 2, 2
ERRCODE_SESSION_EXPIRED = -14
ERRCODE_RATE_LIMIT = -2
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"

_GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
BASH = _GIT_BASH if os.path.exists(_GIT_BASH) else "bash"
_DEFAULT_NODE = r"C:\Program Files\nodejs\node.exe"
_DEFAULT_CODEX_JS = ""  # 留空则自动探测（见 _resolve_tool_paths）
_DEFAULT_CLAUDE_EXE = ""
_DEFAULT_WORK_DIR = ""  # 留空则用仓库根目录下的 wechat_work/
REPLY_TIMEOUT = 600
BACKEND_FILE = DATA_DIR / "backend.json"
DEFAULT_BACKEND = "codex"
BACKEND_IDENTITY = {"codex": "Codex", "claude": "Claude", "generic": "AI 助手"}


def _resolve_tool_paths():
    """解析 agent 可执行文件路径：backend.json / 环境变量 / 常见安装位置。"""
    cfg = {}
    try:
        if BACKEND_FILE.exists():
            cfg = json.loads(BACKEND_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    codex_js = cfg.get("codex_js") or os.environ.get("CODEX_JS") or ""
    claude_exe = cfg.get("claude_exe") or os.environ.get("CLAUDE_EXE") or ""
    work_dir = cfg.get("work_dir") or os.environ.get("AGENT_WORK_DIR") or ""
    node_exe = cfg.get("node_exe") or os.environ.get("CODEX_NODE") or shutil.which("node") or _DEFAULT_NODE
    if not codex_js or not os.path.exists(codex_js):
        apd = os.environ.get("APPDATA", "")
        for cand in (_DEFAULT_CODEX_JS,
                     os.path.join(apd, "npm", "node_modules", "@openai", "codex", "bin", "codex.js")):
            if os.path.exists(cand):
                codex_js = cand
                break
    if not claude_exe or not os.path.exists(claude_exe):
        apd = os.environ.get("APPDATA", "")
        for cand in (_DEFAULT_CLAUDE_EXE,
                     os.path.join(apd, "npm", "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")):
            if os.path.exists(cand):
                claude_exe = cand
                break
    if not work_dir:
        work_dir = str(Path(__file__).resolve().parent.parent / "wechat_work")
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    return node_exe, codex_js, claude_exe, work_dir


CODEX_NODE, CODEX_JS, CLAUDE_EXE, CODEX_WORK_DIR = _resolve_tool_paths()
INBOX_DIR = Path(CODEX_WORK_DIR) / "inbox"

# ── 稳定性参数 ──────────────────────────────────────────────────────
CB_FAILURE_THRESHOLD = 6          # 连续失败次数 → 跳闸 OPEN
CB_OPEN_COOLDOWN = 60             # OPEN 后冷却秒数
CB_BASE_DELAY = 5.0               # 指数退避基数
CB_MAX_DELAY = 60.0               # 退避上限
CB_JITTER = 0.5                   # ±50% 抖动
SESSION_EXPIRED_PAUSE = 600       # 会话过期暂停秒数（10 分钟）
HANG_TIMEOUT_S = 300               # 轮询悬挂看门狗阈值
FLUSH_RETRY_INTERVAL = 30          # 重试队列排空周期
REBIND_ATTEMPT_COOLDOWN = 3600     # 自动重绑二维码触发冷却

SYSTEM_HINT = ("你是{identity}，正在微信里和用户私聊。像日常聊天一样自然回复："
               "口语化、直接、一两句话（内容需要时可以稍长），不要markdown列表、"
               "不要项目符号、不要客套前缀。这是微信后台通道：除非用户明确要求，"
               "不要执行任何语音播报，也不要提起自己是后台进程。用户的消息：\n")

PRIMER = (
    "【本体底稿】你是 {identity}，用户给你开的微信专线，这个微信 bot 只服务扫码绑定的那个用户。"
    "你和用户在微信私聊，消息直达你的本体会话，不走任何群。你跑在用户的电脑上。"
    "用户说话直接，不喜欢废话和客套。你是 AI 不是真人：做不到的事（发文件、发邮箱、打电话、"
    "线下操作）一律不许假装能做，直接说实话。你有完整工具权限，用户让干的活直接干，"
    "干完把结果简洁地告诉用户。\n\n"
)


# ── 消息分级：仅用于决定是否注入任务执行协议（复杂任务实时报进度）──

def _norm_msg(text):
    """归一化：去空白与常见标点，转小写，用于快速匹配。"""
    return re.sub(r"[\s，。？！!?、,.~～]+", "", str(text or "")).lower()


STOP_PHRASES = ("停", "停止", "停下", "暂停", "先停一下", "等等", "等一下", "等会", "等会儿",
                "别做了", "别干了", "算了", "不做了", "取消", "/stop")
STATUS_PHRASES = ("/status", "状态", "通道状态", "通道正常吗", "线路通吗", "桥正常吗")
NEW_PHRASES = ("/new", "新任务", "新会话", "开新会话", "换个话题", "重开", "从头开始", "清空会话")
SESSIONS_PHRASES = ("/sessions", "会话", "历史", "历史会话", "看历史", "会话列表", "有哪些会话", "看看历史")

COMPLEX_KEYWORDS = (
    "帮我", "写", "改", "创建", "生成", "分析", "整理", "统计", "处理", "下载", "上传",
    "翻译", "总结", "报告", "执行", "跑", "脚本", "代码", "程序", "文件", "数据",
    "表格", "文档", "图片", "视频", "网页", "项目", "部署", "修复", "检查", "排查",
    "调研", "设计", "实现", "发文件", "发图片",
)


def classify_message(text):
    """消息分级：complex（任务词/超长）→ 注入执行协议并实时报进度；其余 → 正常回复。"""
    t = str(text or "")
    if len(t) > 80:
        return "complex"
    for kw in COMPLEX_KEYWORDS:
        if kw in t:
            return "complex"
    if re.search(r"[?？]|吗|么|什么|怎么|多少|为什么|哪个|几点|帮我|请", t):
        return "medium"
    return "casual"


# ── 微信媒体收发（iLink CDN + AES-128-ECB） ──

def _norm_aes_key(key):
    """把 iLink 的 aes_key 统一成 16 字节：hex32 / base64(hex32) / base64(16字节)。"""
    if isinstance(key, bytes) and len(key) == 16:
        return key
    s = str(key or "").strip()
    try:
        decoded = base64.b64decode(s)
    except Exception:
        decoded = b""
    if len(decoded) == 16:
        return decoded
    text = decoded.decode("utf-8", "replace")
    if len(decoded) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", text):
        return bytes.fromhex(text)
    if re.fullmatch(r"[0-9a-fA-F]{32}", s):
        return bytes.fromhex(s)
    return decoded


def _sanitize_fname(name):
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip(" .")
    return (safe or "file")[:120]


def _download_media(item):
    """下载并解密 iLink 消息里的媒体，返回本地路径；失败返回 None。"""
    try:
        sub = item.get("image_item") or item.get("file_item") or item.get("video_item") or item.get("voice_item") or item
        media = sub.get("media") or {}
        cdn_url = media.get("full_url") or sub.get("cdn_url") or sub.get("full_url") or sub.get("url")
        if not cdn_url and media.get("encrypt_query_param"):
            cdn_url = f"{CDN_BASE}/download?encrypted_query_param={urllib.parse.quote(str(media['encrypt_query_param']))}"
        aes_key = media.get("aes_key") or sub.get("aes_key")
        if not aes_key and sub.get("aeskey"):
            aes_key = base64.b64encode(bytes.fromhex(str(sub["aeskey"]))).decode()
        fname = sub.get("file_name") or media.get("file_name") or ("image.jpg" if item.get("type") == ITEM_IMAGE else "file")
        if not cdn_url:
            log("媒体项无 CDN 地址，跳过")
            return None
        with urllib.request.urlopen(cdn_url, timeout=30) as r:
            data = r.read()
        if aes_key:
            key = _norm_aes_key(aes_key)
            if len(key) != 16:
                log(f"媒体 AES key 异常({len(key)}字节)，跳过")
                return None
            data = AES.new(key, AES.MODE_ECB).decrypt(data)
            pad = data[-1] if data else 0
            if 1 <= pad <= 16 and all(b == pad for b in data[-pad:]):
                data = data[:-pad]
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        path = INBOX_DIR / _sanitize_fname(fname)
        path.write_bytes(data)
        log(f"📎 已收媒体: {path.name} ({len(data)} bytes)")
        return str(path)
    except Exception as e:
        log(f"媒体下载失败: {str(e)[:120]}")
        return None


def _upload_media(base_url, token, path, media_type, to_user_id):
    """AES 加密上传文件到微信 CDN，返回 sendmessage 需要的引用参数。"""
    data = Path(path).read_bytes()
    if len(data) > 50 * 1024 * 1024:
        raise ValueError(f"文件过大：{len(data) // 1024 // 1024}MB（上限 50MB）")
    filekey = os.urandom(16).hex()
    aeskey_hex = os.urandom(16).hex()
    key = bytes.fromhex(aeskey_hex)
    pad = 16 - len(data) % 16
    encrypted = AES.new(key, AES.MODE_ECB).encrypt(data + bytes([pad]) * pad)
    body = {
        "filekey": filekey,
        "media_type": media_type,
        "to_user_id": to_user_id,
        "rawsize": len(data),
        "rawfilemd5": hashlib.md5(data).hexdigest(),
        "filesize": len(encrypted),
        "no_need_thumb": True,
        "aeskey": aeskey_hex,
        "base_info": {"channel_version": "1.0.2"},
    }
    resp = _req(f"{base_url}/{EP_GET_UPLOAD_URL}", body, token=token, timeout=30)
    upload_param = resp.get("upload_param") or resp.get("uploadParam") or ""
    if not upload_param:
        raise ValueError(f"getuploadurl 无 upload_param: {json.dumps(resp, ensure_ascii=False)[:200]}")
    upload_url = f"{CDN_BASE}/upload?encrypted_query_param={urllib.parse.quote(upload_param)}&filekey={urllib.parse.quote(filekey)}"
    req = urllib.request.Request(upload_url, data=encrypted,
                                 headers={"Content-Type": "application/octet-stream"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        download_param = r.headers.get("x-encrypted-param") or upload_param
    return {
        "aes_key": aeskey_hex,
        "file_key": filekey,
        "file_size": len(data),
        "file_size_encrypted": len(encrypted),
        "file_name": Path(path).name,
        "encrypt_query_param": download_param,
    }


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _safe_print(s=""):
    try:
        print(s, flush=True)
    except Exception:
        pass


def _req(url, payload=None, token=None, timeout=20, method=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def bus_notice(content):
    """仅桥状态通告（不含私聊内容）"""
    try:
        _req(BUS_SEND, {"agent": "Codex桥", "content": content})
    except Exception:
        pass


def _is_stale_session_ret(ret, errcode, errmsg):
    """ret=-2 + 'unknown error' = stale session 信号（同 errcode=-14）"""
    if ret != ERRCODE_RATE_LIMIT and errcode != ERRCODE_RATE_LIMIT:
        return False
    return bool(errmsg) and "unknown error" in str(errmsg).lower()


# ───────────────────────── 熔断器 ─────────────────────────────────────

class _CircuitBreaker:
    """三态 CLOSED/OPEN/HALF_OPEN + 指数退避 + 抖动，防止错误雪崩。"""

    def __init__(self, name, failure_threshold=CB_FAILURE_THRESHOLD,
                 open_cooldown=CB_OPEN_COOLDOWN, base_delay=CB_BASE_DELAY,
                 max_delay=CB_MAX_DELAY, jitter_factor=CB_JITTER):
        self._name = name
        self._threshold = failure_threshold
        self._cooldown = open_cooldown
        self._base = base_delay
        self._max = max_delay
        self._jitter = jitter_factor
        self._state = "CLOSED"
        self._failures = 0
        self._opened_at = 0.0
        self._last_delay = 0.0

    @property
    def state(self):
        return self._state

    @property
    def consecutive_failures(self):
        return self._failures

    @property
    def last_delay(self):
        return self._last_delay

    def before_request(self):
        """CLOSED 放行；OPEN 冷却到期后转 HALF_OPEN 放行。"""
        if self._state != "OPEN":
            return True
        if time.time() - self._opened_at >= self._cooldown:
            self._state = "HALF_OPEN"
            return True
        return False

    def remaining_cooldown(self):
        if self._state != "OPEN":
            return 0.0
        return max(0.0, self._cooldown - (time.time() - self._opened_at))

    def on_success(self):
        self._state = "CLOSED"
        self._failures = 0
        self._last_delay = 0.0

    def on_failure(self, is_rate_limit=False):
        """记录失败并返回本次应等待秒数（指数退避 + 抖动；限流 ×2）。"""
        self._failures += 1
        mult = 2.0 if is_rate_limit else 1.0
        delay = min(self._base * (2 ** (self._failures - 1)), self._max) * mult
        jitter_range = delay * self._jitter
        delay = max(1.0, delay + random.uniform(-jitter_range, jitter_range))
        self._last_delay = delay
        if self._failures >= self._threshold:
            self._state = "OPEN"
            self._opened_at = time.time()
            self._failures = 0
        return delay


# ───────────────────────── 发送重试队列（防丢消息） ─────────────────────────

def _save_to_retry_queue(to, text, context_token=None):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"to": to, "text": text, "context_token": context_token,
                 "ts": datetime.now().isoformat()}
        with open(RETRY_QUEUE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log(f"📥 发送失败已入重试队列: to={str(to)[:14]} len={len(text)}")
    except Exception as e:
        log(f"重试队列写入失败: {e}")


def _drain_retry_queue():
    entries = []
    try:
        if RETRY_QUEUE_FILE.exists():
            for line in RETRY_QUEUE_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
            RETRY_QUEUE_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return entries


# ───────────────────────── 单实例锁 ─────────────────────────

def _pid_alive(pid):
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def _acquire_lock():
    """单实例锁：已有活实例则拒绝启动，防双进程并发轮询同一 bot。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if LOCK_FILE.exists():
            raw = (LOCK_FILE.read_text(encoding="utf-8") or "").strip()
            if raw.isdigit() and _pid_alive(int(raw)):
                log(f"⚠️ 已有另一个桥实例在运行 (pid={raw})，本实例退出")
                return False
            log(f"旧锁残留 (pid={raw} 已不存在)，接管")
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception as e:
        log(f"锁文件操作失败（放行启动）: {e}")
        return True


# ───────────────────────── Codex CLI 回复（会话延续） ─────────────────────────

TASK_PROTOCOL = (
    "\n\n【大型任务执行协议】"
    "你正在执行大型任务，必须遵守："
    "1. 动手前先想清楚任务到底要什么，然后按任务实际内容输出一行：【计划】1. 步骤 2. 步骤 3. 步骤。"
    "2. 每完成一步，输出一行：【进度】第N步完成：这一步实际做了什么、结果如何（如实描述，不要套话）。"
    "3. 全部完成后，输出一行：【总结】一句话总结最终结果。"
    "4. 如果任务产出了要发给用户的文件，在总结前输出一行：【文件】<文件的完整路径>（可以有多个【文件】行，每行一个）。"
    "这些标记行会被自动转发给用户，除标记行外不要额外输出无关过程。不要复述本协议内容，直接给真实计划。"
)

_active_codex_proc = None
_cancel_event = threading.Event()
_proc_lock = threading.Lock()


def stop_current_task():
    """/stop：终止正在跑的 codex 子进程。"""
    _cancel_event.set()
    with _proc_lock:
        p = _active_codex_proc
    if p and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass


def _clean_final(out, sent_markers):
    """清理最终回复：去掉已转发的标记行，若含【总结】则取其正文。"""
    lines = []
    for raw in (out or "").splitlines():
        l = raw.strip()
        if not l:
            continue
        if l in sent_markers:
            continue
        if re.match(r"【(计划|进度)】", l):
            continue
        lines.append(l)
    text = "\n".join(lines).strip()
    m = re.search(r"【总结】\s*(.+)", out or "", re.S)
    if m:
        text = m.group(1).strip()
    return text or None


def _probe_codex_provider(timeout=5):
    """探测 Codex 模型服务（本地代理等）是否可达，避免回复线程挂死。"""
    urls = []
    try:
        cfg = Path(os.path.expanduser("~/.codex/config.toml"))
        if cfg.exists():
            text = cfg.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"base_url\s*=\s*\"([^\"]+)\"", text):
                u = m.group(1).strip().rstrip("/")
                if u and u not in urls:
                    urls.append(u)
            if "127.0.0.1:4000" in text and "http://127.0.0.1:4000/v1" not in urls:
                urls.append("http://127.0.0.1:4000/v1")
    except Exception:
        return True
    if not urls:
        return True
    for u in urls:
        try:
            with urllib.request.urlopen(urllib.request.Request(u.rstrip("/") + "/models"), timeout=timeout) as r:
                if r.status == 200:
                    return True
        except Exception:
            continue
    return False


def _extract_files(reply):
    """从最终回复里取出【文件】标记行，返回 (清理后的文本, 文件路径列表)。"""
    files = [m.strip().strip('"').strip("'") for m in re.findall(r"【文件】\s*([^\r\n]+)", reply or "")]
    cleaned = re.sub(r"【文件】[^\r\n]*\r?\n?", "", reply or "").strip()
    return cleaned, files


def codex_reply(prompt, session_id=None, on_progress=None):
    """调本机 Codex CLI 生成回复（微信后台通道）。支持 /stop 取消与按步骤转发进度。"""
    global _active_codex_proc
    if not _probe_codex_provider():
        log("⚠️ Codex 模型服务不可达（本地代理未启动），跳过本轮")
        return None, session_id
    base = [CODEX_NODE, CODEX_JS, "exec", "--skip-git-repo-check",
            "-c", "approval_policy=never"]
    for attempt in range(3):
        _cancel_event.clear()
        try:
            if session_id:
                cmd = base + ["resume", session_id, prompt]
            else:
                cmd = base + ["-s", "danger-full-access", prompt]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                    errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                    cwd=CODEX_WORK_DIR)
            with _proc_lock:
                _active_codex_proc = proc
            found_sid = [session_id]
            sent_markers = set()

            def _pump():
                for line in iter(proc.stderr.readline, ""):
                    m = re.search(r"session id:\s*([0-9a-fA-F-]{8,})", line)
                    if m:
                        found_sid[0] = m.group(1)
                    if on_progress and not _cancel_event.is_set():
                        mm = re.search(r"【(计划|进度)】\s*(.+)", line.strip())
                        if mm and line.strip() not in sent_markers \
                                and "步骤描述" not in line and "自动转发" not in line:
                            sent_markers.add(line.strip())
                            try:
                                on_progress(("📋 " if mm.group(1) == "计划" else "✅ ") + mm.group(2).strip())
                            except Exception:
                                pass

            pump_thread = threading.Thread(target=_pump, daemon=True)
            pump_thread.start()
            while proc.poll() is None:
                if _cancel_event.is_set():
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    proc.wait()
                    break
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    continue
            out = (proc.stdout.read() or "").strip()
            pump_thread.join(timeout=5)
            with _proc_lock:
                if _active_codex_proc is proc:
                    _active_codex_proc = None
            if _cancel_event.is_set():
                return None, session_id
            if proc.returncode == 0:
                reply = _clean_final(out, sent_markers)
                if reply:
                    return reply, found_sid[0] or session_id
            log(f"⚠️ codex CLI失败(第{attempt+1}次): rc={proc.returncode} out_len={len(out)}")
        except Exception as e:
            log(f"⚠️ codex CLI异常(第{attempt+1}次): {e}")
        time.sleep(20 * (attempt + 1))
    return None, session_id


# ───────────────────────── 多后端适配（Codex / Claude Code / 通用 CLI） ─────────────────────────

def _load_backend():
    """读取 backend.json 选择回复后端。返回 (backend, cfg)。"""
    try:
        if BACKEND_FILE.exists():
            cfg = json.loads(BACKEND_FILE.read_text(encoding="utf-8-sig"))
            b = str(cfg.get("backend") or DEFAULT_BACKEND).strip().lower()
            if b in ("codex", "claude", "generic"):
                return b, cfg
    except Exception as e:
        log(f"backend.json 读取失败（用默认 {DEFAULT_BACKEND}）: {e}")
    return DEFAULT_BACKEND, {}


def claude_reply(prompt, session_id=None, on_progress=None):
    """调本机 Claude Code CLI（stream-json）生成回复。支持续接、进度转发与 /stop。"""
    global _active_codex_proc
    base = [CLAUDE_EXE, "-p", prompt, "--output-format", "stream-json",
            "--include-partial-messages", "--verbose", "--dangerously-skip-permissions"]
    if session_id:
        base += ["--resume", session_id]
    for attempt in range(3):
        _cancel_event.clear()
        try:
            proc = subprocess.Popen(base, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                    errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                    cwd=CODEX_WORK_DIR)
            with _proc_lock:
                _active_codex_proc = proc
            result = {"sid": session_id, "reply": None}
            sent_markers = set()
            buf = ""

            def _handle_text(text):
                nonlocal buf
                if not text:
                    return
                buf += text
                lines = buf.split("\n")
                buf = lines.pop()
                for l in lines:
                    l = l.strip()
                    mm = re.search(r"【(计划|进度)】\s*(.+)", l)
                    if mm and on_progress and l not in sent_markers \
                            and "步骤描述" not in l and "自动转发" not in l:
                        sent_markers.add(l)
                        try:
                            on_progress(("📋 " if mm.group(1) == "计划" else "✅ ") + mm.group(2).strip())
                        except Exception:
                            pass

            def _pump():
                for line in iter(proc.stdout.readline, ""):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    t = obj.get("type")
                    if t == "stream_event":
                        ev = obj.get("event") or {}
                        if ev.get("type") == "content_block_delta":
                            d = ev.get("delta") or {}
                            if d.get("type") == "text_delta":
                                _handle_text(d.get("text") or "")
                    elif t == "assistant":
                        for blk in (obj.get("message") or {}).get("content") or []:
                            if blk.get("type") == "text":
                                _handle_text(blk.get("text") or "")
                    elif t == "result":
                        result["reply"] = str(obj.get("result") or "").strip()
                        result["sid"] = obj.get("session_id") or result["sid"]

            pump_thread = threading.Thread(target=_pump, daemon=True)
            pump_thread.start()
            while proc.poll() is None:
                if _cancel_event.is_set():
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    proc.wait()
                    break
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    continue
            _handle_text("\n")  # 冲刷缓冲，补扫最后一行
            pump_thread.join(timeout=5)
            with _proc_lock:
                if _active_codex_proc is proc:
                    _active_codex_proc = None
            if _cancel_event.is_set():
                return None, session_id
            if proc.returncode == 0 and result["reply"]:
                return result["reply"], result["sid"] or session_id
            log(f"⚠️ claude CLI失败(第{attempt+1}次): rc={proc.returncode}")
        except Exception as e:
            log(f"⚠️ claude CLI异常(第{attempt+1}次): {e}")
        time.sleep(20 * (attempt + 1))
    return None, session_id


def generic_reply(prompt, session_id=None, on_progress=None, cfg=None):
    """通用 CLI 后端：按 backend.json 里的 new_cmd/resume_cmd 模板调用任意 agent。"""
    global _active_codex_proc
    cfg = cfg or {}
    new_cmd = cfg.get("new_cmd") or []
    resume_cmd = cfg.get("resume_cmd") or []
    sess_re = cfg.get("session_regex") or r"session[=_ ]([0-9a-fA-F-]{8,})"
    if session_id and resume_cmd:
        cmd = [c.replace("{prompt}", prompt).replace("{session}", session_id) for c in resume_cmd]
    elif new_cmd:
        cmd = [c.replace("{prompt}", prompt) for c in new_cmd]
    else:
        log("generic 后端未配置 new_cmd/resume_cmd，无法调用")
        return None, session_id
    for attempt in range(3):
        _cancel_event.clear()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                    errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                    cwd=CODEX_WORK_DIR)
            with _proc_lock:
                _active_codex_proc = proc
            out_lines = []
            found_sid = [session_id]
            sent_markers = set()

            def _scan(line, is_err):
                m = re.search(sess_re, line)
                if m:
                    found_sid[0] = m.group(1)
                if on_progress and not _cancel_event.is_set():
                    mm = re.search(r"【(计划|进度)】\s*(.+)", line.strip())
                    if mm and line.strip() not in sent_markers \
                            and "步骤描述" not in line and "自动转发" not in line:
                        sent_markers.add(line.strip())
                        try:
                            on_progress(("📋 " if mm.group(1) == "计划" else "✅ ") + mm.group(2).strip())
                        except Exception:
                            pass

            def _pump_stdout():
                for line in iter(proc.stdout.readline, ""):
                    _scan(line, False)
                    out_lines.append(line)

            def _pump_stderr():
                for line in iter(proc.stderr.readline, ""):
                    _scan(line, True)

            t1 = threading.Thread(target=_pump_stdout, daemon=True)
            t2 = threading.Thread(target=_pump_stderr, daemon=True)
            t1.start()
            t2.start()
            while proc.poll() is None:
                if _cancel_event.is_set():
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    proc.wait()
                    break
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    continue
            t1.join(timeout=5)
            t2.join(timeout=5)
            with _proc_lock:
                if _active_codex_proc is proc:
                    _active_codex_proc = None
            if _cancel_event.is_set():
                return None, session_id
            reply = "".join(out_lines).strip()
            if proc.returncode == 0 and reply:
                return reply, found_sid[0] or session_id
            log(f"⚠️ generic CLI失败(第{attempt+1}次): rc={proc.returncode}")
        except Exception as e:
            log(f"⚠️ generic CLI异常(第{attempt+1}次): {e}")
        time.sleep(20 * (attempt + 1))
    return None, session_id


def agent_reply(backend, prompt, session_id=None, on_progress=None, cfg=None):
    """按后端分派回复生成。"""
    if backend == "claude":
        return claude_reply(prompt, session_id, on_progress)
    if backend == "generic":
        return generic_reply(prompt, session_id, on_progress, (cfg or {}).get("generic") or {})
    return codex_reply(prompt, session_id, on_progress)


# ───────────────────────── 注册（QR 扫码） ─────────────────────────

def register():
    base = ILINK_DEFAULT_BASE
    qr = _req(f"{base}/{EP_GET_BOT_QR}?bot_type=3", timeout=40)
    qrcode_value = str(qr.get("qrcode") or "")
    qrcode_url = str(qr.get("qrcode_img_content") or "")
    scan_data = qrcode_url or qrcode_value
    if not scan_data:
        log(f"QR 获取失败: {json.dumps(qr, ensure_ascii=False)[:300]}")
        return 1

    def _save_qr_png(data):
        try:
            import qrcode
            qrcode.make(data).save(QR_PNG)
            log(f"二维码图片已保存: {QR_PNG}")
        except Exception:
            pass

    _save_qr_png(scan_data)
    _safe_print("\n====== Codex 微信 bot 注册 ======")
    _safe_print("请用微信扫描以下链接对应的二维码（图片见 " + str(QR_PNG) + "）：")
    _safe_print(scan_data)
    try:
        import qrcode as _q
        _q.QRCode().add_data(scan_data).make(fit=True).print_ascii(invert=True)
    except Exception:
        pass

    refresh = 0
    while True:
        try:
            st = _req(f"{base}/{EP_GET_QR_STATUS}?qrcode={urllib.parse.quote(qrcode_value)}", timeout=40)
        except Exception as e:
            log(f"QR轮询异常: {e}")
            time.sleep(2)
            continue
        status = str(st.get("status") or "wait")
        if status == "wait":
            _safe_print(".")
        elif status == "scaned":
            log("已扫码，请在微信里确认…")
        elif status == "scaned_but_redirect":
            host = str(st.get("redirect_host") or "")
            if host:
                base = f"https://{host}"
                log(f"重定向到: {base}")
        elif status == "expired":
            refresh += 1
            if refresh > 3:
                log("二维码多次过期，注册失败，请重试")
                return 1
            log("二维码过期，刷新中…")
            qr = _req(f"{base}/{EP_GET_BOT_QR}?bot_type=3", timeout=40)
            qrcode_value = str(qr.get("qrcode") or "")
            qrcode_url = str(qr.get("qrcode_img_content") or "")
            _save_qr_png(qrcode_url or qrcode_value)
        elif status == "confirmed":
            account = {
                "account_id": str(st.get("ilink_bot_id") or ""),
                "token": str(st.get("bot_token") or ""),
                "base_url": str(st.get("baseurl") or base),
                "user_id": str(st.get("ilink_user_id") or ""),
                "saved_at": datetime.now().isoformat(),
            }
            if not account["account_id"] or not account["token"]:
                log("确认成功但凭据不完整，注册失败")
                return 1
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            ACCOUNT_FILE.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
            ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
            (ACCOUNTS_DIR / f"{account['account_id']}.json").write_text(
                json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"✅ 注册成功: {account['account_id']}")
            return 0
        time.sleep(1.5)


# ───────────────────────── 桥主循环（稳定性加固版） ─────────────────────────

class Bridge:
    def __init__(self):
        self.accounts_dir = ACCOUNTS_DIR
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.account = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
        self.state = {"sync_buf": "", "context_token": "", "boss_chat": "",
                      "codex_session": None, "session_account": "", "seen": [],
                      "sessions": []}
        if STATE_FILE.exists():
            try:
                self.state.update(json.loads(STATE_FILE.read_text(encoding="utf-8-sig")))
            except Exception:
                pass
        self.reply_q = deque()
        self._q_evt = threading.Event()
        for _t in self.state.pop("pending_replies", []) or []:
            if _t:
                self.reply_q.appendleft(_t)
        self.cb = _CircuitBreaker(self.account.get("account_id", "bridge"))
        self._last_poll_ts = time.time()
        self._retry_queue_flush_ts = 0.0
        self._expiry_pause_until = 0.0
        self._rebind_attempted_at = 0.0
        self._account_mtime = ACCOUNT_FILE.stat().st_mtime if ACCOUNT_FILE.exists() else 0.0
        self.backend, self.backend_cfg = _load_backend()
        # 当前主账号进池，作为后续备用
        self._persist_account_state(self.account, self.state)
        self._cleanup_stale_session()

    def save_state(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.state["seen"] = self.state["seen"][-200:]
        try:
            self.state["pending_replies"] = list(self.reply_q)
        except Exception:
            pass
        STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        self._persist_account_state(self.account, self.state)

    def _persist_account_state(self, account, state):
        try:
            acct = dict(account)
            acct["sync_buf"] = state.get("sync_buf", "")
            acct["context_token"] = state.get("context_token", "")
            acct["typing_ticket"] = state.get("typing_ticket", "")
            (self.accounts_dir / f"{account.get('account_id')}.json").write_text(
                json.dumps(acct, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log(f"账号池状态保存失败: {e}")

    def _cleanup_stale_session(self):
        """重绑后旧账号的 codex_session/context_token 必须清空，否则 Codex 回复全灭。"""
        sa = self.state.get("session_account") or ""
        if sa and sa != self.account.get("account_id"):
            log(f"🧹 检测到 codex_session 属于旧账号 {sa}，清空残留会话/令牌")
            self.state["codex_session"] = None
            self.state["context_token"] = ""
            self.state["typing_ticket"] = ""
            self.state["session_account"] = self.account.get("account_id")
            self.save_state()

    # ── 微信 → 回复队列 ──
    def ilink_loop(self):
        while True:
            try:
                self._last_poll_ts = time.time()

                # 定期排空发送重试队列
                if time.time() - self._retry_queue_flush_ts > FLUSH_RETRY_INTERVAL:
                    self._retry_queue_flush_ts = time.time()
                    self._flush_retry_queue()

                # 会话过期暂停期：期间检查热加载，新账号就绪立即恢复
                if time.time() < self._expiry_pause_until:
                    if self._check_account_reload():
                        self._expiry_pause_until = 0.0
                        continue
                    time.sleep(min(10, self._expiry_pause_until - time.time()))
                    continue

                # 熔断检查
                if not self.cb.before_request():
                    wait = self.cb.remaining_cooldown() or 60.0
                    log(f"🔒 熔断 OPEN，{int(wait)}s 后重试")
                    time.sleep(min(wait, 60.0))
                    continue

                resp = _req(f"{self.account['base_url']}/{EP_GET_UPDATES}",
                            {"get_updates_buf": self.state["sync_buf"]},
                            token=self.account["token"], timeout=45)
            except Exception as e:
                wait = self.cb.on_failure()
                log(f"⚠️ getupdates 异常(cb={self.cb.state} failures={self.cb.consecutive_failures} "
                    f"backoff={wait:.1f}s): {str(e)[:100]}")
                time.sleep(wait)
                continue

            ret = resp.get("ret", 0)
            errcode = resp.get("errcode", 0)
            if ret not in (0, None) or errcode not in (0, None):
                if errcode == ERRCODE_SESSION_EXPIRED or _is_stale_session_ret(ret, errcode, resp.get("errmsg")):
                    self._handle_session_expired()
                    continue
                if errcode == ERRCODE_RATE_LIMIT:
                    wait = self.cb.on_failure(is_rate_limit=True)
                    log(f"⚠️ 限流，退避 {wait:.1f}s")
                    time.sleep(min(wait, 30.0))
                    continue
                wait = self.cb.on_failure()
                log(f"⚠️ getupdates ret={ret} errcode={errcode} {str(resp.get('errmsg'))[:80]} "
                    f"(cb={self.cb.state} backoff={wait:.1f}s)")
                time.sleep(wait)
                continue

            self.cb.on_success()
            new_buf = resp.get("get_updates_buf")
            if new_buf:
                self.state["sync_buf"] = new_buf
            updates = resp.get("msgs") or resp.get("update_list") or resp.get("updates") or []
            if updates:
                self.save_state()
            for upd in updates:
                self.handle_update(upd)

    def _handle_session_expired(self):
        log("❌ 微信会话过期(-14/失效)，重置熔断；保持单一账号，暂停后自动重试")
        self.cb.on_success()  # 会话过期视为瞬时事件，不累计熔断失败
        self._expiry_pause_until = time.time() + SESSION_EXPIRED_PAUSE
        if time.time() - self._rebind_attempted_at > REBIND_ATTEMPT_COOLDOWN:
            self._rebind_attempted_at = time.time()
            bus_notice("@用户 微信专线会话过期；已自动生成新二维码，请尽快扫码（见 qrcode.png）")
            threading.Thread(target=self._auto_rebind, daemon=True).start()
        else:
            log(f"自动重绑冷却期内，{int(self._expiry_pause_until - time.time())}s 后重试")

    def _activate_account(self, account):
        """重绑热加载：保存旧状态，加载新账号状态，清空旧 AI 会话残留。"""
        self._persist_account_state(self.account, self.state)
        self.account = account
        self.cb = _CircuitBreaker(account.get("account_id", "bridge"))
        self.state["sync_buf"] = account.get("sync_buf", "")
        self.state["context_token"] = account.get("context_token", "")
        self.state["typing_ticket"] = account.get("typing_ticket", "")
        self.state["codex_session"] = None
        self.state["session_account"] = account.get("account_id")
        clean = {k: v for k, v in account.items() if k in ("account_id", "token", "base_url", "user_id", "saved_at")}
        try:
            ACCOUNT_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        self._account_mtime = ACCOUNT_FILE.stat().st_mtime if ACCOUNT_FILE.exists() else 0.0
        self.save_state()
        log(f"🔄 已加载新绑定账号 {account['account_id']}")

    def _check_account_reload(self):
        """检测 account.json 被重新注册覆盖 → 热加载新账号（返回是否切换）。"""
        try:
            if not ACCOUNT_FILE.exists():
                return False
            mtime = ACCOUNT_FILE.stat().st_mtime
            if mtime == self._account_mtime:
                return False
            self._account_mtime = mtime
            new = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
            if new.get("account_id") and new["account_id"] != self.account.get("account_id"):
                log(f"🔄 检测到新绑定 {new['account_id']}，热切换账号")
                self._activate_account(new)
                return True
        except Exception:
            pass
        return False

    def _auto_rebind(self):
        """后台自动重绑：生成二维码 PNG，用户扫码后 account.json 更新，主循环热加载。"""
        log("🔄 自动生成重新绑定二维码…")
        try:
            code = register()
            if code == 0:
                log("✅ 后台重绑完成，等待主循环热加载新账号")
                bus_notice("@用户 微信专线已重新扫码绑定成功，通道恢复中")
            else:
                log("⚠️ 自动重绑未完成（二维码过期或失败），需要人工 register")
                bus_notice("@用户 微信专线会话过期，自动重绑失败，请运行 register 扫码")
        except Exception as e:
            log(f"自动重绑线程异常: {e}")

    def handle_update(self, upd):
        if not isinstance(upd, dict):
            return
        msg = upd.get("message") if isinstance(upd.get("message"), dict) else upd
        if not isinstance(msg, dict):
            return
        from_user = msg.get("from_user_id", "")
        ctx = msg.get("context_token", "")
        msg_id = msg.get("message_id") or msg.get("client_id") or ""
        if msg_id and msg_id in self.state["seen"]:
            return
        if msg_id:
            self.state["seen"].append(msg_id)
        if ctx:
            self.state["context_token"] = ctx
        if msg.get("message_type") == MSG_TYPE_BOT:
            return
        texts = []
        attachments = []
        for item in (msg.get("item_list") or []):
            t = item.get("type")
            if t == ITEM_TEXT:
                txt = (item.get("text_item") or {}).get("text", "")
                if txt:
                    texts.append(txt)
            elif t in (ITEM_IMAGE, ITEM_FILE, ITEM_VOICE, ITEM_VIDEO):
                p = _download_media(item)
                if p:
                    attachments.append(p)
        text = "\n".join(texts).strip()
        if not from_user:
            return
        if not text and attachments:
            text = f"[用户发来 {len(attachments)} 个文件，请处理]"
        if not text:
            return
        if attachments:
            text += "\n\n[用户这次发来的文件]\n" + "\n".join(f"- {Path(a).name} -> {a}" for a in attachments)
        self.state["boss_chat"] = from_user
        log(f"📩 微信[{from_user[:14]}…]: {text[:60]}")
        norm = _norm_msg(text)
        raw = text.strip()
        if norm in STOP_PHRASES or (norm.startswith("等") and 2 <= len(norm) <= 6):
            stop_current_task()
            self.send_weixin("好，已停下。")
            return
        if norm in STATUS_PHRASES:
            identity = BACKEND_IDENTITY.get(self.backend, "Codex")
            self.send_typing()
            self.send_weixin(f"微信通道正常，{identity} 在线，有事直接说。")
            self.save_state()
            return
        if norm in NEW_PHRASES:
            self.state["codex_session"] = None
            self.save_state()
            self.send_weixin("好，已开启全新会话，你直接说事就行。")
            return
        m_resume = re.search(r"(?:切回|回到|继续|换到|第)\s*(\d+)\s*(?:个)?(?:会话)?\s*$", raw)
        if not m_resume:
            m_resume = re.fullmatch(r"/resume\s*(\d+)", raw) or re.fullmatch(r"resume\s*(\d+)", raw)
        if m_resume:
            idx = int(m_resume.group(1)) - 1
            sessions = self.state.get("sessions") or []
            if 0 <= idx < len(sessions):
                self.state["codex_session"] = sessions[idx]["sid"]
                self.save_state()
                self.send_weixin(f"好的，切回第{idx+1}个会话：{sessions[idx].get('desc', '')}")
            else:
                self.send_weixin(f"没有第{idx+1}个会话，发「历史会话」看看有哪些。")
            return
        if norm in ("继续", "接着聊", "续上", "继续聊"):
            sessions = self.state.get("sessions") or []
            if sessions:
                self.state["codex_session"] = sessions[-1]["sid"]
                self.save_state()
                self.send_weixin(f"好的，继续上次的会话：{sessions[-1].get('desc', '')}")
            else:
                self.send_weixin("没有历史会话，直接说新任务就行。")
            return
        if norm in SESSIONS_PHRASES:
            sessions = self.state.get("sessions") or []
            if not sessions:
                self.send_weixin("当前还没有历史会话，直接说事就行。")
            else:
                lines = ["历史会话："]
                for i, s in enumerate(sessions, 1):
                    lines.append(f"第{i}个：{s.get('desc', '')}（{str(s.get('ts', ''))[:16]}）")
                lines.append("回复「切回第N个」切换")
                self.send_weixin("\n".join(lines))
            return
        level = classify_message(text)
        log(f"💬 收到消息（{level}）：{text[:30]}")
        self.reply_q.appendleft(text)  # 新消息优先
        self._q_evt.set()
        self.save_state()

    def _get_typing_ticket(self):
        if self.state.get("typing_ticket"):
            return self.state["typing_ticket"]
        to = self.state["boss_chat"] or self.account.get("user_id", "")
        try:
            resp = _req(f"{self.account['base_url']}/{EP_GET_CONFIG}",
                        {"ilink_user_id": to, "context_token": self.state.get("context_token", "")},
                        token=self.account["token"], timeout=10)
            t = str(resp.get("typing_ticket") or "")
            if t:
                self.state["typing_ticket"] = t
            return t
        except Exception:
            return ""

    def send_typing(self):
        to = self.state["boss_chat"] or self.account.get("user_id", "")
        ticket = self._get_typing_ticket()
        if not to or not ticket:
            return
        try:
            _req(f"{self.account['base_url']}/{EP_SEND_TYPING}",
                 {"ilink_user_id": to, "typing_ticket": ticket, "status": 1},
                 token=self.account["token"], timeout=10)
        except Exception:
            pass

    # ── 回复工作线程：Agent CLI → 微信（新消息优先） ──
    def reply_worker(self):
        while True:
            self._q_evt.wait()
            self._q_evt.clear()
            if not self.reply_q:
                continue
            text = self.reply_q.popleft()
            try:
                level = classify_message(text)
                sid = self.state.get("codex_session")
                identity = BACKEND_IDENTITY.get(self.backend, "Codex")
                prompt = (PRIMER.format(identity=identity) if not sid else "") + SYSTEM_HINT.format(identity=identity) + text
                on_progress = None
                if level == "complex":
                    prompt += TASK_PROTOCOL
                    def on_progress(msg):
                        self.send_weixin(msg)
                reply, sid = agent_reply(self.backend, prompt, sid, on_progress=on_progress,
                                         cfg=self.backend_cfg)
                self.state["codex_session"] = sid
                self.state["session_account"] = self.account.get("account_id")
                if reply:
                    reply, files = _extract_files(reply)
                    for fp in files:
                        p = Path(fp).expanduser()
                        if not p.is_absolute():
                            p = Path(CODEX_WORK_DIR) / p
                        try:
                            if not self.send_file(str(p)):
                                self.send_weixin(f"文件发送失败：{fp}")
                        except Exception as e:
                            log(f"发送文件异常: {str(e)[:120]}")
                            self.send_weixin(f"文件发送失败：{str(e)[:80]}")
                    if reply.strip():
                        self.send_weixin(reply)
                    self._record_session(sid, text)
                    self.save_state()
                elif _cancel_event.is_set():
                    log("⏹ 任务被 /stop 停止")
                    _cancel_event.clear()
                else:
                    self.send_weixin("（暂时连不上模型服务，稍后再问我一次）")
            except Exception as e:
                log(f"⚠️ 回复线程异常: {e}")

    def _record_session(self, sid, text):
        if not sid:
            return
        sessions = self.state.setdefault("sessions", [])
        sessions = [s for s in sessions if s.get("sid") != sid]
        sessions.append({"sid": sid, "ts": datetime.now().isoformat(), "desc": text[:40]})
        self.state["sessions"] = sessions[-10:]

    def _post_message(self, item_list, to, ctx, timeout=20):
        msg = {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": f"k3b-{uuid.uuid4().hex[:16]}",
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": item_list,
        }
        payload = dict(msg)
        if ctx:
            payload["context_token"] = ctx
        try:
            resp = _req(f"{self.account['base_url']}/{EP_SEND_MESSAGE}", {"msg": payload},
                        token=self.account["token"], timeout=timeout)
            errcode = resp.get("errcode", 0)
            if errcode == ERRCODE_SESSION_EXPIRED:
                log("发送时会话已过期(-14)")
                return "expired"
            if errcode not in (0, None):
                log(f"发送返回 errcode={errcode} {str(resp.get('errmsg'))[:80]}")
                return "error"
            return "ok"
        except Exception as e:
            log(f"微信发送失败: {str(e)[:100]}")
            return "error"

    def _send_message(self, part, to, ctx, timeout=20):
        return self._post_message([{"type": ITEM_TEXT, "text_item": {"text": part}}], to, ctx, timeout)

    def send_file(self, path):
        """上传并发送文件/图片/视频/语音到微信。成功返回 True。"""
        p = Path(path)
        if not p.exists() or not p.is_file():
            log(f"发送文件不存在: {path}")
            return False
        to = self.state["boss_chat"] or self.account.get("user_id", "")
        if not to:
            log("⚠️ 还不知道用户chat_id，无法发文件")
            return False
        ext = p.suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            media_type, kind = 1, "image"
        else:
            media_type, kind = 3, "file"  # 视频/语音暂按文件通道发送（已验证的稳妥路径）
        try:
            ref = _upload_media(self.account["base_url"], self.account["token"], str(p), media_type, to)
        except Exception as e:
            log(f"文件上传失败: {str(e)[:120]}")
            return False
        ctx = self.state["context_token"]
        aes_b64 = base64.b64encode(ref["aes_key"].encode("utf-8")).decode("utf-8")
        if kind == "image":
            item = {"type": ITEM_IMAGE, "image_item": {"media": {
                "encrypt_query_param": ref["encrypt_query_param"],
                "aes_key": aes_b64, "encrypt_type": 1},
                "mid_size": ref["file_size_encrypted"]}}
        else:
            item = {"type": ITEM_FILE, "file_item": {"file_name": ref["file_name"],
                "file_size": ref["file_size"], "media": {
                "encrypt_query_param": ref["encrypt_query_param"],
                "aes_key": aes_b64, "encrypt_type": 1}}}
        r = self._post_message([item], to, ctx, timeout=60)
        if r == "expired":
            log("发文件遇会话过期，去掉 context_token 重试")
            r = self._post_message([item], to, None, timeout=60)
        if r != "ok":
            log(f"发文件失败: {p.name} (rc={r})")
            return False
        log(f"📎 已发文件: {p.name} ({kind})")
        return True

    def send_weixin(self, text):
        to = self.state["boss_chat"] or self.account.get("user_id", "")
        ctx = self.state["context_token"]
        if not to:
            log("⚠️ 还不知道用户chat_id，回复暂缓")
            return
        for i in range(0, len(text), 800):
            part = text[i:i + 800]
            r = self._send_message(part, to, ctx)
            if r == "expired":
                log("去掉 context_token 重试一次")
                r = self._send_message(part, to, None)
            if r != "ok":
                _save_to_retry_queue(to, part, None if r == "expired" else ctx)
            else:
                log(f"📤 已回微信: {part[:40]}")
            time.sleep(1)

    def _flush_retry_queue(self):
        entries = _drain_retry_queue()
        if not entries:
            return
        log(f"🔄 排空发送重试队列（{len(entries)} 条）…")
        for entry in entries:
            part = entry.get("text") or ""
            ctx = entry.get("context_token")
            to = entry.get("to") or self.state["boss_chat"] or self.account.get("user_id", "")
            if not part or not to:
                continue
            r = self._send_message(part, to, ctx, timeout=15)
            if r == "expired" and ctx:
                log("补发遇会话过期，去掉 context_token 重试")
                r = self._send_message(part, to, None, timeout=15)
            if r != "ok":
                _save_to_retry_queue(to, part, None if r == "expired" else ctx)
                continue
            log(f"📤 补发成功: {part[:40]}")

    # ── 轮询悬挂看门狗：防同步轮询整段卡死 ──
    def watchdog(self):
        while True:
            time.sleep(30)
            if time.time() < self._expiry_pause_until:
                continue
            if time.time() - self._last_poll_ts > HANG_TIMEOUT_S:
                log(f"❌ 轮询看门狗：超过 {HANG_TIMEOUT_S}s 无轮询心跳（疑似悬挂），自杀重启")
                os._exit(1)

    def _guarded(self, name, fn):
        """线程崩溃写日志（pythonw 无 stderr，崩溃必须落盘）"""
        try:
            fn()
        except Exception as e:
            log(f"❌ 线程 {name} 崩溃: {type(e).__name__}: {e}")
            os._exit(1)

    def run(self):
        if not _acquire_lock():
            return
        log(f"🌉 Codex 微信桥（专线版 v2）启动 (bot={self.account['account_id']})")
        t1 = threading.Thread(target=lambda: self._guarded("ilink", self.ilink_loop), daemon=True)
        t2 = threading.Thread(target=lambda: self._guarded("reply", self.reply_worker), daemon=True)
        t3 = threading.Thread(target=lambda: self._guarded("watchdog", self.watchdog), daemon=True)
        t1.start()
        t2.start()
        t3.start()
        while True:
            time.sleep(60)
            if not t1.is_alive() or not t2.is_alive():
                log("⚠️ 子线程退出，重启桥进程")
                os._exit(1)


def status():
    if not ACCOUNT_FILE.exists():
        print("未注册。先执行: python codex_weixin_bridge.py register")
        return 1
    a = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
    print(f"account_id: {a.get('account_id')}")
    print(f"user_id: {a.get('user_id')}")
    print(f"saved_at: {a.get('saved_at')}")
    print(f"base_url: {a.get('base_url')}")
    print(f"token: {'已配置' if a.get('token') else '缺失'}")
    backend, _ = _load_backend()
    print(f"backend: {backend}")
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
        print(f"pending_replies: {len(st.get('pending_replies') or [])}")
        print(f"codex_session: {st.get('codex_session')}")
    q = 0
    if RETRY_QUEUE_FILE.exists():
        q = len([l for l in RETRY_QUEUE_FILE.read_text(encoding="utf-8").splitlines() if l.strip()])
    print(f"retry_queue: {q}")
    if LOCK_FILE.exists():
        print(f"lock_pid: {LOCK_FILE.read_text(encoding='utf-8').strip()}")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "register":
        return register()
    if cmd == "status":
        return status()
    if not ACCOUNT_FILE.exists():
        print("未注册。先执行: python codex_weixin_bridge.py register")
        return 1
    Bridge().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
