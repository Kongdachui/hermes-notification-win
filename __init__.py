"""
hermes-notification-win — Windows/WSL 版「回复完成」通知插件
=============================================================
Hermes 在 WSL 里跑，回复完成时在 Windows 桌面弹原生 toast（横幅 + 声音）。

两种投递方式（config.json 的 mode）：
  - "daemon"（默认）：把任务写进 spool/outbox，由 Windows 侧常驻守护进程
    （%LOCALAPPDATA%\\HermesNotify\\toast_daemon.py，基于 win11toast）弹带输入框的 toast。
    用户在通知里打的字写回 spool/inbox，本插件轮询后注入会话 —— 等于在通知里直接回复。
  - "powershell"：旧路径，powershell.exe + WinRT toast，纯展示，无输入框。
  守护进程没跑（心跳超时）时自动回落到 powershell。

点击通知正文 → 打开本轮回复的完整页面（守护进程的 http://127.0.0.1:<http_port>/turn/<id>），
页面底部也能直接回复，回复同样注入会话。

注入回复需要 Hermes 配置显式放行：
  plugins.entries.hermes-notification-win.allow_gateway_injection: true

Config (可选) ~/.hermes/plugins/hermes-notification-win/config.json:
{
    "enabled": true,
    "mode": "daemon",              // daemon | powershell
    "title": "Hermes ✅",
    "sound": true,
    "preview_length": 80,
    "min_response_length": 10,
    "notify_platforms": ["tui", "cli", "terminal", ""],
    "enable_reply": true,          // 通知里带输入框（daemon 模式）
    "inject_reply": true,          // 把收到的回复注入会话
    "spool_dir": "",               // 留空 = %LOCALAPPDATA%\\HermesNotify\\spool
    "http_port": 9117,
    "reply_poll_ms": 1000
}

移植自 itgoyo/hermes-notification (macOS 版, MIT)：保留其插件骨架与配置项。
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults & constants
# ---------------------------------------------------------------------------

DEFAULTS = {
    "enabled": True,
    "mode": "daemon",
    "title": "Hermes ✅",
    "sound": True,
    "preview_length": 80,
    "min_response_length": 10,
    "notify_platforms": ["tui", "cli", "terminal", ""],
    "enable_reply": True,
    "inject_reply": True,
    "enable_approval": True,
    "spool_dir": "",
    "http_port": 9117,
    "reply_poll_ms": 1000,
}

# 通知发送者身份（Windows 自带注册的 PowerShell AUMID；仅 powershell 回退路径使用）
_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"

_WIN_APP_DIR = "HermesNotify"  # under %LOCALAPPDATA%
_PS1_NAME = "notify.ps1"

_ctx = None                 # PluginContext（register 时保存）
_poller_started = False
_inject_warned = False

# 纯 ASCII 的 PowerShell 脚本：参数全部走 base64，规避 PS 5.1 的文件编码问题。
_PS1_CONTENT = r'''param(
    [string]$TitleB64 = "",
    [string]$MessageB64 = "",
    [string]$Url = "",
    [string]$Sound = "default"
)

$ErrorActionPreference = "Stop"

function Decode-B64([string]$s) {
    if ([string]::IsNullOrEmpty($s)) { return "" }
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($s))
}

function Xml-Escape([string]$t) {
    if ($null -eq $t) { return "" }
    $t = $t.Replace("&", "&amp;")
    $t = $t.Replace("<", "&lt;")
    $t = $t.Replace(">", "&gt;")
    return $t
}

$title   = Decode-B64 $TitleB64
$message = Decode-B64 $MessageB64

[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

$tX = Xml-Escape $title
$mX = Xml-Escape $message

$attr = ""
if (-not [string]::IsNullOrEmpty($Url)) {
    $u = $Url.Replace("&", "&amp;")
    $attr = ' activationType="protocol" launch="' + $u + '"'
}

if ($Sound -eq "none") {
    $audio = '<audio silent="true" />'
} else {
    $audio = '<audio src="ms-winsoundevent:Notification.Default" />'
}

$template = '<toast' + $attr + '><visual><binding template="ToastGeneric"><text>' + $tX + '</text><text>' + $mX + '</text></binding></visual>' + $audio + '</toast>'

$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)

$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$appId = '__APP_ID__'

[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
Write-Output "TOAST_OK"
'''.replace("__APP_ID__", _APP_ID)

_ps1_win_path: str | None = None
_local_appdata: Path | None = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    config_path = (
        Path.home() / ".hermes" / "plugins" / "hermes-notification-win" / "config.json"
    )
    cfg = dict(DEFAULTS)
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            logger.warning("hermes-notification-win: failed to load config: %s", e)
    return cfg


# ---------------------------------------------------------------------------
# Windows plumbing
# ---------------------------------------------------------------------------

def _powershell() -> str | None:
    p = shutil.which("powershell.exe")
    if p:
        return p
    fallback = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    if os.path.exists(fallback):
        return fallback
    return None


def _win_local_appdata_path() -> Path | None:
    """%LOCALAPPDATA% 路径（WSL 下转 /mnt/c；原生 Windows 直接使用）。进程内缓存一次。"""
    global _local_appdata
    if _local_appdata is not None:
        return _local_appdata
    if os.name == "nt":
        p = os.environ.get("LOCALAPPDATA", "")
        path = Path(p) if p else None
        if path and path.is_dir():
            _local_appdata = path
        return _local_appdata
    ps = _powershell()
    if not ps:
        return None
    try:
        out = subprocess.run(
            [ps, "-NoProfile", "-Command", "Write-Output $env:LOCALAPPDATA"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        p = out.replace("\\", "/")
        if len(p) > 2 and p[1] == ":":
            p = "/mnt/" + p[0].lower() + p[2:]
        path = Path(p)
        if path.is_dir():
            _local_appdata = path
            return path
    except Exception as e:
        logger.debug("hermes-notification-win: LOCALAPPDATA lookup failed: %s", e)
    return None


def _to_win_path(unix_path: Path) -> str:
    """WSL: /mnt/c/... -> C:\\...；原生 Windows: 原样返回。"""
    s = str(unix_path)
    if os.name == "nt":
        return s
    rest = s[len("/mnt/"):] if s.startswith("/mnt/") else s
    drive, _, tail = rest.partition("/")
    if len(drive) == 1 and tail:
        return f"{drive.upper()}:\\" + tail.replace("/", "\\")
    return s


def _deploy_ps1() -> str | None:
    """把 notify.ps1 写到 %LOCALAPPDATA%/HermesNotify/，返回其 Windows 路径。"""
    global _ps1_win_path
    if _ps1_win_path:
        return _ps1_win_path
    local = _win_local_appdata_path()
    if not local:
        return None
    try:
        target_dir = local / _WIN_APP_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / _PS1_NAME
        if not target.exists() or target.read_text(encoding="utf-8", errors="replace") != _PS1_CONTENT:
            target.write_text(_PS1_CONTENT, encoding="utf-8")
        _ps1_win_path = _to_win_path(target)
        return _ps1_win_path
    except Exception as e:
        logger.warning("hermes-notification-win: could not deploy notify.ps1: %s", e)
        return None


def _b64(text: str) -> str:
    return base64.b64encode((text or "").encode("utf-8")).decode("ascii")


def _send(title: str, message: str, click_url: str = "", sound: bool = True) -> tuple[bool, str]:
    """同步发送一条纯展示的 Windows toast（powershell 路径）。返回 (是否成功, 说明)。"""
    ps = _powershell()
    if not ps:
        return False, "powershell.exe not found (需在 WSL 内运行)"
    script = _deploy_ps1()
    if not script:
        return False, "无法部署 notify.ps1 到 %LOCALAPPDATA%"

    cmd = [
        ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
        "-File", script,
        "-TitleB64", _b64(title),
        "-MessageB64", _b64(message),
        "-Url", click_url or "",
        "-Sound", "default" if sound else "none",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        out = r.stdout.decode("utf-8", "replace").strip()
        err = r.stderr.decode("utf-8", "replace").strip()
        if r.returncode == 0 and "TOAST_OK" in out:
            return True, "sent"
        return False, f"exit={r.returncode} out={out[:200]} err={err[:400]}"
    except Exception as e:
        return False, f"exception: {e}"


def _notify_async(title: str, message: str, click_url: str, sound: bool) -> None:
    def _run() -> None:
        ok, info = _send(title, message, click_url, sound)
        if ok:
            logger.debug("hermes-notification-win: toast sent")
        else:
            logger.warning("hermes-notification-win: toast failed: %s", info)

    threading.Thread(target=_run, daemon=True, name="hermes-notification-win").start()


# ---------------------------------------------------------------------------
# Daemon spool（带输入框的 toast）
# ---------------------------------------------------------------------------

def _spool_dir(cfg: dict) -> Path | None:
    raw = str(cfg.get("spool_dir") or "").strip()
    if raw:
        return Path(raw)
    local = _win_local_appdata_path()
    if local is None:
        return None
    return local / _WIN_APP_DIR / "spool"


def _daemon_alive(cfg: dict) -> bool:
    spool = _spool_dir(cfg)
    if spool is None:
        return False
    try:
        return (time.time() - (spool / "heartbeat").stat().st_mtime) < 15
    except OSError:
        return False


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


_TURN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes 回复</title><style>
:root{color-scheme:dark}
body{background:#0b0b0d;color:#e8e8ea;font:15px/1.65 -apple-system,"Segoe UI",system-ui,sans-serif;margin:0;padding:32px 20px}
.wrap{max-width:820px;margin:0 auto}
.meta{font-size:13px;color:#8e8e93;margin-bottom:6px}
h1{font-size:17px;font-weight:600;margin:0 0 18px;color:#f2f2f7}
pre{white-space:pre-wrap;word-break:break-word;background:#151517;border:1px solid #26262a;border-radius:14px;padding:20px;font:14px/1.75 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0}
textarea{width:100%;box-sizing:border-box;margin-top:20px;background:#151517;color:#e8e8ea;border:1px solid #2c2c31;border-radius:12px;padding:14px;font:14px/1.6 inherit;resize:vertical;min-height:88px}
textarea:focus{outline:none;border-color:#0a84ff}
.row{display:flex;align-items:center;gap:12px;margin-top:12px;flex-wrap:wrap}
button{background:#0a84ff;color:#fff;border:0;border-radius:10px;padding:10px 20px;font-size:14px;cursor:pointer}
button:disabled{opacity:.45;cursor:default}
.hint{font-size:12px;color:#636366}
.ok{font-size:13px;color:#30d158}
.err{font-size:13px;color:#ff453a}
</style></head><body><div class="wrap">
<div class="meta">__WHEN__ · __MODEL__</div>
<h1>Hermes 回复全文</h1>
<pre>__BODY__</pre>
<textarea id="reply" placeholder="在这里回复，Ctrl/Cmd+Enter 发送…"></textarea>
<div class="row"><button id="send">发送</button><span class="hint">回复会注入到正在运行的 Hermes 会话</span><span id="msg"></span></div>
<script>
const send=document.getElementById('send'),ta=document.getElementById('reply'),msg=document.getElementById('msg');
async function go(){const text=ta.value.trim();if(!text)return;send.disabled=true;msg.className='';msg.textContent='发送中…';
 try{const r=await fetch('/reply/__ID__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
  if(!r.ok)throw new Error(await r.text());
  msg.className='ok';msg.textContent='已发送，Hermes 会继续这一轮';ta.value='';}
 catch(e){msg.className='err';msg.textContent='失败：'+e.message;}
 finally{send.disabled=false;}}
send.onclick=go;
ta.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter')go();});
</script></div></body></html>"""


