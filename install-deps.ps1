# 一次性安装本地调试依赖（Python 3.11 venv + pip）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root "backend\venv"
$req = Join-Path $root "backend\requirements-local.txt"

if (-not (Test-Path $venv)) {
    Write-Host "== 创建 venv: $venv"
    py -3.11 -m venv $venv
}
$python = Join-Path $venv "Scripts\python.exe"
Write-Host "== 升级 pip"
& $python -m pip install --upgrade pip
Write-Host "== 安装依赖（$req）"
& $python -m pip install -r $req
Write-Host "== 完成，运行 start-backend.ps1 启动"
& $python -c "import flask, flask_sqlalchemy, flask_sock, jwt, apscheduler, paramiko, PIL; print('deps OK')"
