"""系统设置：告警/巡检阈值等可视化配置（key-value 持久化到 system_configs）。"""
import re
import platform
import subprocess

from flask import jsonify, request, Blueprint
settings_bp = Blueprint("settings", __name__)
from ..extensions import db  # noqa: F401
from ..models import SETTING_DEFAULTS, get_setting, set_setting, Device
from ..auth import login_required, require_role, require_perm

# 前端 dev server 端口（本地 Windows 开发用；线上 Linux 由 nginx 暴露，端口以请求实际到达为准）
LAN_FRONTEND_PORT = 5174
LAN_BACKEND_PORT = 8001


# ---------------------------------------------------------------------------
# 局域网访问信息
# ---------------------------------------------------------------------------
def _lan_ips():
    """本机所有局域网 IPv4（排除回环/APIPA/虚拟网段）。跨平台：Linux 用 ip/hostname，Windows 用 PowerShell。"""
    ips = []
    try:
        if platform.system() == "Windows":
            # ipconfig 在中文 Windows 上经常漏网卡，改用 PowerShell 拿全部地址
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetIPAddress -AddressFamily IPv4 | "
                 "Where-Object { $_.IPAddress -ne '127.0.0.1' } | "
                 "Select-Object -ExpandProperty IPAddress"],
                capture_output=True, timeout=10)
            txt = (out.stdout or b"").decode("utf-8", errors="ignore")
            for line in txt.splitlines():
                line = line.strip()
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", line) and not line.startswith("169.254."):
                    ips.append(line)
        else:
            out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                                 capture_output=True, timeout=10)
            txt = (out.stdout or b"").decode("utf-8", errors="ignore")
            for line in txt.splitlines():
                m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
                if m and m.group(1) not in ("127.0.0.1", "0.0.0.0") \
                        and not m.group(1).startswith("169.254."):
                    ips.append(m.group(1))
            if not ips:  # 兜底
                out2 = subprocess.run(["hostname", "-I"], capture_output=True, timeout=10)
                for tok in (out2.stdout or b"").decode().split():
                    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", tok) and tok != "127.0.0.1":
                        ips.append(tok)
    except Exception:  # noqa: BLE001
        pass
    return sorted(set(ips))


def _device_prefix():
    """云手机所在的网段前缀（如 192.168.9.），没有设备则返回空串。"""
    try:
        rows = db.session.query(Device.ip).filter(Device.ip.isnot(None)).all()
        counts = {}
        for (ip,) in rows:
            ip = (ip or "").strip()
            if not ip or ip.startswith("127."):
                continue
            parts = ip.split(".")
            if len(parts) != 4:
                continue
            key = ".".join(parts[:3]) + "."
            counts[key] = counts.get(key, 0) + 1
        if counts:
            return max(counts.items(), key=lambda kv: kv[1])[0]
    except Exception:  # noqa: BLE001
        pass
    return ""


@settings_bp.get("/settings/lan")
@login_required
def lan_info():
    """局域网访问信息：电脑/手机用哪个地址能打开本平台。

    优先展示与云手机同网段的地址；若一个都没有（库里没设备）就全列出来。
    访问端口以请求实际到达的 Host 为准（线上 nginx 多为 80/443，本地 dev 为 5174）。
    """
    all_ips = _lan_ips()
    prefix = _device_prefix()
    preferred = [ip for ip in all_ips if prefix and ip.startswith(prefix)]
    shown = preferred or all_ips

    # 端口：优先取浏览器实际到达的 Host 端口，否则按平台回退
    port = LAN_FRONTEND_PORT
    host = (request.host or "").strip()
    if ":" in host:
        try:
            port = int(host.split(":", 1)[1])
        except (ValueError, IndexError):
            pass
    elif platform.system() != "Windows":
        port = 80  # 线上 Linux 经 nginx 暴露，标准 HTTP 端口
    # 拼 URL：80 端口省略不写更直观
    def _url(ip):
        return f"http://{ip}" if port in (80, 443) else f"http://{ip}:{port}"

    if platform.system() == "Windows":
        hint = (
            "若其他设备打不开，多半是 Windows 防火墙拦了入站连接："
            f"以管理员身份运行项目根目录的 allow_lan.ps1，放行 {port} 端口即可。"
        )
    else:
        hint = (
            "若同网段其他设备打不开，请确认本机防火墙已放行该端口"
            f"（如 Ubuntu: sudo ufw allow {port}），且访问地址与服务器在同一网段。"
        )
    return jsonify({
        "ips": shown,
        "all_ips": all_ips,
        "prefix": prefix,
        "frontend_port": port,
        "backend_port": LAN_BACKEND_PORT,
        "urls": [_url(ip) for ip in shown],
        "all_urls": [_url(ip) for ip in all_ips],
        "firewall_hint": hint,
    })


@settings_bp.get("/settings")
@require_perm("settings.manage")
def list_settings():
    out = {}
    for k, default in SETTING_DEFAULTS.items():
        v = get_setting(k)
        out[k] = default if v is None else _cast(v, default)
    return jsonify(out)


@settings_bp.put("/settings")
@require_perm("settings.manage")
def update_settings():
    data = request.get_json(silent=True) or {}
    out = {}
    for k, v in data.items():
        if k not in SETTING_DEFAULTS:
            continue
        set_setting(k, v)
        out[k] = v
    return jsonify(out)


def _cast(v, default):
    try:
        if isinstance(default, int):
            return int(v)
        if isinstance(default, float):
            return float(v)
        if isinstance(default, bool):
            return str(v).lower() in ("1", "true", "yes", "on")
    except (TypeError, ValueError):
        return default
    return v
