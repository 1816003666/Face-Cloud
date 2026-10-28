# -*- coding: utf-8 -*-
"""本地 ADB 端口隧道（新服务器专用）。

新服务器（36.156.109.237）上的 redroid 容器 adb 端口绑定在 127.0.0.1，
本地 Windows 无法直接 `adb connect 服务器IP:端口`。
本脚本通过 SSH 把服务器 127.0.0.1:<port> 转发到本地 127.0.0.1:<port>，
使本地 adb 可用 `adb connect localhost:<port>` 访问云手机。

特性：
- 每 45 秒重新扫描服务器容器端口，新建的云手机端口会自动被隧道转发；
- SSH 断线自动重连。
常驻运行：后端启动时由 run_stable.py 自动拉起，或手动 python adb_tunnel.py。
"""
import os
import socket
import threading
import time

import paramiko


def load_env(path):
    env = {}
    try:
        with open(path, "rb") as f:
            raw = f.read()
        for line in raw.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


def get_remote_ports(cli, prefix="redroid"):
    """通过 SSH 在服务器上获取 running redroid 容器的对外 ADB 端口。"""
    cmd = (
        "docker ps --format '{{.Names}}|{{.Ports}}|{{.Status}}' | "
        "awk -F'|' '$1 ~ /^" + prefix + "/ && $3 ~ /^Up/ {print $2}'"
    )
    _, out, _ = cli.exec_command(cmd, timeout=20)
    txt = out.read().decode("utf-8", errors="replace")
    ports = set()
    for seg in txt.replace(",", "\n").split():
        seg = seg.strip()
        if "->" not in seg or "5555/tcp" not in seg:
            continue
        host_part = seg.split("->")[0].strip()
        if ":" in host_part:
            host_part = host_part.rsplit(":", 1)[-1]
        try:
            ports.add(int(host_part))
        except ValueError:
            pass
    return sorted(ports)


def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
    env = load_env(os.path.join(base, ".env"))
    host = env.get("SSH_HOST") or env.get("CLOUD_PHONE_SERVER", "")
    user = env.get("SSH_USERNAME", "root")
    pwd = env.get("SSH_PASSWORD", "")
    prefix = env.get("REDROID_CONTAINER_PREFIX", "redroid")
    if not host or not pwd:
        print("[adb_tunnel] 缺少 SSH_HOST/SSH_PASSWORD，退出", flush=True)
        return

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    def connect():
        cli.connect(host, 22, user, pwd, timeout=20)
        return cli.get_transport()

    transport = connect()
    print("[adb_tunnel] SSH 已连接 %s@%s" % (user, host), flush=True)

    active = {}

    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass
            try:
                src.close()
            except Exception:
                pass

    def forward(client_sock, port):
        try:
            tr = transport
            chan = tr.open_channel(
                "direct-tcpip", ("127.0.0.1", port), client_sock.getpeername()
            )
        except Exception:
            client_sock.close()
            return
        threading.Thread(target=pump, args=(client_sock, chan), daemon=True).start()
        threading.Thread(target=pump, args=(chan, client_sock), daemon=True).start()

    def serve(port):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", port))
            srv.listen(16)
        except Exception as e:
            print("[adb_tunnel] 绑定 %d 失败: %s" % (port, e), flush=True)
            return
        print("[adb_tunnel] 监听 127.0.0.1:%d" % port, flush=True)
        while True:
            try:
                c, _ = srv.accept()
                forward(c, port)
            except Exception:
                break

    def ensure_port(port):
        if port in active:
            return
        t = threading.Thread(target=serve, args=(port,), daemon=True)
        t.start()
        active[port] = t

    # 首次扫描
    ports = get_remote_ports(cli, prefix)
    if not ports:
        ports = list(range(6000, 6629))
    for p in ports:
        ensure_port(p)
    print("[adb_tunnel] 初始转发 %d 个端口" % len(ports), flush=True)

    # 定时刷新（支持新建容器端口自动转发）+ 保活重连
    while True:
        time.sleep(45)
        try:
            if transport is None or not transport.is_active():
                print("[adb_tunnel] SSH 断开，重连 ...", flush=True)
                transport = connect()
                print("[adb_tunnel] 重连成功", flush=True)
            ports = get_remote_ports(cli, prefix)
            for p in ports:
                ensure_port(p)
        except Exception as e:
            print("[adb_tunnel] 刷新失败: %s" % e, flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
