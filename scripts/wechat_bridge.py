#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Codex 微信桥（专线版 v2 — 稳定性加固）— iLink Bot API ↔ Codex CLI

用户微信专线：微信 bot 只服务扫码绑定的那个用户，回复由本机 Agent CLI 生成。
按长期稳定运行的单一 bot 连接经验设计：单一 bot 稳定连接、无自动轮换：
  1. 熔断器 + 指数退避 + 抖动：瞬时错误不再线性傻等，连续失败跳闸 OPEN
  2. 会话过期(-14 / ret=-2+"unknown error")：
     - 暂停 10 分钟 → 重置熔断 → 自动重试（不再无限 300s 干等）
     - 发送时先去掉 context_token 重试一次
     - 自动生成新二维码并保存 PNG + 总线通告，等你扫码；不自动切换其他账号
     - 检测到 account.json 被重新注册覆盖后自动热加载新账号
  3. 发送失败持久化 retry_queue.jsonl，启动自动排空重发（防丢消息）
  4. 单实例锁（防双进程并发轮询同一 bot 被服务端踢）
  5. 轮询悬挂看门狗：同步轮询整段卡死超时后自杀，交给看护拉起
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
import queue
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
TURN_HANG_TIMEOUT = 90    # 常驻连接单轮无任何事件超过此秒数视为卡死，静默自愈重试
TURN_HANG_TIMEOUT_TASK = 300  # 任务：执行命令时可能长时间无文本事件，放宽到 300s
# 模型分工（2026-08-14 稳定版）：对话用快速档保证秒回，任务用质量档；可在 backend.json 覆盖
CHAT_MODEL = "deepseek-v4-flash"
CHAT_EFFORT = "medium"
CASUAL_EFFORT = "low"              # 简单消息（问候/确认）用低推理档，省 token（backend.json 可覆盖）
TASK_MODEL = "deepseek-v4-flash"
TASK_EFFORT = "medium"
ALIVE_NUDGE_S = 25                 # 代码级保活：单轮超 N 秒无任何输出，主动发“还在处理中”
ALIVE_NUDGE_INTERVAL = 60          # 保活提示最小间隔（秒），防刷屏
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
        home = os.path.expanduser("~")
        cands = []
        w = shutil.which("codex")
        if w:
            cands.append(w)
        cands += [
            _DEFAULT_CODEX_JS,
            os.path.join(home, ".codex", "bin", "codex"),        # 原生 CLI（macOS/Linux）
            os.path.join(home, ".codex", "bin", "codex.exe"),    # 原生 CLI（Windows）
            os.path.join(home, ".local", "bin", "codex"),
            os.path.join(apd, "npm", "node_modules", "@openai", "codex", "bin", "codex.js"),
            os.path.join(home, "node_modules", "@openai", "codex", "bin", "codex.js"),
        ]
        for cand in cands:
            if cand and os.path.exists(cand):
                codex_js = cand
                break
    if not claude_exe or not os.path.exists(claude_exe):
        apd = os.environ.get("APPDATA", "")
        home = os.path.expanduser("~")
        cands = []
        w = shutil.which("claude")
        if w:
            cands.append(w)
        cands += [
            _DEFAULT_CLAUDE_EXE,
            os.path.join(apd, "npm", "claude.cmd"),          # Windows: npm 全局 .cmd 壳
            os.path.join(apd, "npm", "claude"),
            os.path.join(home, ".local", "bin", "claude.exe"),  # 原生安装（Windows/macOS）
            os.path.join(home, ".local", "bin", "claude"),
            os.path.join(apd, "npm", "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.js"),
        ]
        for cand in cands:
            if cand and os.path.exists(cand):
                claude_exe = cand
                break
    if not work_dir:
        work_dir = str(HERE.parent / "wechat_work")  # 仓库根下 wechat_work（兑现注释承诺）
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    return node_exe, codex_js, claude_exe, work_dir


CODEX_NODE, CODEX_JS, CLAUDE_EXE, CODEX_WORK_DIR = _resolve_tool_paths()
CODEX_NATIVE = bool(CODEX_JS) and not CODEX_JS.lower().endswith(".js")  # 原生 codex 二进制 vs npm codex.js
INBOX_DIR = Path(CODEX_WORK_DIR) / "inbox"
MEMORY_FILE = Path(CODEX_WORK_DIR) / "wechat_memory.md"  # 每轮注入 + 对话后写回摘要
MEMORY_MAX_ENTRIES = 30

# ── 稳定性参数 ──────────────────────────────────────────────────────
CB_FAILURE_THRESHOLD = 6          # 连续失败次数 → 跳闸 OPEN
CB_OPEN_COOLDOWN = 60             # OPEN 后冷却秒数
CB_BASE_DELAY = 5.0               # 指数退避基数
CB_MAX_DELAY = 60.0               # 退避上限
CB_JITTER = 0.5                   # ±50% 抖动
SESSION_EXPIRED_PAUSE = 600       # 会话过期暂停秒数（10 分钟）
HANG_TIMEOUT_S = 180               # 轮询悬挂看门狗阈值（更快的卡死检测）
FLUSH_RETRY_INTERVAL = 30          # 重试队列排空周期
REBIND_ATTEMPT_COOLDOWN = 3600     # 自动重绑二维码触发冷却
EXPIRY_REBIND_THRESHOLD = 6        # 连续 -14 轮数（约 1 小时）后才判定真失效，触发重绑提醒
SEND_RATE_LIMIT = 8                # 微信侧约 10 条/分钟上限，留余量主动限流
SEND_RATE_WINDOW = 60              # 限流窗口秒数
MAX_MSG_LEN = 1000                 # 单条微信消息长度上限：长回复自动分段成多条短消息，微信阅读友好
POLL_TIMEOUT = 45                  # getupdates 长轮询默认超时秒数（跟随服务端 longpolling_timeout_ms 调整）

SYSTEM_HINT = ("你是{identity}，正在微信里和用户私聊。像日常聊天一样自然回复："
               "口语化、直接、一两句话（内容需要时可以稍长），不要markdown列表、"
               "不要项目符号、不要客套前缀。这是微信后台通道：除非用户明确要求，"
               "不要执行任何语音播报，也不要提起自己是后台进程。\n"
               "回复规矩：绝对不要一大段文字堆在一起——先说结论、再补关键细节，"
               "一条回复尽量控制在几条短消息内（总长 ~600 字内）；一行一个意思，"
               "需要多条时每条说一个要点，宁可分条也别堆成整段；"
               "能用一句话说清绝不多写，长内容只挑重点。用户的消息：\n")

