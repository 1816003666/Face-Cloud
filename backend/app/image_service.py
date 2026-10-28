# -*- coding: utf-8 -*-
"""云手机系统镜像服务层：SSH → 服务器上的 Docker 镜像操作。

设计要点
--------
1. 镜像实体活在**云手机服务器**上（docker image），平台数据库只记录元数据
   （在哪个 host、repo:tag、大小、来源）。
2. 复用 ssh_probe 的连接/执行层；docker 命令优先直接执行，遇到权限不足再退
   到 sudo -n docker —— 和 ssh_probe.probe_server 的判定保持一致
   （117/122 这类机器 firefly 不在 docker 组，必须走 sudo）。
3. **绝不返回假成功**：命令失败一律抛 ImageError，消息可直接展示给用户。
"""
from __future__ import annotations

import os
import re
import shlex
import time

from flask import current_app

from . import ssh_probe


class ImageError(Exception):
    """镜像操作失败（消息可直接回给前端）。"""


def _cfg(key, default=None):
    try:
        return current_app.config.get(key, default)
    except RuntimeError:                     # 无应用上下文（脚本/测试）
        return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def server_kwargs(server) -> dict:
    """Server 模型 → paramiko 连接参数。"""
    return ssh_probe.build_kwargs(
        server.host,
        port=server.port or 22,
        username=server.username or "root",
        secret=server.secret or "",
        auth_type=server.auth_type or "password",
    )


def connect(server):
    """连接服务器，失败抛 ImageError。"""
    try:
        return ssh_probe.ssh_connect(**server_kwargs(server))
    except ssh_probe.SshError as e:
        raise ImageError(str(e)) from e


def docker_timeout() -> int:
    return int(_cfg("IMAGE_DOCKER_TIMEOUT", 1800))


def docker(client, args: str, timeout=None) -> tuple:
    """执行 docker 子命令；直接跑没权限时退到 sudo -n。返回 (rc, out, err)。"""
    t = timeout or docker_timeout()
    rc, out, err = ssh_probe.exec_cmd(client, "docker " + args, timeout=t)
    blob = ((err or "") + (out or "")).lower()
    if rc != 0 and ("permission denied" in blob or "docker.sock" in blob):
        rc, out, err = ssh_probe.exec_cmd(client, "sudo -n docker " + args, timeout=t)
    return rc, out, err


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def docker_or_raise(client, args: str, what: str = "", timeout=None) -> str:
    rc, out, err = docker(client, args, timeout)
    if rc != 0:
        raise ImageError("%s失败：%s" % (what or "docker 命令",
                                        _first_line(err or out) or ("rc=%d" % rc)))
    return out


SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,180}$")


def check_ref(ref: str) -> str:
    """校验 docker 镜像引用，避免把用户输入直接拼进 shell。"""
    if not SAFE_REF.match(ref or ""):
        raise ImageError("非法的镜像引用：%s" % (ref or "(空)"))
    return ref


# ---------------------------------------------------------------------------
# 容器 / 镜像查询
# ---------------------------------------------------------------------------
def list_containers(client) -> list:
    """docker ps -a，解析为 [{name, image, ports, status, host_port}]。"""
    fmt = "{{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}"
    rc, out, err = docker(client, "ps -a --format '%s'" % fmt, timeout=60)
    if rc != 0:
        raise ImageError("读取容器列表失败：%s" % (_first_line(err or out) or ("rc=%d" % rc)))
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        m = re.search(r":(\d+)->5555", parts[2]) or re.search(r":(\d+)->", parts[2])
        rows.append({
            "name": parts[0].strip(),
            "image": parts[1].strip(),
            "ports": parts[2].strip(),
            "status": parts[3].strip(),
            "host_port": int(m.group(1)) if m else None,
        })
    return rows


def container_for_port(client, port: int):
    """按宿主端口找容器名（一台云手机 = 一个 5555 端口映射）。"""
    for c in list_containers(client):
        if c["host_port"] == int(port):
            return c["name"]
    return None


def image_size_bytes(client, ref: str) -> int:
    rc, out, _ = docker(client, "image inspect -f '{{.Size}}' %s" % shlex.quote(check_ref(ref)),
                        timeout=60)
    txt = (out or "").strip()
    return int(txt) if rc == 0 and txt.isdigit() else 0


def list_server_images(client) -> list:
    """服务器上已有的 docker 镜像（只读，用于展示/校验）。"""
    rc, out, err = docker(client,
                          "images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.ID}}'",
                          timeout=60)
    if rc != 0:
        raise ImageError("读取镜像列表失败：%s" % (_first_line(err or out) or ("rc=%d" % rc)))
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append({"ref": parts[0].strip(), "size": parts[1].strip(), "id": parts[2].strip()})
    return rows


