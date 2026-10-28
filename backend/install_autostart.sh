#!/usr/bin/env bash
# 云手机服务器项目 —— 开机自动运行安装脚本
# 在目标服务器上以 root 执行：sudo bash install_autostart.sh [项目目录] [端口]
# 默认：/home/firefly/cloud-phone/backend  8003
# 幂等：重复执行只做必要的改动，不会重复重启服务。
set -euo pipefail

APP_DIR="${1:-/home/firefly/cloud-phone/backend}"
PORT="${2:-8003}"
RUN_USER="${RUN_USER:-firefly}"
SERVICE="cloudphone.service"
UNIT="/etc/systemd/system/${SERVICE}"

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERR] 请用 root 运行：sudo bash $0" >&2
  exit 1
fi
if [ ! -f "${APP_DIR}/run_stable.py" ]; then
  echo "[ERR] 未找到 ${APP_DIR}/run_stable.py，请先部署后端代码" >&2
  exit 1
fi

PY="${APP_DIR}/venv/bin/python"
[ -x "${PY}" ] || PY="$(command -v python3)"

NEW_UNIT="$(cat <<EOF
[Unit]
Description=CloudPhone Board Backend (new platform)
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
Environment=PORT=${PORT}
ExecStart=${PY} run_stable.py
Restart=always
RestartSec=5
StandardOutput=append:/tmp/backend.log
StandardError=append:/tmp/backend.log

[Install]
WantedBy=multi-user.target
EOF
)"

if [ -f "${UNIT}" ] && [ "$(cat "${UNIT}")" = "${NEW_UNIT}" ]; then
  echo "[1/4] systemd 单元已存在且内容一致，跳过重写"
  CHANGED=0
else
  printf '%s\n' "${NEW_UNIT}" > "${UNIT}"
  chmod 644 "${UNIT}"
  echo "[1/4] 已写入 ${UNIT}"
  CHANGED=1
fi

systemctl daemon-reload
echo "[2/4] 已 daemon-reload"

systemctl enable "${SERVICE}" >/dev/null 2>&1 || true
echo "[3/4] 开机自启：$(systemctl is-enabled ${SERVICE} 2>&1)"

if [ "${CHANGED}" = "1" ] || ! systemctl is-active --quiet "${SERVICE}"; then
  systemctl restart "${SERVICE}"
  echo "[4/4] 已重启服务"
else
  echo "[4/4] 服务已在运行，未重启"
fi

sleep 5
echo "--- 状态 ---"
echo "enabled : $(systemctl is-enabled ${SERVICE} 2>&1)"
echo "active  : $(systemctl is-active ${SERVICE} 2>&1)"
echo "--- 健康检查 ---"
curl -s -m 8 "http://127.0.0.1:${PORT}/api/health" || echo "(健康检查无响应)"
echo
echo "[OK] 服务器项目已配置为开机自动运行"