PRIMER = (
    "【本体底稿】你是 {identity}，用户给你开的微信专线，这个微信 bot 只服务扫码绑定的那个用户。"
    "你和用户在微信私聊，消息直达你的本体会话，不走任何群。你跑在用户的电脑上。"
    "用户说话直接，不喜欢废话和客套。你是 AI 不是真人：做不到的事（发文件、发邮箱、打电话、"
    "线下操作）一律不许假装能做，直接说实话。你有完整工具权限，用户让干的活直接干，"
    "干完把结果简洁地告诉用户。\n\n"
    "你的模型、配置和代码由部署方统一维护。不要自行修改桥的配置、模型设置、数据库，"
    "也不要重启任何服务；收到这类请求（如'切换模型''改配置'）直接回复"
    "'模型和配置由部署方负责，我请用户转给它处理'，绝对不要动手，不要假装已改。\n\n"
    "干活不许闷着头：只要是在做事（不是纯聊天），动手前先一句话告诉用户你要做什么；"
    "过程中每完成一个关键步骤或节点，主动发一句进展（这一步做了什么、结果如何）；"
    "卡住或需要长时间等待时，主动说明当前状态；全部完成后给一句总结。"
    "宁可多报不可少报，绝不长时间无声无息。\n"
    "回复一律精简：微信里说话简短直接、先结论后细节，长内容拆成几条短消息分开发，"
    "绝不把一大段文字堆在一起。\n\n"
    "遇到复杂任务（大型开发、系统级改动、长流程工程）不要硬扛："
    "明确告诉用户'这个任务交给更强的桌面 Agent 更稳'，简要说明已做进度和关键上下文，由部署方转交。\n\n"
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
        with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
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


def _toast(title, text):
    """右下角 Windows Toast 通知（不阻塞、不影响任务）。"""
    if os.name != "nt":
        return  # 非 Windows 无 Toast，静默跳过
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden",
                          "-ExecutionPolicy", "Bypass", "-File",
                          _TOAST_PS1,
                          "-Title", title, "-Text", text],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log(f"Toast 提醒失败: {str(e)[:80]}")


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
    """判断 pid 是否仍为运行中的桥进程（防 PID 复用误判）。"""
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    try:
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            if not ok or exit_code.value != STILL_ACTIVE:
                return False
            buf = ctypes.create_unicode_buffer(512)
            n = ctypes.windll.psapi.GetProcessImageFileNameW(h, buf, 512)
            name = buf.value.lower() if n else ""
            return "pythonw.exe" in name or "python.exe" in name
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
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
    """/stop：打断正在跑的任务（常驻连接 interrupt + 兜底杀子进程）。"""
    _cancel_event.set()
    with _proc_lock:
        p = _active_codex_proc
    if p and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass
    try:
        _helper_send({"type": "interrupt"})
    except Exception:
        pass
    try:
        _claude_helper_send({"type": "interrupt"})
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


_APP_SERVER_PORT = 38123
_APP_SERVER_URL = f"ws://127.0.0.1:{_APP_SERVER_PORT}"
_HELPER_JS = str(Path(__file__).resolve().parent / "codex_ws_helper.mjs")
_CLAUDE_HELPER_PY = str(Path(__file__).resolve().parent / "claude_helper.py")
_TOAST_PS1 = str(Path(__file__).resolve().parent / "show_toast.ps1")
_app_server_proc = None
_helper_proc = None
_helper_in = None
_helper_out = None
_helper_q = queue.Queue()
_helper_reader = None
_helper_lock = threading.Lock()
_turn_queues = {}                 # req_id -> queue.Queue：事件按 turn 路由，防并发互抢饿死
_turn_queues_lock = threading.Lock()
_claude_proc = None
_claude_in = None
_claude_out = None
_claude_q = queue.Queue()
_claude_reader = None
_claude_lock = threading.Lock()
_claude_turn_queues = {}          # claude 常驻：req_id -> queue.Queue
_claude_turn_queues_lock = threading.Lock()
_state_lock = threading.Lock()
_active_turns = set()
_active_turns_lock = threading.Lock()


def _port_listening(port, host="127.0.0.1"):
    import socket
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except Exception:
        return False


def _ensure_app_server():
    """确保 Codex app-server 常驻服务在跑（常驻进程）。"""
    global _app_server_proc
    if _port_listening(_APP_SERVER_PORT):
        return True
    log("🔄 启动 Codex app-server 常驻服务…")
    if CODEX_NATIVE:
        cmd = [CODEX_JS, "app-server", "--listen", _APP_SERVER_URL]
    else:
        cmd = [CODEX_NODE, CODEX_JS, "app-server", "--listen", _APP_SERVER_URL]
    _app_server_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(20):
        time.sleep(1)
        if _port_listening(_APP_SERVER_PORT):
            return True
    return False


def _helper_restart():
    """卡死自愈：杀掉助手与 app-server，整体重拉（静默，不打扰用户）。"""
    global _helper_proc, _helper_in, _helper_out, _helper_q, _helper_reader, _app_server_proc
    with _helper_lock:
        if _helper_proc:
            try:
                _helper_proc.kill()
            except Exception:
                pass
            _helper_proc = None
            _helper_in = None
            _helper_out = None
            _helper_q = queue.Queue()
            with _turn_queues_lock:
                _turn_queues.clear()
            _helper_reader = None
        if _app_server_proc and _app_server_proc.poll() is None:
            try:
                _app_server_proc.kill()
            except Exception:
                pass
            _app_server_proc = None
    time.sleep(3)
    return _helper_ensure()


def _helper_start_reader():
    """常驻读线程：把助手 stdout 事件推入队列（消除阻塞 readline 导致超时失效的问题）。"""
    global _helper_reader

    def _read():
        for line in iter(_helper_out.readline, ""):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            rid = obj.get("id")
            if rid:
                with _turn_queues_lock:
                    q = _turn_queues.get(rid)
                if q is not None:
                    q.put(obj)
                    continue
            _helper_q.put(obj)  # ready/无 id 事件：进共享队列
        _helper_q.put({"type": "exit"})
        with _turn_queues_lock:
            qs = list(_turn_queues.values())
        for q in qs:
            q.put({"type": "exit"})

    _helper_reader = threading.Thread(target=_read, daemon=True)
    _helper_reader.start()


