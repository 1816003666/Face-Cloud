# 启动 122 云手机平台（本地 Windows 版）：后端 Flask + 同源托管 frontend\dist
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root "backend\venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "未找到 venv：$python"
    Write-Host "先执行：powershell -ExecutionPolicy Bypass -File `"$root\install-deps.ps1`""
    exit 1
}

$env:PORT = "8003"
Set-Location (Join-Path $root "backend")
Write-Host "backend  : $python"
Write-Host "frontend : $root\frontend\dist"
Write-Host "打开     : http://127.0.0.1:8003/   (Ctrl+C 停止)"
& $python run_stable.py