# ---------------------------------------------------------------------------
# 写操作
# ---------------------------------------------------------------------------
def commit_container(client, container: str, ref: str) -> None:
    """把容器提交成镜像 —— 这就是「从设备创建镜像」。"""
    docker_or_raise(
        client,
        "commit %s %s" % (shlex.quote(container), shlex.quote(check_ref(ref))),
        what="为容器 %s 创建镜像" % container,
        timeout=docker_timeout(),
    )


def remove_image(client, ref: str) -> None:
    docker_or_raise(client, "rmi %s" % shlex.quote(check_ref(ref)), what="删除镜像", timeout=300)


def run_redroid(client, name: str, port: int, ref: str) -> None:
    from .orchestrator import redroid_boot_args
    if int(port) % 2 == 0:
        bm = "-v /dev/binder1:/dev/binder -v /dev/hwbinder1:/dev/hwbinder -v /dev/vndbinder1:/dev/vndbinder "
    else:
        bm = "-v /dev/binder:/dev/binder "
    base = (
        "run -itd --privileged "
        "--name %s "
        "-p %d:5555 "
        + bm +
        "-v /dev/ashmem:/dev/ashmem "
        "-v ~/redroid-data/%d:/data "
        "%s "
    )
    cmd = base % (shlex.quote(name), int(port), int(port), shlex.quote(check_ref(ref)))
    cmd += redroid_boot_args(int(port))
    docker_or_raise(client, cmd, what="启动容器 %s" % name, timeout=docker_timeout())


def remove_container(client, name: str) -> None:
    docker_or_raise(client, "rm -f %s" % shlex.quote(name), what="删除容器 %s" % name, timeout=180)


def wipe_data_dir(client, port: int) -> None:
    """清空该设备的数据卷目录（部署时选择清空数据用）。"""
    ssh_probe.exec_cmd(client, "rm -rf ~/redroid-data/%d" % int(port), timeout=120)


def free_ports(client, start: int, end: int, count: int) -> list:
    """在 [start, end] 里挑 count 个未被容器占用的宿主端口。"""
    used = {c["host_port"] for c in list_containers(client) if c["host_port"]}
    ports = [p for p in range(int(start), int(end) + 1) if p not in used][:int(count)]
    if len(ports) < int(count):
        raise ImageError("服务器可用端口不足：需要 %d 个，%d-%d 内只剩 %d 个"
                         % (count, start, end, len(ports)))
    return ports


# ---------------------------------------------------------------------------
# 镜像包传输（上传 / 下载）
# ---------------------------------------------------------------------------
def _remote_tmp(prefix: str) -> str:
    return "/tmp/%s-%d-%d.tar" % (prefix, os.getpid(), int(time.time()))


def load_image_from_file(client, local_path: str) -> str:
    """把本地镜像包传到服务器并 docker load，返回 docker 的输出（用于解析镜像名）。"""
    remote = _remote_tmp("cpimg-load")
    try:
        sftp = client.open_sftp()
        try:
            sftp.put(local_path, remote)
        finally:
            sftp.close()
        return docker_or_raise(client, "load -i %s" % shlex.quote(remote),
                               what="导入镜像包", timeout=docker_timeout())
    finally:
        try:
            ssh_probe.exec_cmd(client, "rm -f %s" % shlex.quote(remote), timeout=60)
        except Exception:      # noqa: BLE001 - 清理失败不影响结果
            pass


def save_image_to_file(client, ref: str, local_path: str) -> None:
    """把服务器上的镜像导出成 tar 拉到本地。"""
    remote = _remote_tmp("cpimg-save")
    try:
        docker_or_raise(client, "save -o %s %s" % (shlex.quote(remote), shlex.quote(check_ref(ref))),
                        what="导出镜像", timeout=docker_timeout())
        sftp = client.open_sftp()
        try:
            sftp.get(remote, local_path)
        finally:
            sftp.close()
    finally:
        try:
            ssh_probe.exec_cmd(client, "rm -f %s" % shlex.quote(remote), timeout=60)
        except Exception:      # noqa: BLE001
            pass