def _helper_ensure():
    """确保 WS 连接助手常驻（等待 ready）。"""
    global _helper_proc, _helper_in, _helper_out, _helper_q
    if _helper_proc and _helper_proc.poll() is None:
        return True
    if not _ensure_app_server():
        log("⚠️ app-server 启动失败")
        return False
    log("🔄 启动 Codex 常驻连接助手…")
    try:
        _helper_proc = subprocess.Popen([CODEX_NODE, _HELPER_JS],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                        errors="replace",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _helper_in = _helper_proc.stdin
        _helper_out = _helper_proc.stdout
        _helper_q = queue.Queue()
        with _turn_queues_lock:
            _turn_queues.clear()
    except Exception as e:
        log(f"助手启动失败: {str(e)[:120]}")
        return False
    _helper_start_reader()
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            obj = _helper_q.get(timeout=5)
        except queue.Empty:
            continue
        t = obj.get("type")
        if t == "ready":
            log("✅ Codex 常驻连接就绪")
            return True
        if t == "exit":
            log("⚠️ 助手进程退出")
            return False
        if t == "error":
            log(f"助手初始化错误: {obj.get('message')}")
            return False
    log("⚠️ 助手 ready 超时，已终止")
    try:
        _helper_proc.kill()
    except Exception:
        pass
    return False


def _helper_send(cmd):
    with _helper_lock:
        if not _helper_in:
            return False
        _helper_in.write(json.dumps(cmd, ensure_ascii=False) + "\n")
        _helper_in.flush()
        return True


# ───────────────────────── Claude Code 常驻连接（对等 codex_ws_helper） ─────────────────────────

def _claude_helper_start_reader():
    """常驻读线程：把 claude 助手 stdout 事件推入队列（按 req_id 路由）。"""
    global _claude_reader

    def _read():
        for line in iter(_claude_out.readline, ""):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            rid = obj.get("id")
            if rid:
                with _claude_turn_queues_lock:
                    q = _claude_turn_queues.get(rid)
                if q is not None:
                    q.put(obj)
                    continue
            _claude_q.put(obj)  # ready/无 id 事件：进共享队列
        _claude_q.put({"type": "exit"})
        with _claude_turn_queues_lock:
            qs = list(_claude_turn_queues.values())
        for q in qs:
            q.put({"type": "exit"})

    _claude_reader = threading.Thread(target=_read, daemon=True)
    _claude_reader.start()


def _kill_stray_claude_helpers():
    """清理残留的 claude_helper 进程（防孤儿堆积）。只清本助手，不动用户 claude.exe。"""
    if os.name != "nt":
        try:
            subprocess.run(["pkill", "-f", "claude_helper.py"], capture_output=True, timeout=10)
        except Exception:
            pass
        return
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and "
             "$_.CommandLine -match 'claude_helper.py' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def _claude_helper_ensure():
    """确保 Claude 常驻子进程就绪（等待 ready）。"""
    global _claude_proc, _claude_in, _claude_out, _claude_q
    if _claude_proc and _claude_proc.poll() is None:
        return True
    if not CLAUDE_EXE or not os.path.exists(CLAUDE_EXE):
        log("⚠️ claude 可执行文件未找到，跳过常驻助手")
        return False
    log("🔄 启动 Claude 常驻连接助手…")
    try:
        _kill_stray_claude_helpers()
        env = dict(os.environ)
        env["CLAUDE_WORK_DIR"] = str(CODEX_WORK_DIR)
        env["CLAUDE_CLI"] = CLAUDE_EXE
        _claude_proc = subprocess.Popen([sys.executable, "-u", _CLAUDE_HELPER_PY],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                        errors="replace", env=env,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _claude_in = _claude_proc.stdin
        _claude_out = _claude_proc.stdout
        _claude_q = queue.Queue()
        with _claude_turn_queues_lock:
            _claude_turn_queues.clear()
    except Exception as e:
        log(f"Claude 助手启动失败: {str(e)[:120]}")
        return False
    _claude_helper_start_reader()
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            obj = _claude_q.get(timeout=5)
        except queue.Empty:
            continue
        t = obj.get("type")
        if t == "ready":
            log("✅ Claude 常驻连接就绪")
            return True
        if t == "exit":
            log("⚠️ Claude 助手进程退出")
            return False
        if t == "error":
            log(f"Claude 助手初始化错误: {obj.get('message')}")
            return False
    log("⚠️ Claude 助手 ready 超时，已终止")
    try:
        _claude_proc.kill()
    except Exception:
        pass
    return False


def _claude_helper_restart():
    """卡死自愈：杀掉 claude 助手，整体重拉（静默）。"""
    global _claude_proc, _claude_in, _claude_out, _claude_q, _claude_reader
    with _claude_lock:
        if _claude_proc:
            try:
                _claude_proc.kill()
            except Exception:
                pass
            _claude_proc = None
            _claude_in = None
            _claude_out = None
            _claude_q = queue.Queue()
            with _claude_turn_queues_lock:
                _claude_turn_queues.clear()
            _claude_reader = None
    time.sleep(3)
    return _claude_helper_ensure()


def _claude_helper_send(cmd):
    with _claude_lock:
        if not _claude_in:
            return False
        _claude_in.write(json.dumps(cmd, ensure_ascii=False) + "\n")
        _claude_in.flush()
        return True


def _extract_files(reply):
    """从最终回复里取出【文件】标记行，返回 (清理后的文本, 文件路径列表)。"""
    files = [m.strip().strip('"').strip("'") for m in re.findall(r"【文件】\s*([^\r\n]+)", reply or "")]
    cleaned = re.sub(r"【文件】[^\r\n]*\r?\n?", "", reply or "").strip()
    return cleaned, files


def _cut_paragraph(text, max_len):
    """把一段文字按句子切成 ≤max_len 的语义块（中英文句号/问号/叹号/分号断句）。"""
    if len(text) <= max_len:
        return [text]
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
    chunks = []
    buf = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) > max_len:
            if buf:
                chunks.append(buf)
                buf = ""
            for j in range(0, len(p), max_len):
                chunks.append(p[j:j + max_len])
        elif len(buf) + len(p) + 1 > max_len:
            chunks.append(buf)
            buf = p
        else:
            buf = (buf + "\n" + p) if buf else p
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c and c.strip()]


