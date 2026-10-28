# -*- coding: utf-8 -*-
"""SSH 隧道：本机 127.0.0.1:26001-26010 -> 192.168.9.117:127.0.0.1:16001-16010

117 上 USB 直连 10 台 RK3399 云手机，服务器侧已用 `adb forward tcp:1600X tcp:5555`
把每台设备的 adb 暴露到服务器本机的 16001-16010。
本脚本把这些端口再经 SSH 转发到我们这台机器，使本地 adb 可用
`adb connect 127.0.0.1:26001` 访问到它们。

常驻运行，SSH 断线自动重连。
    python tunnel_117.py
"""
import socket
import threading
import time

import paramiko

HOST = "192.168.9.117"
SSH_PORT = 22
USERNAME = "firefly"
PASSWORD = "Passw0rd"

LOCAL_BASE = 26001
REMOTE_BASE = 16001
COUNT = 10
REMOTE_ADDR = "127.0.0.1"

_stop = threading.Event()


def pipe(conn: socket.socket, chan):
    """双向搬运数据。"""
    try:
        while not _stop.is_set():
            r, _, _ = select.select([conn, chan], [], [], 10)
            if not r:
                continue
            if conn in r:
                data = conn.recv(65536)
                if not data:
                    break
                chan.sendall(data)
            if chan in r:
                data = chan.recv(65536)
                if not data:
                    break
                conn.sendall(data)
    except Exception:
        pass
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def serve(transport, local_port, remote_port):
    """监听本地端口，每个连接通过 SSH 通道转发到远端。"""
    while not _stop.is_set():
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", local_port))
            srv.listen(8)
            srv.settimeout(5)
            while not _stop.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                try:
                    chan = transport.open_channel(
                        "direct-tcpip", (REMOTE_ADDR, remote_port), addr)
                except Exception:
                    conn.close()
                    continue
                threading.Thread(target=pipe, args=(conn, chan), daemon=True).start()
        except Exception as e:
            print(f"[tunnel] {local_port} 监听失败: {e!r}", flush=True)
            time.sleep(3)
        finally:
            try:
                srv.close()
            except Exception:
                pass


def main():
    import select as _select
    global select
    select = _select  # 保证 pipe 中可用

    while not _stop.is_set():
        client = None
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(HOST, port=SSH_PORT, username=USERNAME,
                           password=PASSWORD, timeout=15)
            transport = client.get_transport()
            print(f"[tunnel] 已连接 {HOST}", flush=True)
            for i in range(COUNT):
                lp, rp = LOCAL_BASE + i, REMOTE_BASE + i
                threading.Thread(target=serve, args=(transport, lp, rp),
                                 daemon=True).start()
                print(f"[tunnel] 127.0.0.1:{lp} -> {HOST}:{REMOTE_ADDR}:{rp}", flush=True)

            while not _stop.is_set() and transport.is_alive():
                time.sleep(5)
            print("[tunnel] SSH 连接断开，5 秒后重连", flush=True)
        except Exception as e:
            print(f"[tunnel] 连接失败: {e!r}，5 秒后重试", flush=True)
        finally:
            try:
                client.close()
            except Exception:
                pass
        time.sleep(5)


if __name__ == "__main__":
    main()
