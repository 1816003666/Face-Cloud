"""SSH 桥接：通过 SSH 连接主机，列出 USB adb 设备

安全加固（2026-10-14，S1）：
1. 口令必填——删除代码里写死的默认口令（PC 客户端本就显式传 password，不受影响）；
2. host 白名单——仅允许内网白名单网段（默认 192.168.9.0/24、192.168.99.0/24，
   可用环境变量 SSH_BRIDGE_ALLOWED_CIDRS 覆盖）；拒绝回环/链路本地/公网地址，
   防止被当跳板（SSRF）；
3. serial 校验 + 引号包裹（/control、/screenshot）：serial 与 cmd 不再原样拼进
   宿主机 shell，命令注入（借助 serial/cmd 逃逸到宿主机执行任意命令）被封死。
"""
import ipaddress
import os
import re
import shlex
from datetime import datetime

import paramiko
from flask import Blueprint, request, jsonify, Response

from app.extensions import db
from app.models import Device

bp = Blueprint('ssh_bridge', __name__)

_DEFAULT_ALLOWED_CIDRS = "192.168.9.0/24,192.168.99.0/24"
_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,64}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def _allowed_cidrs():
    raw = os.environ.get("SSH_BRIDGE_ALLOWED_CIDRS", _DEFAULT_ALLOWED_CIDRS)
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return nets


def _host_allowed(host):
    """host 必须是白名单网段内的 IPv4 字面量（不接受域名/回环/公网地址）。"""
    try:
        ip = ipaddress.ip_address(str(host).strip())
    except ValueError:
        return False
    if ip.version != 4 or ip.is_loopback or ip.is_link_local \
            or ip.is_multicast or ip.is_unspecified:
        return False
    return any(ip in net for net in _allowed_cidrs())


def _conn_params(data):
    """校验 host/username/password；返回 (params, error_response)。"""
    host = str(data.get('host') or '').strip()
    if not host:
        return None, (jsonify(error='host required'), 400)
    if not _host_allowed(host):
        return None, (jsonify(error='host not allowed'), 400)
    password = data.get('password')
    if not password:
        return None, (jsonify(error='password required'), 400)
    username = str(data.get('username') or 'firefly').strip() or 'firefly'
    if not _USERNAME_RE.fullmatch(username):
        return None, (jsonify(error='invalid username'), 400)
    return (host, username, str(password)), None


def _bad_serial(sn):
    return not (isinstance(sn, str) and _SERIAL_RE.fullmatch(sn))


@bp.route('/ssh-bridge/devices', methods=['POST'])
def list_devices():
    """通过 SSH 连接主机，列出 adb 设备"""
    data = request.get_json(force=True, silent=True) or {}
    params, err = _conn_params(data)
    if err:
        return err
    host, username, password = params

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=username, password=password, timeout=8)

        # 列出 adb 设备
        stdin, stdout, stderr = ssh.exec_command("adb devices")
        out = stdout.read().decode()

        devices = []
        for line in out.strip().split("\n")[1:]:
            if "\tdevice" in line:
                sn = line.split("\t")[0]
                devices.append({"id": sn, "serial": sn, "host": host})

        ssh.close()
        return jsonify(devices=devices)

    except Exception as e:
        return jsonify(error=str(e)), 500


@bp.route('/ssh-bridge/sync', methods=['POST'])
def sync_devices():
    """把 SSH 桥接的设备同步到数据库（云机页可见）"""
    data = request.get_json(force=True, silent=True) or {}
    params, err = _conn_params(data)
    if err:
        return err
    host, username, password = params

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=username, password=password, timeout=8)

        stdin, stdout, stderr = ssh.exec_command("adb devices")
        out = stdout.read().decode()
        ssh.close()

        synced = 0
        for line in out.strip().split("\n")[1:]:
            if "\tdevice" in line:
                sn = line.split("\t")[0]
                # 检查数据库里有没有
                existing = Device.query.filter_by(serial=sn).first()
                if not existing:
                    d = Device(
                        name=f"USB-{sn[:8]}",
                        serial=sn,
                        backend="ssh_bridge",
                        ip=host,
                        status="online",
                        last_seen=datetime.utcnow(),
                        cpu=0.0, mem=0.0,
                    )
                    db.session.add(d)
                    synced += 1
        db.session.commit()

        return jsonify(synced=synced, total=Device.query.count())

    except Exception as e:
        db.session.rollback()
        return jsonify(error=str(e)), 500


@bp.route('/ssh-bridge/control', methods=['POST'])
def control():
    """通过 SSH 执行 adb 命令"""
    data = request.get_json(force=True, silent=True) or {}
    params, err = _conn_params(data)
    if err:
        return err
    host, username, password = params
    sn = data.get('serial')
    cmd = data.get('cmd')

    if not all([host, sn, cmd]):
        return jsonify(error='host, serial, cmd required'), 400
    if _bad_serial(sn):
        return jsonify(error='invalid serial'), 400
    if not isinstance(cmd, str):
        return jsonify(error='invalid cmd'), 400

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=username, password=password, timeout=8)

        # 引号包裹：serial/cmd 不再被宿主机 shell 解释（设备侧语义不变）
        remote_cmd = "adb -s %s shell %s" % (shlex.quote(sn), shlex.quote(cmd))
        stdin, stdout, stderr = ssh.exec_command(remote_cmd)
        out = stdout.read().decode()
        err_out = stderr.read().decode()

        ssh.close()
        return jsonify(output=out, error=err_out)

    except Exception as e:
        return jsonify(error=str(e)), 500


@bp.route('/ssh-bridge/screenshot', methods=['POST'])
def screenshot():
    """通过 SSH 截图，返回 PNG 图片"""
    data = request.get_json(force=True, silent=True) or {}
    params, err = _conn_params(data)
    if err:
        return err
    host, username, password = params
    sn = data.get('serial')

    if not all([host, sn]):
        return jsonify(error='host, serial required'), 400
    if _bad_serial(sn):
        return jsonify(error='invalid serial'), 400

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=username, password=password, timeout=8)

        # 直接输出到 stdout，不用文件
        stdin, stdout, stderr = ssh.exec_command(
            "adb -s %s shell screencap -p" % shlex.quote(sn))
        img_data = stdout.read()

        ssh.close()

        return Response(img_data, mimetype='image/png')

    except Exception as e:
        return jsonify(error=str(e)), 500