def _split_weixin_text(text, max_len=MAX_MSG_LEN):
    """微信友好分片：段落/句子感知，代码块整体保留；长回复自动切成多条短消息。"""
    text = str(text or "")
    if not text:
        return []
    if len(text) <= max_len and "\n" not in text:
        return [text]
    # 1. 先把代码块和普通行区分开
    lines = text.split("\n")
    units = []  # (text, is_code)
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip().startswith("```"):
            block = [lines[i]]
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            if i < n:
                block.append(lines[i])
                i += 1
            units.append(("\n".join(block), True))
        else:
            units.append((lines[i], False))
            i += 1
    # 2. 普通文本按空行分段（一段一个语义块），段落超长按句子切；代码块整体保留
    chunks = []
    para = []

    def flush_para():
        nonlocal para
        if para:
            for seg in _cut_paragraph("\n".join(para), max_len):
                chunks.append(seg)
            para = []

    for u, is_code in units:
        if is_code:
            flush_para()
            if len(u) > max_len:
                open_line, rest = u.split("\n", 1)
                close = ""
                if rest.rstrip().endswith("```"):
                    rest = rest.rstrip()[:-3].rstrip("\n")
                    close = "```"
                chunks.append(open_line)
                for j in range(0, len(rest), max_len - 8):
                    chunks.append(rest[j:j + max_len - 8])
                if close:
                    chunks.append(close)
            else:
                chunks.append(u)
        elif u.strip() == "":
            flush_para()
        else:
            para.append(u)
    flush_para()
    return [c for c in chunks if c and c.strip()]


def _load_memory():
    """读取微信桥记忆文件（每轮注入，不依赖线程）。"""
    try:
        if MEMORY_FILE.exists():
            return MEMORY_FILE.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        log(f"记忆读取失败: {str(e)[:80]}")
    return ""


def _append_memory(entry):
    """对话后追加一行摘要到记忆文件（原子写 + 锁，超量滚动裁剪）。"""
    try:
        with _state_lock:
            header_lt = "## 长期事实"
            header_chat = "## 最近对话"
            text = ""
            if MEMORY_FILE.exists():
                text = MEMORY_FILE.read_text(encoding="utf-8", errors="replace")
            # 拆分：长期事实段保留原样，最近对话段滚动追加
            if header_chat in text:
                before, _, after = text.partition(header_chat)
                lines = [l for l in after.splitlines() if l.strip()][-MEMORY_MAX_ENTRIES:]
                new_after = "\n" + "\n".join(lines) + ("\n" if lines else "") + entry + "\n"
                text = before + header_chat + new_after
            else:
                text = (text.rstrip() + "\n\n" if text.strip() else "") + header_chat + "\n" + entry + "\n"
            tmp = MEMORY_FILE.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, MEMORY_FILE)
    except Exception as e:
        log(f"记忆写入失败: {str(e)[:80]}")


def codex_reply(prompt, session_id=None, on_progress=None, model=None, effort=None, base=None, timeout=None,
                preempt_evt=None, preempt_ctx=None):
    """通过常驻 app-server 连接生成回复（不每条消息重启 Codex）。"""
    global _helper_proc
    hang_timeout = timeout or TURN_HANG_TIMEOUT
    if not _probe_codex_provider():
        log("⚠️ Codex 模型服务不可达（本地代理未启动），跳过本轮")
        return None, session_id
    for attempt in range(3):
        if preempt_evt is not None and preempt_evt.is_set():
            return None, session_id
        _cancel_event.clear()
        with _helper_lock:
            ok = _helper_ensure()
        if not ok:
            log("⚠️ Codex 常驻服务不可用")
            return None, session_id
        try:
            req_id = uuid.uuid4().hex[:8]
            if preempt_ctx is not None:
                preempt_ctx["req_id"] = req_id
            my_q = queue.Queue()
            with _turn_queues_lock:
                _turn_queues[req_id] = my_q
            try:
                payload = {"type": "turn", "id": req_id, "threadId": session_id,
                           "prompt": prompt, "cwd": CODEX_WORK_DIR}
                if model:
                    payload["model"] = model
                if effort:
                    payload["effort"] = effort
                if base:
                    payload["base"] = base
                if not _helper_send(payload):
                    raise RuntimeError("助手未连接")
                last_event = time.time()
                last_alive = time.time()
                while True:
                    if _cancel_event.is_set():
                        return None, session_id
                    if preempt_evt is not None and preempt_evt.is_set():
                        return None, session_id
                    now = time.time()
                    if now - last_event > hang_timeout:
                        try:
                            _helper_send({"type": "interrupt"})
                        except Exception:
                            pass
                        raise RuntimeError("HANG: 单轮无事件超时")
                    if now - last_alive >= ALIVE_NUDGE_INTERVAL and now - last_event >= ALIVE_NUDGE_S:
                        try:
                            if on_progress:
                                on_progress("⏳ 还在处理中…")
                        except Exception:
                            pass
                        last_alive = now
                    try:
                        obj = my_q.get(timeout=5)
                    except queue.Empty:
                        continue
                    last_event = time.time()
                    t = obj.get("type")
                    if t == "exit":
                        raise RuntimeError("助手退出")
                    if t == "progress" and on_progress:
                        try:
                            on_progress(obj.get("text") or "")
                        except Exception:
                            pass
                    elif t == "result":
                        text = (obj.get("text") or "").strip()
                        reply = _clean_final(text, set())
                        return reply or None, obj.get("threadId") or session_id
                    elif t == "error":
                        raise RuntimeError(obj.get("message") or "turn error")
            finally:
                with _turn_queues_lock:
                    _turn_queues.pop(req_id, None)
        except Exception as e:
            msg = str(e)
            log(f"⚠️ Codex 常驻连接异常(第{attempt+1}次): {msg[:120]}")
            if "HANG" in msg:
                if attempt >= 2:
                    log("🧹 多次卡死：放弃旧线程并重启常驻连接（自愈）")
                    session_id = None  # 卡死的旧线程不再续接，改用新线程
                    try:
                        _helper_restart()
                    except Exception as ex:
                        log(f"重启常驻连接异常: {str(ex)[:80]}")
                else:
                    log("🔧 单轮无事件超时：中断重试（保留线程上下文）")
                time.sleep(3)
            elif "thread not found" in msg:
                log("🧹 旧线程失效，自动开新线程重试")
                session_id = None
            else:
                with _helper_lock:
                    if _helper_proc:
                        try:
                            _helper_proc.kill()
                        except Exception:
                            pass
                        _helper_proc = None
                time.sleep(3)
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