# ---------------------------------------------------------------------------
# 真机（板子）分区镜像
# ---------------------------------------------------------------------------
# 白名单；safe=False 表示运行中被 dm-* 占用，**不能在线写回**（会把设备写坏）
BOARD_WORKDIR = "/data/cpimg"
DEVICE_PARTITIONS = {
    "boot":     {"safe": True,  "desc": "内核 + ramdisk"},
    "vbmeta":   {"safe": True,  "desc": "AVB 校验元数据"},
    "dtbo":     {"safe": True,  "desc": "设备树 overlay"},
    "recovery": {"safe": True,  "desc": "recovery 分区"},
    "super":    {"safe": False, "desc": "动态分区（system/vendor/product/odm）"},
}


def board_connect(host: str, **kwargs):
    """连接真机板子（root SSH）。kwargs 透传给 ssh_probe.build_kwargs。"""
    try:
        return ssh_probe.ssh_connect(**ssh_probe.build_kwargs(host, **kwargs))
    except ssh_probe.SshError as e:
        raise ImageError(str(e)) from e


def _sh(client, cmd: str, timeout: int = 600):
    rc, out, err = ssh_probe.exec_cmd(client, cmd, timeout=timeout)
    return rc, (out or "").strip(), (err or "").strip()


def board_partition_info(client) -> dict:
    """板子分区表：{name: {device, size_mb, in_use}}。

    in_use 判定：
      - 该块设备出现在 mount 里，或
      - 它是某个 dm-* 的底层设备（super 就是这种情况）
    """
    rc, out, err = _sh(
        client,
        "for n in boot recovery vbmeta dtbo super userdata; do "
        "p=$(readlink -f /dev/block/by-name/$n 2>/dev/null); [ -n \"$p\" ] || continue; "
        "printf '%s|%s|%s\\n' \"$n\" \"$p\" \"$(cat /sys/class/block/$(basename $p)/size 2>/dev/null)\"; "
        "done",
        timeout=60)
    if rc != 0:
        raise ImageError("读取分区表失败：%s" % (err or out or ("rc=%d" % rc)))

    rc2, mnt, _ = _sh(client, "mount | awk '{print $1}'", timeout=60)
    mounted = set((mnt or "").split())
    rc3, slaves, _ = _sh(
        client,
        "for d in /sys/block/dm-*; do [ -d $d ] || continue; "
        "basename $(readlink -f $d/slaves/* 2>/dev/null | head -1) 2>/dev/null; done",
        timeout=60)
    backing = set(x for x in (slaves or "").split() if x)

    info = {}
    for line in (out or "").splitlines():
        f = line.split("|")
        if len(f) != 3:
            continue
        name, dev, sz = f[0], f[1], f[2]
        base = dev.rsplit("/", 1)[-1]
        try:
            size_mb = int(sz) // 2048
        except ValueError:
            size_mb = 0
        info[name] = {
            "device": dev,
            "size_mb": size_mb,
            "in_use": (dev in mounted) or (base in backing),
        }
    return info


def dump_partitions(client, parts: list, tag: str = None, on_progress=None,
                    total_steps: int = None) -> tuple:
    """在板子上 dd 出各分区并打包成 tar.gz。

    返回 (板子上的包路径, 包内文件名列表)。只读操作，安全。

    on_progress(done, total, stage)：done 是"已完成第几步"。
    整个导出在调用方（路由）那里算 len(parts)+2 步：N 次 dd + 打包 + 拉回，
    所以这里默认按同样的总数报，最后一步（拉回）由路由补上。
    """
    total_steps = int(total_steps or (len(parts) + 2))

    def _rep(done, stage):
        if on_progress is not None:
            try:
                on_progress(done, total_steps, stage)
            except Exception:                  # noqa: BLE001
                pass

    bad = [p for p in parts if p not in DEVICE_PARTITIONS]
    if bad:
        raise ImageError("不支持的分区：%s" % "、".join(bad))
    if not parts:
        raise ImageError("请至少选择一个分区")
    _sh(client, "mkdir -p %s" % shlex.quote(BOARD_WORKDIR), timeout=60)
    tag = tag or ("dump-%d" % int(time.time()))
    remote = "%s/%s.tar.gz" % (BOARD_WORKDIR, tag)

    imgs = []
    for _i, p in enumerate(parts):
        _rep(_i, "导出分区 %s（%d/%d）" % (p, _i + 1, len(parts)))
        f = "%s/%s.img" % (BOARD_WORKDIR, p)
        rc, out, err = _sh(
            client,
            "dd if=/dev/block/by-name/%s of=%s bs=4M 2>&1 | tail -1"
            % (shlex.quote(p), shlex.quote(f)),
            timeout=docker_timeout())
        if rc != 0:
            raise ImageError("导出分区 %s 失败：%s" % (p, err or out))
        imgs.append(os.path.basename(f))

    _rep(len(parts), "打包压缩（tar.gz）")
    rc, out, err = _sh(
        client,
        "cd %s && tar -czf %s %s && rm -f %s"
        % (shlex.quote(BOARD_WORKDIR), shlex.quote(remote),
           " ".join(shlex.quote(x) for x in imgs),
           " ".join(shlex.quote("%s/%s" % (BOARD_WORKDIR, x)) for x in imgs)),
        timeout=docker_timeout())
    if rc != 0:
        raise ImageError("打包失败：%s" % (err or out))
    return remote, imgs


