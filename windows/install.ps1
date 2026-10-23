#Requires -Version 5.1
<#
  Hermes notification daemon installer (Windows side).

  What it does:
    1. Creates %LOCALAPPDATA%\HermesNotify\venv with a Python that win11toast supports
       (3.11 / 3.12 -- 3.13+ breaks it with "notification platform is unavailable")
    2. pip installs win11toast
    3. Copies toast_daemon.py next to the venv
    4. Writes a Startup entry so the daemon runs at logon
    5. Starts the daemon now

  Usage (from the repo root):
    powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
#>
$ErrorActionPreference = "Stop"

$root = Join-Path $env:LOCALAPPDATA "HermesNotify"
$src = $PSScriptRoot
New-Item -ItemType Directory -Force -Path $root | Out-Null

function Find-Python {
    $candidates = @(
        @{ cmd = "py"; args = @("-3.12") },
        @{ cmd = "py"; args = @("-3.11") },
        @{ cmd = "python"; args = @() }
    )
    foreach ($c in $candidates) {
        $exe = Get-Command $c.cmd -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        try {
            $ver = & $c.cmd @($c.args + @("-c", "import sys;print('%d.%d' % sys.version_info[:2])")) 2>$null
        } catch { continue }
        if (-not $ver) { continue }
        $major, $minor = $ver.Trim().Split(".")
        if ([int]$major -eq 3 -and [int]$minor -ge 11 -and [int]$minor -le 12) {
            return @{ cmd = $c.cmd; args = $c.args; version = $ver.Trim() }
        }
    }
    return $null
}

Write-Host "==> Looking for Python 3.11 / 3.12" -ForegroundColor Cyan
$py = Find-Python
if (-not $py) {
    Write-Host "No Python 3.11/3.12 found." -ForegroundColor Red
    Write-Host "Install one (https://www.python.org/downloads/) or: winget install Python.Python.3.12"
    Write-Host "Python 3.13/3.14 will NOT work: win11toast fails with WinError -2143420155."
    exit 1
}
Write-Host "    using $($py.cmd) $($py.args -join ' ') (Python $($py.version))"

$venv = Join-Path $root "venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "==> Creating venv at $venv" -ForegroundColor Cyan
    & $py.cmd @($py.args + @("-m", "venv", $venv))
} else {
    Write-Host "==> Reusing existing venv" -ForegroundColor Cyan
}

$pyExe = Join-Path $venv "Scripts\python.exe"
$pywExe = Join-Path $venv "Scripts\pythonw.exe"

Write-Host "==> Installing win11toast" -ForegroundColor Cyan
& $pyExe -m pip install --upgrade pip --quiet
& $pyExe -m pip install --upgrade win11toast --quiet
$ver = & $pyExe -c "import win11toast, importlib.metadata as m; print(m.version('win11toast'))"
Write-Host "    win11toast $ver"

$daemonSrc = Join-Path $src "toast_daemon.py"
if (-not (Test-Path $daemonSrc)) {
    Write-Host "toast_daemon.py not found next to install.ps1" -ForegroundColor Red
    exit 1
}
Copy-Item $daemonSrc (Join-Path $root "toast_daemon.py") -Force

Write-Host "==> Writing Startup entry" -ForegroundColor Cyan
$startup = [Environment]::GetFolderPath("Startup")
$cmdPath = Join-Path $startup "hermes-toast-daemon.cmd"
$cmdBody = @"
@echo off
rem Hermes notification daemon (auto-start at logon)
cd /d "$root"
start "" /B "$pywExe" "$root\toast_daemon.py"
"@
Set-Content -Path $cmdPath -Value $cmdBody -Encoding ASCII
Write-Host "    $cmdPath"

Write-Host "==> Starting daemon" -ForegroundColor Cyan
$stop = Join-Path $root "spool\stop"
if (Test-Path $stop) { Remove-Item $stop -Force }
Start-Process -FilePath $pywExe -ArgumentList "`"$root\toast_daemon.py`"" -WindowStyle Hidden
Start-Sleep -Seconds 3

$hb = Join-Path $root "spool\heartbeat"
if (Test-Path $hb) {
    Write-Host "    daemon is alive (heartbeat ok)" -ForegroundColor Green
} else {
    Write-Host "    daemon did not write a heartbeat -- check $root\spool\daemon.log" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Windows side done. Next (WSL side):" -ForegroundColor Cyan
Write-Host "  1. cp -r <this repo> ~/.hermes/plugins/hermes-notification-win"
Write-Host "  2. cp config.example.json ~/.hermes/plugins/hermes-notification-win/config.json"
Write-Host "  3. hermes plugins enable hermes-notification-win"
Write-Host "  4. hermes config set plugins.entries.hermes-notification-win.allow_gateway_injection true"
Write-Host "  5. (optional, approval buttons) hermes config set security.approval.transport win-toast"
Write-Host "  6. restart hermes (or reload the gateway's plugins)"
Write-Host ""
Write-Host "Test:  $pyExe -c \"from win11toast import notify; notify('Hermes','ok',input='reply',button='Send')\""
