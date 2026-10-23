#Requires -Version 5.1
<#
  Stop the Hermes notification daemon and remove its Startup entry.
  The venv and spool under %LOCALAPPDATA%\HermesNotify are left in place; add -Purge to delete them.
#>
param([switch]$Purge)
$ErrorActionPreference = "Continue"

$root = Join-Path $env:LOCALAPPDATA "HermesNotify"
$stop = Join-Path $root "spool\stop"
if (Test-Path (Join-Path $root "spool")) {
    Set-Content -Path $stop -Value "stop" -Encoding ASCII
    Start-Sleep -Seconds 2
    Write-Host "stop file written; daemon exits on its next poll"
}

$cmdPath = Join-Path ([Environment]::GetFolderPath("Startup")) "hermes-toast-daemon.cmd"
if (Test-Path $cmdPath) {
    Remove-Item $cmdPath -Force
    Write-Host "removed Startup entry: $cmdPath"
}

Get-Process pythonw -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "$root*" } |
    ForEach-Object { Write-Host "still running: PID $($_.Id)"; }

if ($Purge) {
    Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "purged $root"
}
