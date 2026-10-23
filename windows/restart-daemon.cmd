@echo off
rem 重启 Hermes 通知守护进程（改了 toast_daemon.py 之后用这个）
cd /d "%~dp0"
echo stop> spool\stop
timeout /t 2 /nobreak >nul
del /q spool\stop 2>nul
start "" /B "%~dp0venv\Scripts\pythonw.exe" "%~dp0toast_daemon.py"
echo daemon restarted