def _claude_reply_persistent(prompt, session_id=None, on_progress=None, base=None, timeout=None):
    """通过常驻 claude 子进程生成回复（不每条消息冷启动）。"""
    global _claude_proc
    hang_timeout = timeout or TURN_HANG_TIMEOUT
    _cancel_event.clear()
    for attempt in range(3):
        if _cancel_event.is_set():
            return None, session_id
        with _claude_lock:
            ok = _claude_helper_ensure()
        if not ok:
            log("⚠️ Claude 常驻服务不可用")
            return None, session_id
        try:
            req_id = uuid.uuid4().hex[:8]
            my_q = queue.Queue()
            with _claude_turn_queues_lock:
                _claude_turn_queues[req_id] = my_q
            try:
                payload = {"type": "turn", "id": req_id, "session": session_id,
                           "prompt": prompt}
                if base:
                    payload["base"] = base
                if not _claude_helper_send(payload):
                    raise RuntimeError("助手未连接")
                last_event = time.time()
                last_alive = time.time()
                while True:
                    if _cancel_event.is_set():
                        return None, session_id
                    now = time.time()
                    if now - last_event > hang_timeout:
                        try:
                            _claude_helper_send({"type": "interrupt", "reqId": req_id})
                        except Exception:
                            pass
                        raise RuntimeError("HANG: 单轮无事件超时")
                    if now - last_alive >= ALIVE_NUDGE_INTERVAL and now - last_event >= ALIVE_NUDGE_S:
                        try:
                            if on_progress:
                                on_progress("⏳ 还在处理中…")
                        except Exception:
                            pass
                        last_alive = now
                    try:
                        obj = my_q.get(timeout=5)
                    except queue.Empty:
                        continue
                    last_event = time.time()
                    t = obj.get("type")
                    if t == "exit":
                        raise RuntimeError("助手退出")
                    if t == "progress" and on_progress:
                        try:
                            on_progress(obj.get("text") or "")
                        except Exception:
                            pass
                    elif t == "result":
                        text = (obj.get("text") or "").strip()
                        reply = _clean_final(text, set())
                        return reply or None, obj.get("session") or session_id
                    elif t == "error":
                        raise RuntimeError(obj.get("message") or "turn error")
            finally:
                with _claude_turn_queues_lock:
                    _claude_turn_queues.pop(req_id, None)
        except Exception as e:
            msg = str(e)
            log(f"⚠️ Claude 常驻连接异常(第{attempt+1}次): {msg[:120]}")
            if "HANG" in msg:
                if attempt >= 2:
                    log("🧹 多次卡死：重启 Claude 常驻连接（自愈）")
                    session_id = None
                    try:
                        _claude_helper_restart()
                    except Exception as ex:
                        log(f"重启 Claude 常驻连接异常: {str(ex)[:80]}")
                else:
                    log("🔧 单轮无事件超时：中断重试（保留会话上下文）")
                time.sleep(3)
            else:
                with _claude_lock:
                    if _claude_proc:
                        try:
                            _claude_proc.kill()
                        except Exception:
                            pass
                        _claude_proc = None
                time.sleep(3)
    return None, session_id


def claude_reply(prompt, session_id=None, on_progress=None, base=None, timeout=None):
    """Claude Code 回复：优先常驻子进程（不冷启动），失败自动回退 spawn 模式。"""
    reply, sid = _claude_reply_persistent(prompt, session_id, on_progress,
                                          base=base, timeout=timeout)
    if reply is not None:
        return reply, sid
    log("↩️ Claude 常驻通道失败，回退 spawn 模式")
    return _claude_reply_spawn(prompt, session_id, on_progress, base=base)


def _claude_reply_spawn(prompt, session_id=None, on_progress=None, base=None):
    """（回退路径）调本机 Claude Code CLI（stream-json）生成回复。支持续接、进度转发与 /stop。"""
    global _active_codex_proc
    exe = CLAUDE_EXE
    if not exe or not os.path.exists(exe):
        log("⚠️ claude 可执行文件未找到：请安装 Claude Code（npm i -g @anthropic-ai/claude-code）或配置 backend.json 的 claude_exe")
        return None, session_id
    cmd_head = [exe]
    if exe.lower().endswith(".js"):
        cmd_head = [CODEX_NODE, exe]  # npm 包 bin 是 js 入口，需要 node 启动
    full_prompt = prompt
    if base and not session_id:
        full_prompt = base.strip() + "\n\n" + prompt
    cmd = cmd_head + ["-p", full_prompt, "--output-format", "stream-json",
            "--include-partial-messages", "--verbose", "--dangerously-skip-permissions"]
    if session_id:
        cmd += ["--resume", session_id]
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
            result = {"sid": session_id, "reply": None}
            sent_markers = set()
            buf = ""
            last_output = [time.time()]
            last_alive = [time.time()]

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
                    last_output[0] = time.time()
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
                now = time.time()
                if now - last_output[0] >= ALIVE_NUDGE_S and now - last_alive[0] >= ALIVE_NUDGE_INTERVAL:
                    try:
                        if on_progress:
                            on_progress("⏳ 还在处理中…")
                    except Exception:
                        pass
                    last_alive[0] = now
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
    if cmd:
        cmd[0] = shutil.which(cmd[0]) or cmd[0]   # Windows: 裸命令自动解析（.cmd/.exe）
        if cmd[0].lower().endswith(".js"):
            cmd.insert(0, CODEX_NODE)
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
            last_output = [time.time()]
            last_alive = [time.time()]

            def _scan(line, is_err):
                last_output[0] = time.time()
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
                now = time.time()
                if now - last_output[0] >= ALIVE_NUDGE_S and now - last_alive[0] >= ALIVE_NUDGE_INTERVAL:
                    try:
                        if on_progress:
                            on_progress("⏳ 还在处理中…")
                    except Exception:
                        pass
                    last_alive[0] = now
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