def _write_turn_page(cfg: dict, turn_id: str, text: str, meta: dict) -> str:
    """写这一轮的完整回复页面（守护进程用 127.0.0.1:<http_port> 提供），返回 click_url。"""
    spool = _spool_dir(cfg)
    if spool is None:
        return ""
    turns = spool.parent / "turns"
    page = (
        _TURN_HTML.replace("__ID__", html.escape(turn_id))
        .replace("__BODY__", html.escape(text))
        .replace("__WHEN__", html.escape(time.strftime("%Y-%m-%d %H:%M")))
        .replace("__MODEL__", html.escape(str(meta.get("model") or "")))
    )
    try:
        _write_atomic(turns / f"{turn_id}.html", page)
        _write_atomic(turns / f"{turn_id}.json", json.dumps(meta, ensure_ascii=False))
    except Exception as e:
        logger.warning("hermes-notification-win: turn page failed: %s", e)
        return ""
    port = int(cfg.get("http_port") or 9117)
    return f"http://127.0.0.1:{port}/turn/{turn_id}"


def _enqueue_job(cfg: dict, job: dict) -> bool:
    spool = _spool_dir(cfg)
    if spool is None:
        return False
    try:
        _write_atomic(spool / "outbox" / f"{job['id']}.json", json.dumps(job, ensure_ascii=False))
        return True
    except Exception as e:
        logger.warning("hermes-notification-win: enqueue failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# 回复注入
# ---------------------------------------------------------------------------

def _inject_allowed() -> bool:
    """注入要 Hermes 配置显式放行（plugins.entries.<id>.allow_gateway_injection）。"""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(
            "hermes-notification-win"
        ) or {}
        return entry.get("allow_gateway_injection") is True
    except Exception:
        return False


def _inject(text: str, session_key: str) -> bool:
    global _inject_warned
    if _ctx is None:
        return False
    if not _inject_allowed():
        if not _inject_warned:
            _inject_warned = True
            logger.warning(
                "hermes-notification-win: 回复注入被拒绝——在 config.yaml 的 "
                "plugins.entries.hermes-notification-win 下加 allow_gateway_injection: true"
            )
        return False
    try:
        ok = _ctx.inject_message(text, role="user", session_key=session_key or None)
    except Exception as e:
        logger.warning("hermes-notification-win: inject failed: %s", e)
        return False
    if not ok:
        logger.warning(
            "hermes-notification-win: inject_message 未被接受（session_key=%s）", session_key or "-"
        )
    return bool(ok)


def _drain_inbox(cfg: dict) -> None:
    spool = _spool_dir(cfg)
    if spool is None:
        return
    inbox = spool / "inbox"
    done = spool / "done"
    if not inbox.is_dir():
        return
    for f in sorted(inbox.glob("*.json")):
        if f.name.startswith("appr-"):
            continue  # 审批结果由 _approval_present 自己收，别当聊天回复注入
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        try:
            f.replace(done / f"reply-{f.name}")
        except Exception:
            pass
        action = str(data.get("action") or "")
        text = str(data.get("text") or "").strip()
        key = str(data.get("session_key") or "")
        if action != "reply" or not text:
            continue
        if not bool(cfg.get("inject_reply", True)):
            logger.info("hermes-notification-win: 收到回复但 inject_reply=false，丢弃")
            continue
        logger.info(
            "hermes-notification-win: 通知里收到回复（%d 字），注入会话 %s", len(text), key or "-"
        )
        _inject(text, key)


# ---------------------------------------------------------------------------
# 审批按钮（security.approval.transport: win-toast）
# ---------------------------------------------------------------------------

_APPROVAL_LABELS = {
    "once": "允许一次",
    "session": "允许本会话",
    "always": "永远允许",
    "deny": "拒绝",
}


def _approval_present(request):
    """Hermes 审批传输：把待批命令弹成带按钮的 Windows toast，等用户点。

    任何异常/超时/守护进程不在 → 返回 deny（失败关闭）。
    """
    cfg = _load_config()
    choices = [c for c in getattr(request, "allowed_choices", ()) if c in _APPROVAL_LABELS]
    if not choices:
        choices = ["deny"]
    if not _daemon_alive(cfg):
        logger.warning("hermes-notification-win: 守护进程不在，审批按拒绝处理（fail-closed）")
        return request.respond("deny")

    jid = f"appr-{getattr(request, 'request_id', '') or int(time.time() * 1000)}"
    command = str(getattr(request, "command", "") or "").strip()
    desc = str(getattr(request, "description", "") or "").strip()
    body = command if not desc else (f"{command}\n\n{desc}" if command else desc)
    job = {
        "id": jid,
        "kind": "approval",
        "title": "Hermes 需要你批准",
        "body": body[:700],
        "buttons": [_APPROVAL_LABELS[c] for c in choices],
        "sound": True,
        "ts": time.time(),
    }
    if not _enqueue_job(cfg, job):
        return request.respond("deny")

    spool = _spool_dir(cfg)
    path = spool / "inbox" / f"{jid}.json"
    try:
        budget = float(getattr(request, "timeout_seconds", 300) or 300) - 10
    except Exception:
        budget = 290
    deadline = time.time() + max(5.0, min(budget, 280))
    logger.info("hermes-notification-win: 审批通知已弹出（%s）", jid)
    while time.time() < deadline:
        if path.exists():
            label = ""
            try:
                label = str(json.loads(path.read_text(encoding="utf-8")).get("text") or "").strip()
            except Exception:
                pass
            finally:
                with suppress_os_error():
                    path.unlink()
            for c in choices:
                if _APPROVAL_LABELS[c] == label:
                    logger.info("hermes-notification-win: 审批结果 %s -> %s", jid, c)
                    return request.respond(c)
            logger.warning("hermes-notification-win: 审批返回了未知按钮 %r，按拒绝处理", label)
            return request.respond("deny")
        time.sleep(0.4)
    logger.warning("hermes-notification-win: 审批等待超时（%s），按拒绝处理", jid)
    return request.respond("deny")


class suppress_os_error:
    """unlink 失败（文件已被守护进程挪走）不影响决策。"""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, OSError)


