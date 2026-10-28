# -*- coding: utf-8 -*-
"""网络扫描：自动发现局域网内运行 ADB 服务的云手机。

本地后端模式下，云手机（真机 / redroid 容器）在局域网内通过 adb over TCP
（默认 5555/5556/5557/5558/5559 等端口）提供服务。客户端通过"扫描网络"
发现这些设备并导入设备列表。
"""
import concurrent.futures
import ipaddress
import os
import socket
import subprocess
import time

# 常见 adb over TCP 端口（可扩展）
# 单台/少量：5555-5559；云手机集群：5901-5930（36.156.109.x 实测端口段）
ADB_PORTS = [5555, 5556, 5557, 5558, 5559] + list(range(5901, 5931))
# 单端口探测超时（秒）
PROBE_TIMEOUT = 0.35
# 并发探测数
MAX_WORKERS = 128


def _adb_path() -> str:
    """获取 adb 可执行文件路径（优先配置，其次 PATH）。"""
    try:
        from flask import current_app
        p = current_app.config.get("ADB_PATH", "")
    except Exception:
        p = os.environ.get("ADB_PATH", "")
    if p and os.path.isfile(p):
        return p
    for c in ("adb",):
        try:
            if os.name == "nt":
                import shutil
                f = shutil.which(c)
                if f:
                    return f
        except Exception:
            pass
        if os.path.isfile(c):
            return c
    return "adb"


def _probe(ip: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def local_cidrs() -> list:
    """自动推导本机所在的所有 IPv4 网段（/24）。"""
    cidrs = []
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            net = ipaddress.ip_network(f"{ip}/24", strict=False)
            cidrs.append(str(net))
    except Exception:
        pass
    # 去重保持顺序
    seen = set()
    out = []
    for c in cidrs:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _scan_cidr(cidr: str, ports: list = None, max_workers: int = MAX_WORKERS):
    """扫描单个网段，返回 [(ip, [开放端口]), ...]。"""
    ports = ports or ADB_PORTS
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return []
    hosts = [str(h) for h in net.hosts()]

    def probe_host(ip):
        hits = []
        for p in ports:
            if _probe(ip, p):
                hits.append(p)
        return ip, hits

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for ip, hits in ex.map(probe_host, hosts):
            if hits:
                found.append((ip, hits))
    return found


def scan_networks(cidrs=None, ports: list = None) -> list:
    """扫描多个网段，返回 [{ip, port, serial}, ...]。"""
    if not cidrs:
        cidrs = local_cidrs()
        if not cidrs:
            return []
    ports = ports or ADB_PORTS
    results = []
    for cidr in cidrs:
        for ip, hits in _scan_cidr(cidr, ports):
            for p in hits:
                results.append({"ip": ip, "port": p, "serial": f"{ip}:{p}"})
    return results


def _run_adb(args: list, timeout: int = 12) -> tuple:
    adb = _adb_path()
    try:
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = 0x08000000
        else:
            si = None
            creationflags = 0
        p = subprocess.run(
            [adb] + args, capture_output=True, timeout=timeout,
            startupinfo=si, creationflags=creationflags,
        )
        return p.returncode, p.stdout, p.stderr
    except Exception:
        return -1, b"", b""


def adb_connect(serial: str) -> bool:
    rc, out, err = _run_adb(["connect", serial], timeout=10)
    text = (out + err).decode("utf-8", errors="replace").lower()
    return "connected" in text or "already" in text


def _device_info(serial: str, max_tries: int = 5) -> dict:
    """获取设备基本信息（型号/系统/分辨率/序列号），adb 连接未就绪时重试。"""
    def g(prop):
        for _ in range(max_tries):
            rc, out, _ = _run_adb(["-s", serial, "shell", "getprop", prop], timeout=8)
            if rc == 0 and out.strip():
                return out.decode("utf-8", errors="replace").strip()
            time.sleep(0.4)
        return ""

    def wm():
        for _ in range(max_tries):
            rc, out, _ = _run_adb(["-s", serial, "shell", "wm", "size"], timeout=8)
            if rc == 0 and out.strip():
                t = out.decode("utf-8", errors="replace").strip()
                if ":" in t:
                    return t.split(":", 1)[1].strip()
            time.sleep(0.4)
        return ""

    # 等待 adb 状态就绪
    online = False
    for _ in range(max_tries):
        rc, out, _ = _run_adb(["devices"], timeout=8)
        for line in out.decode("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == serial and parts[1] == "device":
                online = True
                break
        if online:
            break
        time.sleep(0.4)

    return {
        "serial": serial,
        "online": online,
        "model": g("ro.product.model"),
        "android": g("ro.build.version.release"),
        "resolution": wm(),
        "brand": g("ro.product.brand"),
    }


def scan_hosts(hosts: list, ports: list = None) -> list:
    """扫描指定主机 IP 的 adb 端口，返回 [{ip, port, serial}, ...]。

    hosts: 如 ["192.168.9.121"]，对该 IP 探测全部 ADB_PORTS。
    """
    ports = ports or ADB_PORTS
    results = []
    for ip in hosts:
        ip = str(ip).strip()
        if not ip:
            continue
        for p in ports:
            if _probe(ip, p):
                results.append({"ip": ip, "port": p, "serial": f"{ip}:{p}"})
    return results


def discover(cidrs=None, hosts=None, connect: bool = True) -> dict:
    """扫描网络并尝试 adb connect，返回发现的设备列表。

    优先扫描 hosts（单 IP 列表，探测全部 adb 端口），否则扫描 cidrs 网段。

    返回: {cidrs: [...], hosts: [...], scanned: n, devices: [{serial, ip, port, online, model, android, resolution}]}
    """
    t0 = time.time()
    if hosts:
        targets = scan_hosts(hosts)
        cidrs = []
    else:
        if not cidrs:
            cidrs = local_cidrs()
        targets = scan_networks(cidrs)
    devices = []
    for t in targets:
        serial = t["serial"]
        if connect:
            adb_connect(serial)
        info = _device_info(serial)
        info.update({"ip": t["ip"], "port": t["port"]})
        devices.append(info)
    return {
        "cidrs": cidrs,
        "hosts": hosts or [],
        "scanned": len(targets),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "devices": devices,
    }