def agent_reply(backend, prompt, session_id=None, on_progress=None, cfg=None,
                model=None, effort=None, base=None, timeout=None,
                preempt_evt=None, preempt_ctx=None):
    """按后端分派回复生成。"""
    if backend == "claude":
        return claude_reply(prompt, session_id, on_progress, base=base, timeout=timeout)
    if backend == "generic":
        return generic_reply(prompt, session_id, on_progress, (cfg or {}).get("generic") or {})
    return codex_reply(prompt, session_id, on_progress, model=model, effort=effort,
                       base=base, timeout=timeout, preempt_evt=preempt_evt, preempt_ctx=preempt_ctx)


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
            try:
                import shutil
                home = os.environ.get("USERPROFILE", "")
                desktop = Path(home) / "Desktop"
                if not desktop.exists():
                    desktop = Path(home) / "OneDrive" / "Desktop"
                if desktop.exists():
                    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                    dest = desktop / f"微信重绑二维码-{ts}.png"
                    shutil.copy2(QR_PNG, dest)
                    log(f"二维码已复制到桌面: {dest}")
            except Exception as e:
                log(f"桌面副本失败: {str(e)[:80]}")
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
        if not (self.account.get("user_id") or ""):
            log("⚠️ 账号缺少 user_id：已进入拒收模式（fail-closed），请重新扫码注册")
        self.state = {"sync_buf": "", "context_token": "", "boss_chat": "",
                      "codex_session": None, "session_account": "", "seen": [],
                      "seen_fps": [], "sessions": []}
        if STATE_FILE.exists():
            try:
                self.state.update(json.loads(STATE_FILE.read_text(encoding="utf-8-sig")))
            except Exception:
                pass
        self.reply_q = deque()
        self._q_cond = threading.Condition()
        self.task_q = deque()          # 后台任务队列（FIFO 顺序执行）
        self._task_cond = threading.Condition()
        self._task_preempt = threading.Event()   # 聊天到达时置位：任务让位
        self._chat_active = 0                    # 当前正在处理的聊天数
        self._running_task = {"active": False, "sid": None, "desc": "", "req_id": None}
        self._resume_task = None                 # 被打断待恢复的任务（队首优先）
        self._send_ts = []
        self._send_lock = threading.Lock()
        for _t in self.state.pop("pending_replies", []) or []:
            if _t:
                self.reply_q.appendleft(_t)
        for leftover in (self.state.pop("processing", None) or []):
            if leftover:
                log(f"♻️ 上次中断时正在处理的消息补回队列: {leftover[:40]}")
                self.reply_q.appendleft(leftover)
        for _t in self.state.pop("task_queue", []) or []:
            if _t:
                self.task_q.append(_t)
        self.cb = _CircuitBreaker(self.account.get("account_id", "bridge"))
        self._last_poll_ts = time.time()
        self._retry_queue_flush_ts = 0.0
        self._expiry_pause_until = 0.0
        self._rebind_attempted_at = 0.0
        self._expiry_streak = 0
        self._poll_timeout = POLL_TIMEOUT
        self._account_mtime = ACCOUNT_FILE.stat().st_mtime if ACCOUNT_FILE.exists() else 0.0
        self.backend, self.backend_cfg = _load_backend()
        # 当前主账号进池，作为后续备用
        self._persist_account_state(self.account, self.state)
        self._cleanup_stale_session()

    def save_state(self):
        with _state_lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.state["seen"] = self.state["seen"][-200:]
            try:
                self.state["pending_replies"] = list(self.reply_q)
            except Exception:
                pass
            try:
                self.state["task_queue"] = list(self.task_q)
            except Exception:
                pass
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_FILE)
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
                            token=self.account["token"], timeout=self._poll_timeout)
            except Exception as e:
                wait = self.cb.on_failure()
                log(f"⚠️ getupdates 异常(cb={self.cb.state} failures={self.cb.consecutive_failures} "
                    f"backoff={wait:.1f}s): {str(e)[:100]}")
                time.sleep(wait)
                continue

            ret = resp.get("ret", 0)
            errcode = resp.get("errcode", 0)
            suggested = resp.get("longpolling_timeout_ms")
            if isinstance(suggested, int) and 10 <= suggested <= 120:
                self._poll_timeout = suggested
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
            self._expiry_streak = 0
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
        self._expiry_streak += 1
        if self._expiry_streak < EXPIRY_REBIND_THRESHOLD:
            log(f"会话过期第 {self._expiry_streak}/{EXPIRY_REBIND_THRESHOLD} 轮，静默重试中（先不出码）")
            return
        if time.time() - self._rebind_attempted_at > REBIND_ATTEMPT_COOLDOWN:
            self._rebind_attempted_at = time.time()
            bus_notice("@用户 微信专线持续过期，已自动生成新二维码，请尽快扫码（见桌面）")
            _toast("微信通道需要重新扫码",
                   "会话持续过期，二维码已生成到桌面（微信重绑二维码-时间.png），扫码即可恢复。")
            threading.Thread(target=self._auto_rebind, daemon=True).start()
        else:
            log(f"重绑提醒冷却期内（上次 {int(time.time() - self._rebind_attempted_at)}s 前），继续静默重试")

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
        try:
            self.send_weixin("微信通道已重新绑定，恢复使用。")
        except Exception:
            pass

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
        allowed = self.account.get("user_id", "") or ""
        if not from_user or from_user != allowed:
            log(f"⛔ 拦截未授权发信人: {from_user!r}（仅接受绑定用户 {allowed}）")
            return
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
        fp = hashlib.md5((from_user + "|" + text).encode("utf-8")).hexdigest()
        seen_fps = self.state.setdefault("seen_fps", [])
        if fp in seen_fps:
            log("♻️ 内容指纹去重，跳过重复消息")
            return
        seen_fps.append(fp)
        self.state["seen_fps"] = seen_fps[-50:]
        norm = _norm_msg(text)
        raw = text.strip()
        if norm in STOP_PHRASES:
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
        if level == "complex":
            # 复杂任务：进后台任务队列，FIFO 顺序执行，不占聊天通道
            with self._task_cond:
                self.task_q.append(text)
                self._task_cond.notify()
            self.send_typing()
            self.send_weixin("收到，任务已入队，会按顺序执行并实时报进度。")
            self.save_state()
            return
        # 对话消息：新消息优先；若后台任务在跑，先打断它，任务处理完聊天后自动恢复
        if self._running_task.get("active"):
            self._preempt_task()
        with self._q_cond:
            self.reply_q.appendleft(text)  # 新消息优先
            self._q_cond.notify()
        self.save_state()

    def _get_typing_ticket(self):
        entry = self.state.get("typing_ticket")
        if entry:
            if isinstance(entry, dict):
                if time.time() - entry.get("ts", 0) < 600:
                    return entry.get("ticket", "")
            else:
                return entry  # 旧格式字符串兼容
        to = self.state["boss_chat"] or self.account.get("user_id", "")
        try:
            resp = _req(f"{self.account['base_url']}/{EP_GET_CONFIG}",
                        {"ilink_user_id": to, "context_token": self.state.get("context_token", "")},
                        token=self.account["token"], timeout=10)
            t = str(resp.get("typing_ticket") or "")
            if t:
                self.state["typing_ticket"] = {"ticket": t, "ts": time.time()}
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

    # ── 回复工作线程池：Agent CLI → 微信（新消息优先，多消息并发） ──
    def _reply_worker_loop(self):
        while True:
            with self._q_cond:
                while not self.reply_q:
                    self._q_cond.wait()
                text = self.reply_q.popleft()
            with _active_turns_lock:
                self._chat_active += 1
            try:
                self._process_one(text)
            except Exception as e:
                log(f"⚠️ 回复线程异常: {e}")
            finally:
                with _active_turns_lock:
                    self._chat_active -= 1

    def _process_one(self, text):
        with _active_turns_lock:
            parallel = self._chat_active > 1
            token = uuid.uuid4().hex
            _active_turns.add(token)
        self.state.setdefault("processing", []).append(text)
        self.save_state()
        try:
            level = classify_message(text)
            if level == "complex":
                # 兜底：复杂任务一律转后台任务队列（FIFO），不占聊天通道
                with self._task_cond:
                    self.task_q.append(text)
                    self._task_cond.notify()
                self.save_state()
                return
            sid = self.state.get("codex_session")
            if parallel:
                sid = None  # 并行模式：新开线程独立处理，不占用主会话
            identity = BACKEND_IDENTITY.get(self.backend, "Codex")
            mem = _load_memory()
            memory_block = ("【微信桥记忆】\n" + mem + "\n\n") if mem else ""
            prompt = memory_block + SYSTEM_HINT.format(identity=identity) + text
            base_prompt = PRIMER.format(identity=identity) if not sid else ""
            on_progress = self.send_weixin
            cfg = self.backend_cfg or {}
            chat_model = cfg.get("chat_model") or CHAT_MODEL
            chat_effort = cfg.get("chat_effort") or CHAT_EFFORT
            casual_effort = cfg.get("casual_effort") or CASUAL_EFFORT
            # 简单消息降推理档省 token；普通对话用 chat 档；复杂任务由 _process_task 用 task 档处理
            turn_model, turn_effort = (chat_model, casual_effort if level == "casual" else chat_effort)
            turn_timeout = TURN_HANG_TIMEOUT_TASK if level == "complex" else TURN_HANG_TIMEOUT
            reply, new_sid = agent_reply(self.backend, prompt, sid, on_progress=on_progress,
                                         cfg=self.backend_cfg, model=turn_model, effort=turn_effort,
                                         base=base_prompt or None, timeout=turn_timeout)
            if not parallel and new_sid:
                self.state["codex_session"] = new_sid
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
                if not parallel:
                    self._record_session(new_sid, text)
                    _append_memory(f"- {datetime.now():%Y-%m-%d %H:%M} | 用户：{text[:60]} → 桥：{reply[:80]}")
                self.save_state()
            elif _cancel_event.is_set():
                log("⏹ 任务被 /stop 停止")
                _cancel_event.clear()
            else:
                self.send_weixin("（暂时连不上模型服务，稍后再问我一次）")
        except Exception as e:
            log(f"⚠️ 回复线程异常: {e}")
        finally:
            with _active_turns_lock:
                _active_turns.discard(token)
            try:
                self.state["processing"].remove(text)
            except Exception:
                pass
            self.save_state()

    def _task_worker_loop(self):
        """后台任务线程：FIFO 顺序执行；被聊天打断后自动恢复。"""
        while True:
            with self._task_cond:
                while not self.task_q:
                    self._task_cond.wait()
                text = self.task_q.popleft()
            try:
                self._process_task(text)
            except Exception as e:
                log(f"⚠️ 任务线程异常: {e}")

    def _process_task(self, text):
        """后台执行复杂任务；聊天打断时保存恢复标记，聊天结束后自动续做。"""
        preempt_evt = self._task_preempt
        preempt_ctx = self._running_task   # codex_reply 会把当前 turn 的 req_id 实时写进来
        resume_sid = None
        if self._resume_task and self._resume_task.get("desc") == text:
            resume_sid = self._resume_task.pop("sid", None)
            self._resume_task = None
        with _active_turns_lock:
            token = uuid.uuid4().hex
            _active_turns.add(token)
        self.state.setdefault("processing", []).append(text)
        self.save_state()
        try:
            identity = BACKEND_IDENTITY.get(self.backend, "Codex")
            mem = _load_memory()
            memory_block = ("【微信桥记忆】\n" + mem + "\n\n") if mem else ""
            if resume_sid:
                prompt = ("继续执行刚才被打断的任务，从断点接着做，完成后按协议汇报。任务内容：\n" + text)
                base_prompt = ""
                sid = resume_sid
            else:
                prompt = text
                base_prompt = PRIMER.format(identity=identity)
                sid = None
            if self.backend == "codex":
                prompt = memory_block + SYSTEM_HINT.format(identity=identity) + prompt
            else:
                prompt = memory_block + prompt
            prompt += TASK_PROTOCOL
            self._running_task.update({"sid": sid, "desc": text, "active": True, "req_id": None})
            cfg = self.backend_cfg or {}
            task_model = cfg.get("task_model") or TASK_MODEL
            task_effort = cfg.get("task_effort") or TASK_EFFORT
            chat_model = cfg.get("chat_model") or CHAT_MODEL
            chat_effort = cfg.get("chat_effort") or CHAT_EFFORT
            reply, new_sid = agent_reply(self.backend, prompt, sid, on_progress=self.send_weixin,
                                         cfg=self.backend_cfg, model=task_model, effort=task_effort,
                                         base=base_prompt or None, timeout=TURN_HANG_TIMEOUT_TASK,
                                         preempt_evt=preempt_evt, preempt_ctx=preempt_ctx)
            if not reply and not preempt_evt.is_set() and not _cancel_event.is_set():
                # task 档失败：用 chat 档重试一次（配置不同即降档；相同也是多一次瞬时错误兜底）
                log("↩️ task 档失败，用 chat 档重试一次")
                reply, new_sid = agent_reply(self.backend, prompt, sid, on_progress=self.send_weixin,
                                             cfg=self.backend_cfg, model=chat_model, effort=chat_effort,
                                             base=base_prompt or None, timeout=TURN_HANG_TIMEOUT_TASK,
                                             preempt_evt=preempt_evt, preempt_ctx=preempt_ctx)
            self._running_task.update({"active": False, "sid": new_sid or sid, "req_id": None})
            if preempt_evt.is_set():
                # 被打断：保存恢复标记，等聊天清空后放回队首自动续做
                preempt_evt.clear()
                self._resume_task = {"sid": new_sid or sid, "desc": text}
                log(f"⏸️ 任务被新消息打断，聊天结束后自动恢复: {text[:30]}")
                deadline = time.time() + 180
                while (self.reply_q or self._chat_active > 0) and time.time() < deadline:
                    time.sleep(0.5)
                with self._task_cond:
                    self.task_q.appendleft(text)
                    self._task_cond.notify()
                return
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
                self._record_session(new_sid or sid, text)
                _append_memory(f"- {datetime.now():%Y-%m-%d %H:%M} | 任务：{text[:60]} → 结果：{reply[:80]}")
            else:
                self.send_weixin("（任务执行失败，稍后我重新试一次）")
        except Exception as e:
            log(f"⚠️ 任务处理异常: {e}")
        finally:
            with _active_turns_lock:
                _active_turns.discard(token)
            try:
                self.state["processing"].remove(text)
            except Exception:
                pass
            self.save_state()

    def _preempt_task(self):
        """新消息优先：打断当前后台任务（按 reqId 定向中断，任务自动恢复）。仅 codex 后端支持。"""
        if self.backend != "codex":
            return  # claude/generic 无法定向打断：任务继续后台跑，聊天并行处理
        with self._task_cond:
            if not self._running_task.get("active"):
                return
            self._task_preempt.set()
        req = self._running_task.get("req_id")
        try:
            if req:
                _helper_send({"type": "interrupt", "reqId": req})
            else:
                _helper_send({"type": "interrupt"})
        except Exception:
            pass

    def _record_session(self, sid, text):
        if not sid:
            return
        sessions = self.state.setdefault("sessions", [])
        sessions = [s for s in sessions if s.get("sid") != sid]
        sessions.append({"sid": sid, "ts": datetime.now().isoformat(), "desc": text[:40]})
        self.state["sessions"] = sessions[-10:]

    def _post_message(self, item_list, to, ctx, timeout=20):
        self._throttle_send()
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

    def _throttle_send(self):
        """微信发送限流：窗口内最多 SEND_RATE_LIMIT 条，超出则等待（防服务端限流丢消息）。"""
        wait = 0.0
        with self._send_lock:
            now = time.time()
            cutoff = now - SEND_RATE_WINDOW
            self._send_ts = [t for t in self._send_ts if t > cutoff]
            if len(self._send_ts) >= SEND_RATE_LIMIT:
                wait = self._send_ts[0] + SEND_RATE_WINDOW - now
            if wait <= 0:
                self._send_ts.append(now)
        if wait > 0:
            time.sleep(wait)  # 锁外等待：不阻塞其他发送线程
            with self._send_lock:
                now = time.time()
                self._send_ts = [t for t in self._send_ts if t > now - SEND_RATE_WINDOW]
                self._send_ts.append(now)

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
        for part in _split_weixin_text(text):
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
        if not RETRY_QUEUE_FILE.exists():
            return
        try:
            lines = [l for l in RETRY_QUEUE_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        except Exception:
            return
        if not lines:
            return
        log(f"🔄 排空发送重试队列（{len(lines)} 条）…")
        remaining = []
        for line in lines:
            try:
                entry = json.loads(line)
            except Exception:
                remaining.append(line)
                continue
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
                remaining.append(line)
                continue
            log(f"📤 补发成功: {part[:40]}")
        try:
            if remaining:
                tmp = RETRY_QUEUE_FILE.with_suffix(".tmp")
                tmp.write_text("\n".join(remaining) + "\n", encoding="utf-8")
                os.replace(tmp, RETRY_QUEUE_FILE)
            else:
                RETRY_QUEUE_FILE.unlink(missing_ok=True)
        except Exception as e:
            log(f"重试队列写回失败: {e}")

    # ── 轮询悬挂看门狗：防同步轮询整段卡死 ──
    def watchdog(self):
        while True:
            time.sleep(30)
            if time.time() < self._expiry_pause_until:
                continue
            if time.time() - self._last_poll_ts > HANG_TIMEOUT_S:
                log(f"❌ 轮询看门狗：超过 {HANG_TIMEOUT_S}s 无轮询心跳（疑似悬挂），自杀重启")
                os._exit(1)
            # 常驻连接健康：Codex 后端确保 app-server/助手在跑（自动拉起，不重启整桥）
            if self.backend == "codex":
                try:
                    with _helper_lock:
                        if _helper_proc is None or _helper_proc.poll() is not None:
                            log("🔄 看门狗：常驻助手不在，重新拉起")
                            _helper_ensure()
                        elif not _port_listening(_APP_SERVER_PORT):
                            log("🔄 看门狗：app-server 不在，重新拉起")
                            _helper_ensure()
                except Exception as e:
                    log(f"看门狗助手检查异常: {str(e)[:80]}")

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
        _helper_ensure()  # 预热 Codex 常驻连接（常驻进程）
        t1 = threading.Thread(target=lambda: self._guarded("ilink", self.ilink_loop), daemon=True)
        t2a = threading.Thread(target=lambda: self._guarded("reply", self._reply_worker_loop), daemon=True)
        t2b = threading.Thread(target=lambda: self._guarded("reply", self._reply_worker_loop), daemon=True)
        t2c = threading.Thread(target=lambda: self._guarded("task", self._task_worker_loop), daemon=True)
        t3 = threading.Thread(target=lambda: self._guarded("watchdog", self.watchdog), daemon=True)
        t1.start()
        t2a.start()
        t2b.start()
        t2c.start()
        t3.start()
        while True:
            time.sleep(60)
            if not t1.is_alive() or not t2a.is_alive() or not t2b.is_alive() or not t2c.is_alive():
                log("⚠️ 子线程退出，重启桥进程")
                os._exit(1)


def status():
    if not ACCOUNT_FILE.exists():
        print("未注册。先执行: python wechat_bridge.py register")
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
        print("未注册。先执行: python wechat_bridge.py register")
        return 1
    Bridge().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