def _start_reply_poller(cfg: dict) -> None:
    global _poller_started
    if _poller_started:
        return
    _poller_started = True
    interval = max(300, int(cfg.get("reply_poll_ms") or 1000)) / 1000.0

    def _loop() -> None:
        while True:
            try:
                _drain_inbox(_load_config())
            except Exception as e:
                logger.debug("hermes-notification-win: poller error: %s", e)
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="hermes-notification-reply").start()


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

def _preview(text: str, limit: int) -> str:
    body = text
    if body.startswith("```"):
        nl = body.find("\n")
        body = body[nl + 1:] if nl != -1 else body
    body = body[:limit]
    if len(text) > limit:
        body += "…"
    return body


def _on_post_llm_call(
    session_id: str = "",
    user_message: str = "",
    assistant_response: str = "",
    conversation_history: list = None,
    model: str = "",
    platform: str = "",
    **kwargs,
) -> None:
    try:
        cfg = _load_config()
        if not cfg.get("enabled", True):
            return

        # 默认过滤网关平台（telegram 等），只在本地 TUI/CLI 弹通知
        allowed = cfg.get("notify_platforms") or DEFAULTS["notify_platforms"]
        if str(platform or "").lower() not in {str(p).lower() for p in allowed}:
            return

        text = (assistant_response or "").strip()
        if len(text) < int(cfg.get("min_response_length", 10)):
            return

        title = str(cfg.get("title", DEFAULTS["title"]))
        body = _preview(text, int(cfg.get("preview_length", 80)))
        sound = bool(cfg.get("sound", True))

        if str(cfg.get("mode") or "daemon").lower() != "daemon":
            _notify_async(title, body, "", sound)
            return

        if not _daemon_alive(cfg):
            logger.debug("hermes-notification-win: 守护进程不在，回落 powershell")
            _notify_async(title, body, "", sound)
            return

        # 注入需要 gateway 的 session_key（不是 agent.session_id）
        key = ""
        try:
            from hermes_cli.plugin_session import session_key as _sk
            key = _sk() or ""
        except Exception:
            pass

        turn_id = f"{int(time.time() * 1000)}-{os.getpid()}"
        meta = {"session_key": key, "session_id": session_id or "", "model": model}
        click = _write_turn_page(cfg, turn_id, text, meta)
        job = {
            "id": turn_id,
            "title": title,
            "body": body,
            "click_url": click,
            "session_key": key,
            "session_id": session_id or "",
            "model": model,
            "sound": sound,
            "ts": time.time(),
        }
        if not _enqueue_job(cfg, job):
            _notify_async(title, body, "", sound)
    except Exception as e:  # 通知失败绝不影响主流程
        logger.debug("hermes-notification-win: hook error: %s", e)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    global _ctx
    cfg = _load_config()
    if not cfg.get("enabled", True):
        logger.info("hermes-notification-win: disabled via config, skipping")
        return
    _ctx = ctx
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    if cfg.get("enable_reply", True):
        _start_reply_poller(cfg)
    if cfg.get("enable_approval", True):
        try:
            ctx.register_approval_transport("win-toast", _approval_present)
            logger.info("hermes-notification-win: 审批传输 'win-toast' 已注册（security.approval.transport 选它才生效）")
        except Exception as e:
            logger.warning("hermes-notification-win: 审批传输注册失败: %s", e)
    if str(cfg.get("mode") or "daemon").lower() == "daemon":
        logger.info(
            "hermes-notification-win: registered (mode=daemon, daemon=%s, reply=%s)",
            "alive" if _daemon_alive(cfg) else "DOWN→powershell fallback",
            cfg.get("enable_reply", True),
        )
    else:
        ps = _powershell()
        logger.info("hermes-notification-win: registered (via %s)", ps)
