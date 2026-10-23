"""Hermes toast daemon (Windows side).

轮询 spool/outbox 里的任务，弹带输入框的 Windows toast；用户输入的内容写回
spool/inbox，由 WSL 侧的 Hermes 插件注入到会话。

同时起一个只监听 127.0.0.1 的 HTTP 服务：
  GET  /turn/<id>   这一轮的完整回复页面（可回复）
  POST /reply/<id>  页面里的回复框提交 {"text": "..."}
  GET  /health      存活探针

依赖：win11toast（由 windows/install.ps1 装进 %LOCALAPPDATA%\HermesNotify\venv）
启动：venv\\Scripts\\pythonw.exe toast_daemon.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(os.path.dirname(os.path.abspath(__file__)))
SPOOL = BASE / "spool"
OUTBOX = SPOOL / "outbox"
INBOX = SPOOL / "inbox"
DONE = SPOOL / "done"
TURNS = BASE / "turns"
LOG = SPOOL / "daemon.log"
HEARTBEAT = SPOOL / "heartbeat"
STOP = SPOOL / "stop"
LOCK = BASE / "daemon.lock"

HTTP_PORT = int(os.environ.get("HERMES_TOAST_PORT", "9117"))
POLL_S = 0.5
_log_lock = threading.Lock()
_lock_fh = None
_alive: dict[str, object] = {}


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with _log_lock:
            if LOG.exists() and LOG.stat().st_size > 1_000_000:
                LOG.replace(LOG.with_suffix(".log.1"))
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def ensure_dirs() -> None:
    for d in (SPOOL, OUTBOX, INBOX, DONE, TURNS):
        d.mkdir(parents=True, exist_ok=True)


def single_instance() -> bool:
    global _lock_fh
    try:
        import msvcrt

        _lock_fh = open(LOCK, "a+")
        msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except Exception:
        return False


def write_inbox(job: dict, action: str, text: str) -> None:
    jid = str(job.get("id") or "")
    if not jid:
        return
    payload = {
        "id": jid,
        "action": action,
        "text": (text or "").strip(),
        "session_key": job.get("session_key") or "",
        "session_id": job.get("session_id") or "",
        "ts": time.time(),
    }
    tmp = INBOX / f"{jid}.tmp"
    final = INBOX / f"{jid}.json"
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, final)
        log(f"inbox <- {jid} action={action} len={len(payload['text'])}")
    except Exception:
        log("write_inbox failed\n" + traceback.format_exc())


def show_toast(job: dict) -> None:
    from win11toast import activated_args, notify

    jid = str(job.get("id") or "")
    kind = str(job.get("kind") or "notify")
    click = (job.get("click_url") or "").strip()
    sound = bool(job.get("sound", True))
    kwargs = dict(
        title=job.get("title") or "Hermes ✅",
        body=job.get("body") or "",
        duration="long",
        on_click=click or None,
    )
    if kind == "approval":
        # 审批通知：一排按钮，不要输入框
        kwargs["buttons"] = [str(b) for b in (job.get("buttons") or ["拒绝"])]
    else:
        kwargs["input"] = "reply"
        kwargs["button"] = "发送"
    if not sound:
        kwargs["audio"] = {"silent": "true"}
    note = notify(**kwargs)
    _alive[jid] = note  # 保持引用，回调不被回收

    def on_activated(sender, event):
        try:
            data = activated_args(sender, event) or {}
            if kind == "approval":
                raw = str(data.get("arguments") or "")
                label = raw.split(":", 1)[1] if ":" in raw else raw
                write_inbox(job, "reply", label.strip())
            else:
                text = ((data.get("user_input") or {}).get("reply") or "").strip()
                write_inbox(job, "reply" if text else "empty", text)
        except Exception:
            log("activated handler failed\n" + traceback.format_exc())
        finally:
            _alive.pop(jid, None)

    def on_dismissed(sender, event):
        try:
            write_inbox(job, "dismissed", "")
        except Exception:
            log("dismissed handler failed\n" + traceback.format_exc())
        finally:
            _alive.pop(jid, None)

    note.add_activated(on_activated)
    note.add_dismissed(on_dismissed)
    log(f"toast shown id={jid} click={click or '-'}")


def serve_loop() -> None:
    def read_turn(tid: str) -> bytes | None:
        safe = "".join(c for c in tid if c.isalnum() or c in "-_")
        p = TURNS / f"{safe}.html"
        if not p.is_file():
            return None
        return p.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静音默认日志
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send(200, b'{"ok":true}', "application/json")
                return
            if path.startswith("/turn/"):
                body = read_turn(path[len("/turn/"):])
                if body is None:
                    self._send(404, b"turn not found", "text/plain; charset=utf-8")
                    return
                self._send(200, body, "text/html; charset=utf-8")
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not path.startswith("/reply/"):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            tid = path[len("/reply/"):]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(min(length, 100_000)) if length else b"{}"
                data = json.loads(raw.decode("utf-8") or "{}")
                text = str(data.get("text") or "").strip()
            except Exception:
                self._send(400, b'{"ok":false,"error":"bad json"}', "application/json")
                return
            if not text:
                self._send(400, b'{"ok":false,"error":"empty"}', "application/json")
                return
            job = {"id": tid}
            meta = TURNS / f"{''.join(c for c in tid if c.isalnum() or c in '-_')}.json"
            if meta.is_file():
                try:
                    job.update(json.loads(meta.read_text(encoding="utf-8")))
                except Exception:
                    pass
            job["id"] = tid
            write_inbox(job, "reply", text)
            self._send(200, b'{"ok":true}', "application/json")

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
        srv.daemon_threads = True
        log(f"http listening on 127.0.0.1:{HTTP_PORT}")
        srv.serve_forever()
    except Exception:
        log("http server failed\n" + traceback.format_exc())


def prune_turns(keep: int = 200) -> None:
    """只留最近 keep 份回执页面，避免无限堆积。"""
    try:
        files = sorted(TURNS.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)
    except Exception:
        log("prune_turns failed\n" + traceback.format_exc())


def main() -> int:
    ensure_dirs()
    if not single_instance():
        return 0  # 已有实例
    log("daemon start pid=%s" % os.getpid())
    prune_turns()
    threading.Thread(target=serve_loop, daemon=True, name="http").start()
    seen: set[str] = set()
    while True:
        if STOP.exists():
            log("stop file seen; exiting")
            try:
                STOP.unlink()
            except Exception:
                pass
            return 0
        try:
            HEARTBEAT.write_text(str(time.time()), encoding="utf-8")
            for f in sorted(OUTBOX.glob("*.json")):
                name = f.name
                if name in seen:
                    continue
                seen.add(name)
                try:
                    job = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    log(f"bad job {name}: {traceback.format_exc()}")
                    f.replace(DONE / name)
                    continue
                try:
                    show_toast(job)
                except Exception:
                    log(f"show_toast failed {name}\n" + traceback.format_exc())
                try:
                    f.replace(DONE / name)
                except Exception:
                    pass
        except Exception:
            log("loop error\n" + traceback.format_exc())
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