def board_file_size(client, remote: str) -> int:
    rc, out, _ = _sh(client, "stat -c %%s %s 2>/dev/null || echo 0" % shlex.quote(remote), timeout=60)
    try:
        return int((out or "0").strip())
    except ValueError:
        return 0


def pull_board_file(client, remote: str, local_path: str, on_bytes=None) -> None:
    """从板子把包拉回平台。on_bytes(已传, 总大小) 可选，用来报"传到 42%"。"""
    sftp = client.open_sftp()
    try:
        if on_bytes is None:
            sftp.get(remote, local_path)
        else:
            def _cb(transferred, total):
                try:
                    on_bytes(int(transferred or 0), int(total or 0))
                except Exception:              # noqa: BLE001 - 进度回调不能影响传输
                    pass
            sftp.get(remote, local_path, callback=_cb)
    finally:
        sftp.close()


def restore_partitions(client, local_archive: str, parts: list, on_progress=None,
                       base: int = 0, total_steps: int = None) -> None:
    """把镜像包推到板子并写回指定分区。

    **拒绝**写回正在被占用的分区（super）——运行中写它会导致设备损坏。

    on_progress(done, total, stage)：多台板子时调用方用 base/total_steps 把
    各台板的进度拼成一条总进度。
    """
    total_steps = int(total_steps or (len(parts) + 2))

    def _rep(done, stage):
        if on_progress is not None:
            try:
                on_progress(base + done, total_steps, stage)
            except Exception:                  # noqa: BLE001
                pass

    bad = [p for p in parts if p not in DEVICE_PARTITIONS]
    if bad:
        raise ImageError("不支持的分区：%s" % "、".join(bad))
    info = board_partition_info(client)
    danger = [p for p in parts if info.get(p, {}).get("in_use")]
    if danger:
        raise ImageError(
            "分区 %s 正在被占用（挂载中 / dm 底层设备），运行中写回会导致设备损坏。"
            "请先让设备进入可写状态（recovery）后再刷。" % "、".join(danger))

    remote_dir = BOARD_WORKDIR + "/restore"
    _sh(client, "rm -rf %s && mkdir -p %s" % (shlex.quote(remote_dir), shlex.quote(remote_dir)),
        timeout=60)
    remote = remote_dir + "/pack.tar.gz"
    _rep(0, "上传镜像包到板子")
    sftp = client.open_sftp()
    try:
        try:
            size = os.path.getsize(local_archive)
        except OSError:
            size = 0

        def _cb(transferred, total):
            if size:
                _rep(0, "上传镜像包到板子（%d%%）" % min(100, int((transferred or 0) * 100 / size)))
        sftp.put(local_archive, remote, callback=_cb if size else None)
    finally:
        sftp.close()
    _rep(1, "解包")
    rc, out, err = _sh(client, "cd %s && tar -xzf %s" % (shlex.quote(remote_dir), shlex.quote(remote)),
                       timeout=docker_timeout())
    if rc != 0:
        raise ImageError("解包失败：%s" % (err or out))
    for _i, p in enumerate(parts):
        _rep(2 + _i, "写回分区 %s（%d/%d）" % (p, _i + 1, len(parts)))
        img = "%s/%s.img" % (remote_dir, p)
        rc, out, err = _sh(
            client,
            "dd if=%s of=/dev/block/by-name/%s bs=4M 2>&1 | tail -1"
            % (shlex.quote(img), shlex.quote(p)),
            timeout=docker_timeout())
        if rc != 0:
            raise ImageError("写回分区 %s 失败：%s" % (p, err or out))
    _sh(client, "rm -rf %s" % shlex.quote(remote_dir), timeout=120)


def cleanup_board(client, remote: str) -> None:
    """删掉板子上导出的临时包。"""
    try:
        _sh(client, "rm -f %s" % shlex.quote(remote), timeout=120)
    except Exception:      # noqa: BLE001
        pass

