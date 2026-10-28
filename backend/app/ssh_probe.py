"""服务器 SSH 探测：连接远程服务器并发现其中的云手机。

发现优先级（**adb devices 排在最前，因为它最贴近"服务器上到底有什么云手机"**）：
0. adb devices：在服务器上执行 `adb devices -l`。只要服务器自己的 adb 能看到，
   就一定能被发现——覆盖三种形态：
     · USB 直连板子（如 117：serial = D8835SUB06）
     · 网络 ADB / redroid 容器（如 122：serial = 192.168.99.101:5555）
     · 本机 forward 端口（如 36.156.109.x：serial = 127.0.0.1:7012）
1. docker：列出容器并解析 5555/tcp 的宿主端口映射 → serial = <host>:<port>
2. 端口扫描：以上都拿不到时，扫描监听在 adb 端口段的 TCP 端口（默认 5555-5699）

为什么必须加 adb devices：docker 方式依赖 docker 权限且只看得到容器，
端口扫描只认 5555 段。而 117/122 这两类服务器既没有 docker 权限、
adb 也不在 5555 段上监听，两种旧方式都会"连得上但一台都发现不到"。

用途：管理员在「设备管理 → 服务器 +」自助登记一台服务器，系统即可把上面的
云手机批量接入看板。
"""
import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor

DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_CMD_TIMEOUT = 20
MAX_MODEL_PROBE = 32          # 最多并发探测机型的前 N 台（防止重负载）


class SshError(Exception):
    """SSH 连接/执行失败。"""


def build_kwargs(host, port=22, username="root", secret="", auth_type="password",
                 timeout=DEFAULT_CONNECT_TIMEOUT):
    """组装 paramiko.connect 参数。"""
    kwargs = {
        "hostname": host,
        "port": int(port or 22),
        "username": username or "root",
        "timeout": timeout,
        "allow_agent": False,
        "look_for_keys": False,
    }
    secret = secret or ""
    if auth_type == "key":
        if not secret:
            raise SshError("私钥模式必须提供私钥路径")
        if not os.path.isfile(secret):
            raise SshError(f"私钥文件不存在：{secret}")
        kwargs["key_filename"] = secret
    else:
        if not secret:
            raise SshError("密码模式必须提供密码")
        kwargs["password"] = secret
    return kwargs


def ssh_connect(**kwargs):
    """建立 SSH 连接，失败抛 SshError。"""
    try:
        import paramiko
    except ImportError as e:  # pragma: no cover
        raise SshError("后端缺少 paramiko，无法 SSH 连接（pip install paramiko）") from e
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(**kwargs)
    except socket.timeout as e:
        client.close()
        raise SshError(f"连接超时：{kwargs.get('hostname')}:{kwargs.get('port')}") from e
    except Exception as e:  # noqa: BLE001 - paramiko 异常类型较多，统一包一层
        client.close()
        raise SshError(f"SSH 连接失败：{e}") from e
    return client


def exec_cmd(client, cmd, timeout=DEFAULT_CMD_TIMEOUT):
    """执行远程命令，返回 (rc, stdout, stderr)。"""
    try:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err
    except Exception as e:  # noqa: BLE001
        raise SshError(f"执行命令失败：{cmd[:60]}... ({e})") from e


# ---------------------------------------------------------------------------
# 端口映射解析
# ---------------------------------------------------------------------------
def parse_adb_port(ports_field, container_port=5555):
    """从 docker 的 Ports 字段解析宿主机映射端口。

    支持形如：
        0.0.0.0:7012->5555/tcp, :::7012->5555/tcp
        [::]:7012->5555/tcp
    找不到返回 None。
    """
    target = f"->{container_port}/tcp"
    for part in (ports_field or "").split(","):
        part = part.strip()
        if not part.endswith(target):
            continue
        left = part[: -len(target)].strip()          # 形如 0.0.0.0:7012 / :::7012 / [::]:7012
        m = re.search(r"(\d+)$", left)
        if m:
            return int(m.group(1))
    return None


