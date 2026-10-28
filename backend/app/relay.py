# -*- coding: utf-8 -*-
"""云手机 SSH 中继通道。

为什么需要它
------------
服务器上的云手机，本机往往是连不上的：

  · 117 这类 USB 直连板子：serial 是 `D8835SUB06` 这种 USB 号，
    只有服务器上做了 `adb forward tcp:16001 tcp:5555` 才有一个
    127.0.0.1 端口——而那个 127.0.0.1 指的是**服务器自己**。
  · 122 这类网络 ADB / redroid：serial 是 `192.168.99.101:5555`，
    192.168.99.x 是服务器上的 docker 网桥网段，平台机器根本路由不到
    （实测 ping 100% 丢包）。

好在 SSH 的 `direct-tcpip` 通道可以让 sshd **代替我们**去连服务器侧的地址。
于是本模块做一件事：

    本机 127.0.0.1:<本端口>  ==SSH==>  服务器  ==>  <target_host>:<target_port>

之后平台只要 `adb connect 127.0.0.1:<本端口>`，就能像本地设备一样操作。

用法
----
    port = ensure_relay(ssh_host, ssh_user, secret, "192.168.99.101", 5555)
    # -> 36001，之后 adb connect 127.0.0.1:36001

说明：通道活在后端进程里，后端重启需要重新 ensure（发现时会自动重建）。
"""
from __future__ import annotations

import socket
import threading
import time

LOCAL_PORT_BASE = 36000       # 本机中继端口起始（26001-26010 已被 117 隧道占用）
LOCAL_PORT_MAX = 36900

_lock = threading.Lock()
# key: (ssh_host, ssh_port, username, target_host, target_port) -> 本机端口
_assigned: dict[tuple, int] = {}
# ssh_host -> ssh 凭据（同一个服务器复用一条 SSH 连接）
_transports: dict[tuple, object] = {}
_local_ports: set[int] = set()


def _pick_local_port() -> int:
    for p in range(LOCAL_PORT_BASE, LOCAL_PORT_MAX):
        if p in _local_ports:
            continue
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            try:
                s.close()
            except OSError:
                pass
    raise RuntimeError("本机中继端口已用尽")


def _get_transport(ssh_host, ssh_port, username, secret, auth_type):
    """按服务器复用一条 SSH 连接，断线自动重连。"""
    import paramiko

    key = (ssh_host, ssh_port, username)
    with _lock:
        tr = _transports.get(key)
        if tr is not None and tr.is_active():
            return tr

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {
        "hostname": ssh_host,
        "port": int(ssh_port or 22),
        "username": username,
        "timeout": 15,
        "allow_agent": False,
        "look_for_keys": False,
    }
    if auth_type == "key":
        kwargs["key_filename"] = secret
    else:
        kwargs["password"] = secret
    cli.connect(**kwargs)
    # 保活，避免长时间空闲被服务器断开
    tr = cli.get_transport()
    if tr:
        tr.set_keepalive(30)
    with _lock:
        _transports[key] = tr
    return tr


def _pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:  # noqa: BLE001
            pass
        try:
            src.close()
        except Exception:  # noqa: BLE001
            pass


def _serve(local_port, getter, target_host, target_port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", local_port))
    srv.listen(32)
    while True:
        try:
            conn, _addr = srv.accept()
        except OSError:
            break
        try:
            tr = getter()
            chan = tr.open_channel(
                "direct-tcpip",
                (target_host, int(target_port)),
                conn.getpeername(),
            )
        except Exception:  # noqa: BLE001 - 建通道失败就断开这个连接，等下一次
            try:
                conn.close()
            except OSError:
                pass
            continue
        threading.Thread(target=_pump, args=(conn, chan), daemon=True).start()
        threading.Thread(target=_pump, args=(chan, conn), daemon=True).start()


def ensure_relay(ssh_host, ssh_user, secret, target_host, target_port,
                 ssh_port=22, auth_type="password", timeout=20) -> int:
    """建立（或复用）一条中继通道，返回本机端口。"""
    if not target_host or not target_port:
        raise ValueError("缺少中继目标地址")

    key = (ssh_host, int(ssh_port or 22), ssh_user, target_host, int(target_port))
    with _lock:
        if key in _assigned:
            return _assigned[key]
        local_port = _pick_local_port()
        _local_ports.add(local_port)
        _assigned[key] = local_port

    def getter():
        return _get_transport(ssh_host, ssh_port, ssh_user, secret, auth_type)

    # 先探一次，确保 SSH 可用（失败就不占着端口）
    getter()
    threading.Thread(
        target=_serve,
        args=(local_port, getter, target_host, target_port),
        name=f"relay-{local_port}",
        daemon=True,
    ).start()

    # 等端口真的开始监听
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=1):
                return local_port
        except OSError:
            time.sleep(0.2)
    return local_port


def relay_status():
    """返回当前已建立的中继，便于排查。"""
    with _lock:
        items = []
        for (host, port, user, th, tp), lp in _assigned.items():
            items.append({
                "ssh_host": host, "ssh_port": port, "username": user,
                "target": f"{th}:{tp}", "local_port": lp,
                "local_serial": f"127.0.0.1:{lp}",
            })
        items.sort(key=lambda x: x["local_port"])
        return items
