# hermes-notification-win

Hermes Agent 的 Windows 通知插件（WSL 跑 Hermes，通知弹在 Windows 桌面）。
**不只是通知**：回复完成时弹出的横幅带输入框，可以直接在通知里回话；危险命令的审批也能在通知里点按钮放行。

> 由 [itgoyo/hermes-notification](https://github.com/itgoyo/hermes-notification)（macOS 版，MIT）的 Windows 移植演进而来；通知底座换成了 [win11toast](https://github.com/GitHub30/win11toast)。

## 三个能力

| 能力 | 说明 |
|---|---|
| **完成通知 + 就地回复** | 一轮回复结束后弹横幅：标题 + 80 字预览 + 输入框。打字点「发送」，内容作为用户消息注入当前会话，Hermes 接着干 |
| **点开看全文** | 点横幅正文 → 浏览器打开这一轮的完整回复（本地 `127.0.0.1:9117`），页面底部也能回复 |
| **审批按钮** | 危险命令不再只在终端里问：横幅上直接给「允许一次 / 允许本会话 / 永远允许 / 拒绝」 |

## 怎么工作的

```
Hermes 插件 (post_llm_call)                     Windows 常驻守护进程 (toast_daemon.py)
   ├─ 写任务 → spool/outbox/*.json  ─────────▶  notify(input="reply", button="发送")
   └─ 写全文 → turns/<id>.html                    │
                                                  ├─ 用户打字/点按钮
   轮询 spool/inbox ◀─────────────────────────────┘
   └─ ctx.inject_message(text, session_key=当前会话)
```

WSL 和 Windows 之间**只用共享文件**（spool 目录）通信——WSL 连不上 Windows 的 `127.0.0.1`，别指望 HTTP。
反方向没问题：回执页面由守护进程自己起 HTTP 服务，Windows 浏览器直接开。

## 安装

### 1. Windows 侧

```powershell
git clone https://github.com/Kongdachui/hermes-notification-win
cd hermes-notification-win
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
```

它会：找 Python **3.11 / 3.12** → 在 `%LOCALAPPDATA%\HermesNotify\venv` 建独立环境 → 装 win11toast →
拷守护进程 → 写开机自启 → 立刻启动。

> ⚠️ **Python 3.13+ 不行**：win11toast 会抛 `OSError [WinError -2143420155] The notification platform is unavailable`。
> 没有 3.11/3.12 的话：`winget install Python.Python.3.12`。

### 2. WSL 侧

```bash
cp -r hermes-notification-win ~/.hermes/plugins/
cd ~/.hermes/plugins/hermes-notification-win
cp config.example.json config.json
hermes plugins enable hermes-notification-win
hermes config set plugins.entries.hermes-notification-win.allow_gateway_injection true
# 可选：审批按钮（不做这步就只在终端里问）
hermes config set security.approval.transport win-toast
```

然后重启 `hermes`（插件只在进程启动时加载）。不想重启 TUI 的话，热重载：

```bash
PYTHONPATH=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python -c "
from pathlib import Path; from gateway.control_socket import reload_gateway_plugins
print(reload_gateway_plugins(Path.home()/'.hermes'))"
```

## 配置

`~/.hermes/plugins/hermes-notification-win/config.json`

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `mode` | `"daemon"` | `daemon` = 带输入框的 toast；`powershell` = 旧的纯展示 toast（零依赖） |
| `title` | `"Hermes ✅"` | 横幅标题 |
| `sound` | `true` | 提示音 |
| `preview_length` | `80` | 横幅里正文截断长度 |
| `min_response_length` | `10` | 短于此不弹 |
| `notify_platforms` | `["tui","cli","terminal",""]` | 只在这些平台弹（避免 Telegram 等网关重复打扰） |
| `enable_reply` | `true` | 横幅带输入框 + 起回复轮询线程 |
| `inject_reply` | `true` | 把收到的回复注入会话 |
| `enable_approval` | `true` | 注册审批传输 `win-toast` |
| `spool_dir` | `""` | 留空 = `%LOCALAPPDATA%\HermesNotify\spool` |
| `http_port` | `9117` | 回执页面的本地端口 |

## 审批按钮的取舍

`security.approval.transport` 一旦设成 `win-toast`，**审批只走通知**（终端里的弹窗被替换）。所以本插件是**失败关闭**的：

- 守护进程不在 / 等你等到超时（默认 300s，内部预留 10s）/ 点到未知按钮 → 一律当**拒绝**
- 回退：`hermes config set security.approval.transport builtin`

## 已知限制

- 通知来源显示为「Python」（借的是 win11toast 默认 AUMID）。想显示「Hermes」要注册自己的开始菜单快捷方式 + AUMID，暂未做
- 守护进程靠 Windows 登录态自启；没登录/没跑时会自动回落到纯展示的 powershell toast
- 插件里用了一处 Hermes 私有接口 `hermes_cli.plugin_session.session_key()` 来拿会话 key（`ctx.*` 没有公开等价物），已包在 try/except 里，Hermes 升级后若改名会退化为「不注入」而不是崩
- 只在 Windows + WSL 环境验证过

## 卸载

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\uninstall.ps1        # 停守护 + 删自启
powershell -ExecutionPolicy Bypass -File .\windows\uninstall.ps1 -Purge # 连 venv/spool 一起删
```

```bash
hermes config set security.approval.transport builtin   # 如果开过审批
hermes plugins disable hermes-notification-win
rm -rf ~/.hermes/plugins/hermes-notification-win
```

## License

MIT。见 [LICENSE](LICENSE)。

致谢：本项目的插件骨架与配置项源自 [itgoyo/hermes-notification](https://github.com/itgoyo/hermes-notification)（MIT，macOS 版）；
toast 的输入框与按钮回调由 [win11toast](https://github.com/GitHub30/win11toast)（MIT）提供。