def parse_listen_ports(text, port_start, port_end):
    """从 ss -ltn / netstat -ltn 输出解析落在 [port_start, port_end] 的监听端口。"""
    ports = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("state", "netid", "active", "proto")):
            continue
        # ss 输出： LISTEN 0 4096 0.0.0.0:7012 0.0.0.0:*
        # netstat 输出： tcp 0 0 0.0.0.0:7012 0.0.0.0:* LISTEN
        for chunk in line.split():
            m = re.match(r"^(?:\d+\.\d+\.\d+\.\d+|\[::\]|::|\*|:::)[:](\d+)$", chunk)
            if m:
                p = int(m.group(1))
                if port_start <= p <= port_end:
                    ports.add(p)
            else:
                m2 = re.match(r"^[:]+\[?::\]?[:\]]*:(\d+)$", chunk)
                if m2:
                    p = int(m2.group(1))
                    if port_start <= p <= port_end:
                        ports.add(p)
    return sorted(ports)


# ---------------------------------------------------------------------------
# adb devices 解析 + 可达性判定
# ---------------------------------------------------------------------------
_ADB_STATE_OK = {"device"}


def parse_adb_devices(text):
    """解析 `adb devices -l` 输出。

    行示例：
      D8835SUB06  device usb:2-1.2.7 product:rk3399_... model:SR_C11G_3399JD4 ...
      192.168.99.101:5555  device product:redroid_arm64 model:redroid11_arm64 ...
    """
    items = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("list of devices") or low.startswith("* daemon"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state not in ("device", "offline", "unauthorized", "recovery",
                         "sideload", "no", "connecting"):
            continue
        info = {}
        for kv in parts[2:]:
            if ":" in kv:
                k, v = kv.split(":", 1)
                info[k] = v
        items.append({
            "serial": serial,
            "state": state,
            "model": info.get("model", ""),
            "product": info.get("product", ""),
            "device": info.get("device", ""),
            "usb": info.get("usb", ""),
        })
    return items


def split_serial(serial):
    """把 serial 拆成 (host, port)；USB serial 返回 (None, None)。"""
    s = (serial or "").strip()
    if ":" not in s:
        return None, None
    host, _, port = s.rpartition(":")
    try:
        return host, int(port)
    except ValueError:
        return None, None


def probe_tcp(host, port, timeout=2.0):
    """从**本机**试探 host:port 是否可连（判定能否直接 adb connect）。"""
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parse_adb_forwards(text):
    """解析 `adb forward --list`，返回 {serial: host_port}。

    行示例： C4DFCSUB05 tcp:16002 tcp:5555

    ⚠️ 只认**远端是 5555** 的转发。板子上还有别的转发（例如
    `tcp:60010 tcp:9100` 给 Prometheus 采集 node_exporter 用），
    原实现忽略了远端端口，把 60010 当成 adb 端口 → 中继连到 9100 →
    adb 永远 offline，设备在平台上一直挂 error。
    """
    mapping = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        serial, local, remote = parts[0], parts[1], parts[2]
        if ":" not in local or ":" not in remote:
            continue
        try:
            remote_port = int(remote.rsplit(":", 1)[1])
            local_port = int(local.rsplit(":", 1)[1])
        except ValueError:
            continue
        if remote_port != 5555:        # 只认 adb，别的转发（9100 指标等）跳过
            continue
        mapping[serial] = local_port
    return mapping


def ensure_adb_forward(client, serial, used_ports=None, min_port=19001, max_port=19100):
    """在服务器上为 USB 设备补一条 `adb forward tcp:P tcp:5555`，返回端口 P。

    USB 直连板子在服务器上只有 USB serial，网络上没有地址；必须先 forward
    出一个服务器本机端口，之后才能经 SSH 中继给平台用。
    """
    used = set(used_ports or ())
    for p in range(min_port, max_port):
        if p in used:
            continue
        try:
            rc, _out, _err = exec_cmd(
                client, f"adb -s {serial} forward tcp:{p} tcp:5555", timeout=15)
        except SshError:
            return None
        if rc == 0:
            return p
    return None


def classify_access(item, forwards, host_cache=None, probe_timeout=2.0):
    """判定本机如何访问这台设备，返回 (mode, target_host, target_port)。

    mode:
      direct  本机可直接 adb connect（serial 已是 host:port 且本机可达）
      remote  设备在服务器侧网段（如 122 的 192.168.99.x），本机不可达，
              需经 SSH 隧道中继：让 SSH 直连 target_host:target_port 即可
      usb     USB 直连板子，服务器上看得见、网络上没有地址，
              需在服务器上 adb forward 出一个端口再中继
    """
    host_cache = host_cache if host_cache is not None else {}
    serial = item["serial"]
    host, port = split_serial(serial)

    if host and port:
        # 127.0.0.1:xxxx 指的是服务器自己，本机必须经隧道
        if host in ("127.0.0.1", "localhost", "::1"):
            return "remote", host, port
        if host not in host_cache:
            host_cache[host] = probe_tcp(host, port, probe_timeout)
        return ("direct" if host_cache[host] else "remote"), host, port

    # USB serial：看服务器上是否已建过 forward
    p = forwards.get(serial)
    if p:
        return "usb", "127.0.0.1", p
    return "usb", None, None


def precheck_hosts(items, timeout=2.0, workers=16):
    """并发探测各设备 host 在本机是否可达，返回 {host: bool}（避免串行等待超时）。"""
    hosts = set()
    for it in items:
        h, p = split_serial(it["serial"])
        if h and p and h not in ("127.0.0.1", "localhost", "::1"):
            hosts.add(h)

    cache: dict[str, bool] = {}

    def _one(h):
        # 取该 host 上任一端口试探即可（同网段通常同结果）
        port = next((split_serial(it["serial"])[1] for it in items
                     if split_serial(it["serial"])[0] == h), None)
        return h, probe_tcp(h, port, timeout)

    if hosts:
        try:
            with ThreadPoolExecutor(max_workers=min(workers, len(hosts))) as pool:
                for h, ok in pool.map(_one, sorted(hosts)):
                    cache[h] = ok
        except Exception:  # noqa: BLE001
            pass
    return cache


# ---------------------------------------------------------------------------
# 机型探测（best-effort）
# ---------------------------------------------------------------------------
def _probe_container_info(client, container, timeout=8):
    """在容器内取机型/安卓版本，失败返回空串。"""
    cmd = (
        f"docker exec {container} sh -c "
        f"'getprop ro.product.model; getprop ro.product.brand; getprop ro.build.version.release' "
        f"2>/dev/null"
    )
    try:
        rc, out, _ = exec_cmd(client, cmd, timeout=timeout)
    except SshError:
        return "", "", ""
    if rc != 0:
        return "", "", ""
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    model = lines[0] if len(lines) > 0 else ""
    brand = lines[1] if len(lines) > 1 else ""
    android = lines[2] if len(lines) > 2 else ""
    # getprop 不存在时输出为空或命令未找到的错误文本，统一判空
    for v in (model, brand, android):
        if v and ("not found" in v.lower() or "error" in v.lower()):
            v = ""
    if model.lower() in ("", "unknown"):
        model = ""
    return model, brand, android


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def probe_server(params, probe_models=True):
    """SSH 连接服务器并发现云手机。

    params: dict(host, port, username, auth_type, secret, adb_port_start, adb_port_end, container_prefix)
    返回 dict：
        {
          online: bool, hostname, kernel, docker: bool, source: 'docker'|'port-scan'|'',
          devices: [{serial, name, container, status, port, model, brand, android}],
          message: ''   # 提示信息
        }
    """
    host = (params.get("host") or "").strip()
    try:
        kwargs = build_kwargs(
            host,
            port=params.get("port") or 22,
            username=params.get("username") or "root",
            secret=params.get("secret") or "",
            auth_type=params.get("auth_type") or "password",
        )
    except SshError as e:
        return {"online": False, "devices": [], "message": str(e)}

    p_start = int(params.get("adb_port_start") or 5555)
    p_end = int(params.get("adb_port_end") or 5699)
    prefix = (params.get("container_prefix") or "redroid").strip()

    client = None
    try:
        client = ssh_connect(**kwargs)
        rc, out, _ = exec_cmd(client, "hostname; uname -sr", timeout=15)
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        hostname = lines[0] if lines else ""
        kernel = lines[1] if len(lines) > 1 else ""

        devices = []
        source = ""
        docker_ok = False
        docker_err = ""
        prefix_hits = 0

        # ---- 方式 0：adb devices（最通用，优先级最高）----
        # 服务器自己的 adb 能看到什么，云手机就是什么；覆盖 USB 直连、网络 ADB、
        # redroid 容器、本机 forward 端口等所有形态。
        try:
            rc_a, out_a, _ = exec_cmd(client, "adb devices -l 2>&1", timeout=30)
            adb_items = parse_adb_devices(out_a) if rc_a == 0 else []
        except SshError:
            adb_items = []

        usable = [it for it in adb_items if it["state"] in _ADB_STATE_OK]
        if usable:
            try:
                rc_f, out_f, _ = exec_cmd(client, "adb forward --list 2>&1", timeout=20)
                forwards = parse_adb_forwards(out_f)
            except SshError:
                forwards = {}
            host_cache = precheck_hosts(usable)
            for it in usable:
                mode, th, tp = classify_access(it, forwards, host_cache)
                # USB 设备若服务器上还没建 forward，补一条，让它有可中继的地址
                if mode == "usb" and not tp:
                    p = ensure_adb_forward(client, it["serial"],
                                           used_ports=set(forwards.values()))
                    if p:
                        th, tp = "127.0.0.1", p
                        forwards[it["serial"]] = p
                devices.append({
                    "serial": it["serial"],
                    "name": it["model"] or it["serial"],
                    "container": "",
                    "status": "running",
                    "port": tp or 0,
                    "image": "",
                    "model": it["model"],
                    "brand": "",
                    "android": "",
                    "access": mode,             # direct / remote / usb
                    "target_host": th or "",
                    "target_port": tp or 0,
                    "usb": it["usb"],
                    "product": it["product"],
                })
            source = "adb"

        # ---- 方式 1：docker 容器 ----
        if not devices:
            fmt = r"{{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Image}}\t{{.State}}"
            ps_cmd = f"docker ps -a --format '{fmt}'"
            rc, out, err = exec_cmd(client, ps_cmd, timeout=30)
            if rc != 0 or not out.strip():
                # 常见情况：用户不在 docker 组，改用免密 sudo 再试一次
                docker_err = (err or "").strip().splitlines()[0] if (err or "").strip() else ""
                rc2, out2, err2 = exec_cmd(client, f"sudo -n {ps_cmd}", timeout=30)
                if rc2 == 0:
                    rc, out, err = rc2, out2, err2
                    docker_err = ""
            if rc == 0:
                # rc=0 表示 docker 可读；容器列表可能为空（服务器尚未部署云手机）
                docker_ok = True
                for line in out.strip().splitlines():
                    parts = line.split("\t")
                    if len(parts) < 3:
                        continue
                    name, cstatus, ports = parts[0].strip(), parts[1].strip(), parts[2].strip()
                    image = parts[3].strip() if len(parts) > 3 else ""
                    state = parts[4].strip() if len(parts) > 4 else ""
                    # 前缀过滤：容器名前缀 或 镜像名包含关键字（前缀为空则全部容器）
                    hit = False
                    if prefix:
                        hit = name.lower().startswith(prefix.lower()) or (prefix.lower() in image.lower())
                    else:
                        hit = True
                    if not hit:
                        continue
                    prefix_hits += 1
                    port = parse_adb_port(ports)
                    if not port:
                        continue
                    if not (p_start <= port <= p_end) and port not in (5555,):
                        # 端口范围外的映射也保留（用户常用 7012-7016 等自定义段）
                        pass
                    devices.append({
                        "serial": f"{host}:{port}",
                        "name": name,
                        "container": name,
                        "status": "running" if state.lower() == "running" or cstatus.lower().startswith("up") else "stopped",
                        "port": port,
                        "image": image,
                        "model": "",
                        "brand": "",
                        "android": "",
                        "access": "direct",
                        "target_host": "",
                        "target_port": 0,
                        "usb": "",
                        "product": "",
                    })
                source = "docker"

        # ---- 方式 2：端口扫描（docker 不可用或没找到容器）----
        port_scanned = False
        if not devices:
            listed = False
            for cmd in ("ss -ltnH", "ss -ltn", "netstat -ltn"):
                try:
                    rc2, out2, _ = exec_cmd(client, cmd, timeout=20)
                except SshError:
                    continue
                if rc2 == 0 and out2.strip():
                    listed = True
                    port_scanned = True
                    for p in parse_listen_ports(out2, p_start, p_end):
                        devices.append({
                            "serial": f"{host}:{p}",
                            "name": f"{host}-{p}",
                            "container": "",
                            "status": "running",
                            "port": p,
                            "image": "",
                            "model": "",
                            "brand": "",
                            "android": "",
                            "access": "direct",
                            "target_host": "",
                            "target_port": 0,
                            "usb": "",
                            "product": "",
                        })
                    break
            if devices:
                source = "port-scan"
            elif not docker_ok and not listed:
                return {
                    "online": True, "hostname": hostname, "kernel": kernel,
                    "docker": False, "source": "", "devices": [],
                    "message": f"服务器已连接，但读取云手机失败：{docker_err or 'Docker 不可用，且无法读取监听端口'}",
                }

        devices.sort(key=lambda d: d["port"])

        # ---- 机型探测（并发、best-effort）----
        if probe_models and source == "docker" and devices:
            targets = devices[:MAX_MODEL_PROBE]

            def _fill(item):
                if not item["container"]:
                    return
                model, brand, android = _probe_container_info(client, item["container"])
                item["model"], item["brand"], item["android"] = model, brand, android

            try:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(_fill, targets))
            except Exception:  # noqa: BLE001 - 机型探测失败不影响主体结果
                pass

        msg = ""
        if not devices:
            msg = "已连接服务器，但未发现云手机："
            if docker_ok and prefix_hits:
                msg += f"匹配前缀「{prefix}」的容器 {prefix_hits} 个，但都没有 5555/tcp 端口映射"
            elif docker_ok:
                msg += f"未找到匹配前缀「{prefix}」的容器，可尝试修改容器前缀"
                if port_scanned:
                    msg += f"（已同时扫描端口 {p_start}-{p_end}，无监听）"
            elif docker_err:
                msg += f"Docker 读取失败（{docker_err}），请用 root 或具有 docker 权限的账号"
            else:
                msg += f"未检测到 Docker，且端口 {p_start}-{p_end} 无监听"
        if len(devices) > MAX_MODEL_PROBE and probe_models:
            msg += f"（云手机数量较多，仅前 {MAX_MODEL_PROBE} 台探测了机型）"
        return {
            "online": True,
            "hostname": hostname,
            "kernel": kernel,
            "docker": docker_ok,
            "source": source,
            "devices": devices,
            "message": msg,
        }
    except SshError as e:
        return {"online": False, "devices": [], "message": str(e)}
    finally:
        if client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def test_connection(params):
    """仅测试 SSH 能否连通并拿到基本信息。返回 (ok, info_dict)。"""
    try:
        kwargs = build_kwargs(
            (params.get("host") or "").strip(),
            port=params.get("port") or 22,
            username=params.get("username") or "root",
            secret=params.get("secret") or "",
            auth_type=params.get("auth_type") or "password",
        )
    except SshError as e:
        return False, {"message": str(e)}
    client = None
    try:
        client = ssh_connect(**kwargs)
        probe = (
            "hostname; uptime; "
            "docker --version 2>/dev/null || echo 'no-docker'; "
            "docker ps >/dev/null 2>&1 && echo 'docker-ok' "
            "|| (sudo -n docker ps >/dev/null 2>&1 && echo 'docker-sudo-ok' || echo 'docker-denied')"
        )
        rc, out, err = exec_cmd(client, probe, timeout=25)
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        version = lines[2] if len(lines) > 2 else ""
        has_docker = bool(version and version != "no-docker")
        perm = lines[3] if len(lines) > 3 else ""
        can_list = perm in ("docker-ok", "docker-sudo-ok")
        info = {
            "hostname": lines[0] if lines else "",
            "uptime": lines[1] if len(lines) > 1 else "",
            "docker_version": version if has_docker else "",
            "has_docker": has_docker,
            "docker_access": perm or "",
            "message": "连接成功",
        }
        if has_docker and not can_list:
            info["message"] = "连接成功，但当前账号无权执行 docker ps（请用 root 或加入 docker 组）"
        elif not has_docker:
            info["message"] = "连接成功，但该服务器未安装 Docker（发现时会改用端口扫描）"
        return True, info
    except SshError as e:
        return False, {"message": str(e)}
    finally:
        if client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
