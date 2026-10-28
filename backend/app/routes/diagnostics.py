"""后端诊断模块：系统诊断、设备深度诊断、一键修复、批量修复、诊断历史。

提供完整的云手机平台健康检查与自动修复能力：
- GET  /diag/system              系统级诊断（SSH / Docker / ADB / 服务器资源）
- GET  /diag/device/<id>         单设备深度诊断（延迟 / 截图 / 指标 / 网络）
- POST /diag/device/<id>/fix     一键修复单台设备（自动尝试多种策略）
- POST /diag/batch-fix           批量修复离线/异常设备
- GET  /diag/history             诊断历史查询
"""
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from flask import Blueprint, g, jsonify, request

from .. import orchestrator
from ..auth import login_required, require_platform, require_role
from ..extensions import db
from ..models import (
    Device, DiagnosticReport, MaintenanceLog, record_audit, iso_utc)

diag_bp = Blueprint("diagnostics", __name__)


# ────────────────────────── 内部工具 ──────────────────────────

def _device_port(serial: str) -> int | None:
    """从 serial（如 192.168.1.100:5555）中提取端口号。"""
    if not serial or ":" not in serial:
        return None
    try:
        return int(serial.split(":")[-1])
    except (TypeError, ValueError):
        return None


def _safe_float(val, default=0.0):
    """安全地将值转为 float。"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ────────────────────────── 系统诊断子模块 ──────────────────────────

def _check_ssh() -> dict:
    """SSH 连通性检测：建立连接并测量延迟。"""
    result = {"ok": False, "latency_ms": None, "server": "", "error": ""}
    if not orchestrator._ssh_enabled():
        # 本部署用「servers 表 + 跳板机」管理多台服务器，没有全局 SSH 配置；
        # 这里不算故障，改由 servers 巡检结果说明问题（避免每次诊断都刷一条误报）。
        result["mode"] = "per-server"
        return result
    try:
        result["server"] = orchestrator._server_ip()
        t0 = time.monotonic()
        client = orchestrator._ssh_connect()
        try:
            # echo 验证连通性
            rc, out, err = orchestrator._ssh_exec(client, "echo OK", timeout=5)
            latency = round((time.monotonic() - t0) * 1000, 1)
            if rc == 0:
                result["ok"] = True
                result["latency_ms"] = latency
            else:
                result["error"] = err.strip() or "SSH echo 失败"
        finally:
            client.close()
    except Exception as e:
        result["error"] = str(e)
    return result


def _check_docker() -> dict:
    """获取 Docker 容器运行状态。"""
    result = {"running": 0, "total": 0, "containers": [], "error": ""}
    if not orchestrator._ssh_enabled():
        result["mode"] = "per-server"
        return result
    try:
        containers = orchestrator.list_remote_containers()
        result["running"] = sum(1 for c in containers if "Up" in c.get("status", ""))
        result["total"] = len(containers)
        result["containers"] = containers
    except Exception as e:
        result["error"] = str(e)
    return result


def _check_adb() -> dict:
    """检测本地 + 远端 ADB 设备。"""
    result = {
        "local_count": 0, "remote_count": 0,
        "local_devices": [], "remote_devices": [], "error": "",
    }
    try:
        local_devs = orchestrator.list_adb_devices()
        result["local_count"] = len(local_devs)
        result["local_devices"] = [
            {"serial": d.get("serial", ""), "status": d.get("status", "")}
            for d in local_devs[:30]
        ]
    except Exception as e:
        result["error"] = f"本地 ADB: {e}"

    if orchestrator._ssh_enabled():
        try:
            remote_devs = orchestrator.list_remote_adb_devices()
            result["remote_count"] = len(remote_devs)
            result["remote_devices"] = [
                {"serial": d.get("serial", ""), "status": d.get("status", "")}
                for d in remote_devs[:30]
            ]
        except Exception as e:
            result["error"] = (result["error"] + "; " if result["error"] else "") + f"远端 ADB: {e}"
    return result


def _check_server_resources() -> dict:
    """通过 SSH 采集服务器资源：负载 / 内存 / 磁盘。"""
    resources = {
        "ok": False, "load_avg": "", "memory": "", "disk": "",
        "memory_percent": None, "disk_percent": None, "error": "",
    }
    if not orchestrator._ssh_enabled():
        resources["error"] = "SSH 未配置，无法采集远程服务器资源"
        return resources
    try:
        client = orchestrator._ssh_connect()
        try:
            # 负载均值
            rc, out, _ = orchestrator._ssh_exec(client, "cat /proc/loadavg", timeout=5)
            resources["load_avg"] = out.strip() if rc == 0 and out else ""

            # 内存使用率
            rc, out, _ = orchestrator._ssh_exec(
                client,
                "free -m | awk '/Mem:/{printf \"%.1f\", $3/$2*100}'",
                timeout=5,
            )
            if rc == 0 and out:
                resources["memory_percent"] = _safe_float(out.strip())
                resources["memory"] = f"{resources['memory_percent']}%"

            # 磁盘使用率（根分区）
            rc, out, _ = orchestrator._ssh_exec(
                client,
                "df -h / | awk 'NR==2{print $5}' | tr -d '%'",
                timeout=5,
            )
            if rc == 0 and out:
                resources["disk_percent"] = _safe_float(out.strip())
                resources["disk"] = f"{resources['disk_percent']}%"

            resources["ok"] = True
        finally:
            client.close()
    except Exception as e:
        resources["error"] = str(e)
    return resources


def _server_checks() -> list:
    """按 servers 表逐台巡检（全局 SSH 未配置时，这才是可用的诊断口径）。

    每台取：SSH 是否连通、docker 容器 运行/总数、负载、内存、磁盘。
    8 线程并发 + 5 秒超时，9 台服务器约 1~2 秒出结果。
    """
    from .. import ssh_probe
    from ..models import Server

    jump_srv = db.session.query(Server).filter(Server.host == "192.168.9.122").first()
    jump = ({"host": jump_srv.host, "port": int(jump_srv.port or 22),
             "username": jump_srv.username,
             "password": (jump_srv.secret if (jump_srv.auth_type or "password") != "key" else None)}
            if jump_srv is not None else None)

    def _one(s):
        item = {"id": s.id, "name": s.name, "host": s.host, "ssh_ok": False,
                "docker_running": 0, "docker_total": 0,
                "load": "", "memory": "", "disk": "", "error": ""}
        extra = {}
        cli = None
        try:
            cli = ssh_probe.ssh_connect(
                hostname=s.host, port=int(s.port or 22), username=s.username,
                password=(s.secret if (s.auth_type or "password") != "key" else None),
                timeout=4, allow_agent=False, look_for_keys=False)
        except Exception as e:  # noqa: BLE001
            # 99.x 内网宿主平台本机不可达，但 122 能通 → 走跳板机再试一次
            if jump is None or not str(s.host).startswith("192.168.99."):
                item["error"] = str(e)[:100]
                return item
            try:
                import paramiko
                outer = paramiko.SSHClient()
                outer.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                outer.connect(hostname=jump["host"], port=jump["port"], username=jump["username"],
                              password=jump["password"], timeout=4,
                              allow_agent=False, look_for_keys=False)
                ch = outer.get_transport().open_channel(
                    "direct-tcpip", (s.host, int(s.port or 22)), ("127.0.0.1", 0))
                cli = paramiko.SSHClient()
                cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                cli.connect(hostname=s.host, port=int(s.port or 22), username=s.username,
                            password=(s.secret if (s.auth_type or "password") != "key" else None),
                            sock=ch, timeout=4, allow_agent=False, look_for_keys=False)
                extra["via"] = "jump:192.168.9.122"
            except Exception as e2:  # noqa: BLE001
                item["error"] = f"{str(e)[:50]} / 跳板机: {str(e2)[:50]}"
                return item
        try:
            item["ssh_ok"] = True
            item.update(extra)
            cmd = ("docker ps -q 2>/dev/null | wc -l; docker ps -aq 2>/dev/null | wc -l; "
                   "cat /proc/loadavg | cut -d' ' -f1-3; "
                   "free -m | awk '/Mem:/{printf \"%.0f\", $3/$2*100}'; echo; "
                   "df -h / | awk 'NR==2{print $5}'")
            rc, out, _err = ssh_probe.exec_cmd(cli, cmd, timeout=12)
            lines = [x.strip() for x in (out or "").splitlines()]
            if rc == 0 and len(lines) >= 5:
                item["docker_running"] = int(lines[0] or 0)
                item["docker_total"] = int(lines[1] or 0)
                item["load"] = lines[2]
                item["memory"] = (lines[3] or "") + "%"
                item["disk"] = lines[4]
        except Exception as e:  # noqa: BLE001
            item["error"] = str(e)[:100]
        finally:
            try:
                cli.close()
            except Exception:  # noqa: BLE001
                pass
        return item

    rows = db.session.query(Server).order_by(Server.id).all()
    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(_one, rows):
            out.append(r)
    return out


def _build_system_report() -> dict:
    """构建完整的系统诊断报告。"""
    issues = []

    ssh_info = _check_ssh()
    docker_info = _check_docker()
    adb_info = _check_adb()
    resources = _check_server_resources()

    # 收集问题
    if not ssh_info["ok"] and ssh_info.get("mode") != "per-server":
        issues.append({"level": "warning", "message": f"SSH 连接失败: {ssh_info['error']}"})
    elif ssh_info["latency_ms"] and ssh_info["latency_ms"] > 500:
        issues.append({"level": "warning", "message": f"SSH 延迟偏高: {ssh_info['latency_ms']}ms"})

    if docker_info["error"] and docker_info.get("mode") != "per-server":
        issues.append({"level": "error", "message": f"Docker: {docker_info['error']}"})
    elif docker_info["running"] == 0 and docker_info["total"] > 0:
        issues.append({"level": "critical", "message": "所有 Docker 容器均已停止"})
    elif docker_info["running"] < docker_info["total"]:
        issues.append({
            "level": "warning",
            "message": f"部分容器未运行: {docker_info['running']}/{docker_info['total']}",
        })

    if resources.get("memory_percent") and resources["memory_percent"] > 90:
        issues.append({"level": "critical", "message": f"服务器内存 {resources['memory_percent']}%，超出 90% 阈值"})
    elif resources.get("memory_percent") and resources["memory_percent"] > 80:
        issues.append({"level": "warning", "message": f"服务器内存 {resources['memory_percent']}%"})

    if resources.get("disk_percent") and resources["disk_percent"] > 90:
        issues.append({"level": "critical", "message": f"服务器磁盘 {resources['disk_percent']}%，超出 90% 阈值"})
    elif resources.get("disk_percent") and resources["disk_percent"] > 80:
        issues.append({"level": "warning", "message": f"服务器磁盘 {resources['disk_percent']}%"})

    # 全局 SSH 未配置时，改用「按服务器巡检」作为诊断口径
    servers = _server_checks()
    if ssh_info.get("mode") == "per-server" and servers:
        ok_cnt = sum(1 for s in servers if s["ssh_ok"])
        ssh_info["ok"] = ok_cnt == len(servers)
        ssh_info["servers_ok"] = ok_cnt
        ssh_info["servers_total"] = len(servers)
        docker_info["running"] = sum(s["docker_running"] for s in servers)
        docker_info["total"] = sum(s["docker_total"] for s in servers)
        docker_info["mode"] = "per-server"
        # 资源取「最紧张的那台」作为代表值
        def _pct(v):
            try:
                return float(str(v).rstrip("%") or 0)
            except ValueError:
                return 0.0
        worst_mem = max(servers, key=lambda s: _pct(s["memory"]), default=None)
        worst_disk = max(servers, key=lambda s: _pct(s["disk"]), default=None)
        worst_load = max(servers, key=lambda s: _pct((s["load"] or "0").split()[0] if s["load"] else 0),
                         default=None)
        resources["ok"] = ok_cnt > 0
        if worst_load and worst_load["load"]:
            resources["load_avg"] = worst_load["load"]
            resources["load_host"] = worst_load["host"]
        if worst_mem and worst_mem["memory"]:
            resources["memory"] = worst_mem["memory"]
            resources["memory_host"] = worst_mem["host"]
        if worst_disk and worst_disk["disk"]:
            resources["disk"] = worst_disk["disk"]
            resources["disk_host"] = worst_disk["host"]
    for s in servers:
        if not s["ssh_ok"]:
            issues.append({"level": "warning",
                           "message": f"{s['name']}（{s['host']}）SSH 不通：{s['error'][:60]}"})
        elif s["docker_total"] and s["docker_running"] < s["docker_total"]:
            issues.append({"level": "warning",
                           "message": f"{s['name']} 容器未全运行：{s['docker_running']}/{s['docker_total']}"})
        try:
            mem = float((s["memory"] or "0%").rstrip("%") or 0)
            if mem >= 90:
                issues.append({"level": "critical", "message": f"{s['name']} 内存 {mem:.0f}%"})
        except ValueError:
            pass

    return {
        "ssh": ssh_info,
        "docker": docker_info,
        "adb": adb_info,
        "resources": resources,
        "servers": servers,
        "issues": issues,
    }


# ────────────────────────── 设备诊断子模块 ──────────────────────────

def _build_device_report(device: Device) -> dict:
    """构建单台设备的深度诊断报告（ADB 延迟 / 截图 / 系统信息 / 资源 / 网络）。

    总超时 30 秒，各步骤共享时间预算。
    """
    serial = device.serial
    issues = []
    deadline = time.monotonic() + 30

    def _time_left():
        return max(0.0, deadline - time.monotonic())

    # ── ADB 连接测试（最多 8 秒）──
    # USB serial（不含冒号）需要走 SSH 在服务器端检测，不能用本机 adb connect
    adb_test = {"ok": False, "latency_ms": None, "state": device.status, "error": ""}
    if device.backend == "redroid" and serial:
        try:
            t0 = time.monotonic()
            is_usb_serial = ":" not in serial

            if is_usb_serial:
                # USB serial：通过 SSH 在服务器上查 adb devices（本机大概率看不到）
                adb_state = None
                if orchestrator._ssh_enabled():
                    try:
                        remote_devs = orchestrator.list_remote_adb_devices()
                        for d in remote_devs:
                            if d.get("serial") == serial:
                                adb_state = d.get("status")
                                break
                    except Exception:
                        pass
                # 回退到本机查
                if adb_state is None:
                    adb_state = orchestrator._adb_serial_state(serial)

                latency = round((time.monotonic() - t0) * 1000, 1)
                adb_test["ok"] = adb_state == "device"
                adb_test["latency_ms"] = latency
                adb_test["state"] = adb_state or "unknown"
                adb_test["detect_mode"] = "usb_remote" if orchestrator._ssh_enabled() else "usb_local"
            else:
                # IP:port serial：走正常的 _ensure_adb_connected
                ok = orchestrator._ensure_adb_connected(serial, timeout=min(8, int(_time_left())))
                latency = round((time.monotonic() - t0) * 1000, 1)
                adb_test["ok"] = bool(ok)
                adb_test["latency_ms"] = latency
                # 获取实际 ADB 状态
                adb_devices = orchestrator.list_adb_devices()
                for d in adb_devices:
                    if d.get("serial") == serial:
                        adb_test["state"] = d.get("status", "unknown")
                        break

            if not adb_test["ok"]:
                issues.append({"level": "critical", "message": f"ADB 连接失败（状态: {adb_test['state']}）"})
            elif (adb_test["latency_ms"] or 0) > 3000:
                issues.append({"level": "warning", "message": f"ADB 连接延迟偏高: {adb_test['latency_ms']}ms"})
        except Exception as e:
            adb_test["error"] = str(e)
            issues.append({"level": "error", "message": f"ADB 连接异常: {e}"})
    elif device.backend == "simulator":
        adb_test = {"ok": True, "latency_ms": 0, "state": "simulator", "info": "模拟器无需 ADB"}

    # ── 截图测试（最多 10 秒）──
    screenshot_test = {"ok": False, "size_bytes": 0, "error": ""}
    if device.backend == "redroid" and serial and adb_test["ok"] and _time_left() > 2:
        try:
            t0 = time.monotonic()
            img = orchestrator.get_screenshot(serial)
            shot_ms = round((time.monotonic() - t0) * 1000, 1)
            if img and len(img) > 0:
                screenshot_test["ok"] = True
                screenshot_test["size_bytes"] = len(img)
                screenshot_test["capture_ms"] = shot_ms
            else:
                screenshot_test["error"] = "截图返回空"
                issues.append({"level": "error", "message": "截图获取失败"})
        except Exception as e:
            screenshot_test["error"] = str(e)
            issues.append({"level": "error", "message": f"截图异常: {e}"})
    else:
        screenshot_test["info"] = "ADB 未连接或跳过截图" if not adb_test["ok"] else ""

    # ── 系统信息 + CPU + 内存（合并为一条 ADB shell，减少往返）──
    sys_info = {}
    resource_usage = {"cpu_percent": None, "mem_percent": None, "error": ""}
    if serial and adb_test["ok"] and _time_left() > 1:
        combined_cmd = (
            "echo __PROPS__; "
            "getprop ro.product.model; getprop ro.build.version.release; "
            "getprop ro.product.brand; getprop ro.product.manufacturer; "
            "echo __STAT__; head -1 /proc/stat; "
            "echo __MEM__; head -3 /proc/meminfo"
        )
        try:
            rc, out, _ = orchestrator._adb_shell(
                serial, combined_cmd, timeout=min(10, int(_time_left())))
            if rc == 0 and out:
                text = out.decode("utf-8", errors="replace") if isinstance(out, bytes) else str(out)
                # 解析 getprop
                if "__PROPS__" in text:
                    props_part = text.split("__PROPS__")[1].split("__STAT__")[0].strip().split("\n")
                    for i, k in enumerate(["model", "android_version", "brand", "manufacturer"]):
                        sys_info[k] = props_part[i].strip() if i < len(props_part) else ""
                # 解析 CPU
                if "__STAT__" in text:
                    stat_line = text.split("__STAT__")[1].split("__MEM__")[0].strip()
                    if stat_line.startswith("cpu "):
                        nums = stat_line.split()[1:]
                        if len(nums) >= 4:
                            try:
                                user, nice, system, idle = int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3])
                                total = user + nice + system + idle
                                resource_usage["cpu_percent"] = round((user + nice + system) / total * 100, 1) if total > 0 else 0
                            except (ValueError, IndexError):
                                pass
                # 解析内存
                if "__MEM__" in text:
                    mem_part = text.split("__MEM__")[1].strip().split("\n")
                    total_kb = avail_kb = 0
                    for line in mem_part[:3]:
                        if line.startswith("MemTotal:"):
                            total_kb = _parse_mem_kb(line)
                        elif line.startswith("MemAvailable:") or line.startswith("MemFree:"):
                            avail_kb = _parse_mem_kb(line)
                    if total_kb and avail_kb:
                        resource_usage["mem_percent"] = round((total_kb - avail_kb) / total_kb * 100, 1)
        except Exception as e:
            resource_usage["error"] = str(e)

    # ── 网络：用数据库已有值，不跑 curl（避免超时）──
    network = {"egress_ip": device.egress_ip or "", "error": ""}

    return {
        "device_id": device.id,
        "device_name": device.name,
        "serial": serial,
        "backend": device.backend,
        "adb_test": adb_test,
        "screenshot_test": screenshot_test,
        "sys_info": sys_info,
        "resource_usage": resource_usage,
        "network": network,
        "issues": issues,
    }


def _parse_mem_kb(line: str) -> int:
    """从 /proc/meminfo 行解析 KB 数。"""
    try:
        return int(line.split()[1])
    except (ValueError, IndexError):
        return 0


# ────────────────────────── 一键修复逻辑 ──────────────────────────

def _try_fix_device(device: Device) -> dict:
    """尝试修复单台设备，按顺序尝试多种策略直至成功或全部失败。

    根据设备类型自动选择修复策略：
    - USB serial（如 78AF0SUB10）：SSH 重连 ADB → 服务器重启 adbd → 验证
    - IP:port 容器：ADB 重连 → 容器重启 → 销毁重建

    返回 {"ok": bool, "steps": [...], "actions": [...], "error": ""}
    """
    serial = device.serial
    port = _device_port(serial) if device.backend == "redroid" else None
    is_usb = serial and ":" not in serial
    result = {"ok": False, "steps": [], "actions": [], "error": ""}

    # 模拟器无需修复
    if device.backend == "simulator":
        result["ok"] = True
        result["steps"].append({"name": "模拟器设备", "detail": "模拟器无需修复", "ok": True})
        return result

    if is_usb:
        # ═══ USB serial 设备（RK3399 开发板等）修复策略 ═══
        # 策略 1：在服务器上重连 ADB
        try:
            if orchestrator._ssh_enabled():
                client = orchestrator._ssh_shared_client()
                # 先断开再重连
                orchestrator._ssh_exec(client, f"adb disconnect {serial}", timeout=10)
                time.sleep(0.5)
                rc, out, err = orchestrator._ssh_exec(client, f"adb connect {serial}", timeout=15)
                connected = rc == 0 and ("connected" in (out or "").lower() or "already" in (out or "").lower())
                result["steps"].append({
                    "name": "ADB 重连（服务器端）",
                    "detail": (out or err or "").strip()[:100],
                    "ok": connected,
                })
                if connected:
                    result["ok"] = True
                    result["actions"].append("reconnect")
                    return result
            else:
                result["steps"].append({
                    "name": "ADB 重连",
                    "detail": "SSH 未配置，无法远程重连 USB 设备",
                    "ok": False,
                })
        except Exception as e:
            result["steps"].append({"name": "ADB 重连", "detail": f"失败: {e}", "ok": False})

        # 策略 2：在服务器上重启 adbd 服务
        try:
            if orchestrator._ssh_enabled():
                client = orchestrator._ssh_shared_client()
                # 通过 adb shell 重启 adbd（root 权限下）
                rc, out, err = orchestrator._ssh_exec(
                    client,
                    f"adb -s {serial} shell 'su -c stop && su -c start'",
                    timeout=20,
                )
                restarted = rc == 0
                result["steps"].append({
                    "name": "重启 adbd 服务",
                    "detail": "已发送重启命令" if restarted else (err or out or "失败").strip()[:100],
                    "ok": restarted,
                })
                if restarted:
                    # 等待 adbd 重启完成
                    time.sleep(5)
                    # 验证设备恢复
                    for _ in range(6):
                        rc2, out2, _ = orchestrator._ssh_exec(
                            client, f"adb -s {serial} get-state", timeout=10)
                        state = (out2 or "").strip() if rc2 == 0 else ""
                        if state == "device":
                            result["ok"] = True
                            result["actions"].append("restart_adbd")
                            return result
                        time.sleep(2)
                    result["steps"][-1]["detail"] = "adbd 已重启但设备未恢复"
        except Exception as e:
            result["steps"].append({"name": "重启 adbd", "detail": f"失败: {e}", "ok": False})

        # 策略 3：在服务器上 kill-server 再 start-server
        try:
            if orchestrator._ssh_enabled():
                client = orchestrator._ssh_shared_client()
                orchestrator._ssh_exec(client, "adb kill-server", timeout=10)
                time.sleep(2)
                orchestrator._ssh_exec(client, "adb start-server", timeout=10)
                time.sleep(3)
                # 验证
                rc, out, _ = orchestrator._ssh_exec(
                    client, f"adb -s {serial} get-state", timeout=10)
                state = (out or "").strip() if rc == 0 else ""
                result["steps"].append({
                    "name": "重启 ADB 服务",
                    "detail": f"ADB 服务已重启，设备状态: {state}",
                    "ok": state == "device",
                })
                if state == "device":
                    result["ok"] = True
                    result["actions"].append("restart_adb_server")
                    return result
        except Exception as e:
            result["steps"].append({"name": "重启 ADB 服务", "detail": f"失败: {e}", "ok": False})

        result["error"] = "USB 设备修复失败，可能需要物理检查连接"
        return result

    # ═══ IP:port 容器设备修复策略 ═══
    # 策略 1：ADB 重连
    try:
        orchestrator._run_adb(["disconnect", serial], timeout=5)
        time.sleep(0.5)
        connected = orchestrator.adb_connect(serial)
        result["steps"].append({
            "name": "ADB 重连",
            "detail": "断开后重新连接 ADB",
            "ok": bool(connected),
        })
        if connected:
            result["ok"] = True
            result["actions"].append("reconnect")
            return result
    except Exception as e:
        result["steps"].append({"name": "ADB 重连", "detail": f"失败: {e}", "ok": False})

    # 策略 2：容器重启
    if port is not None:
        try:
            r = orchestrator.container_power(port, "restart")
            restart_ok = r.get("ok", False)
            result["steps"].append({
                "name": "容器重启",
                "detail": r.get("error", "") if not restart_ok else "重启完成，等待 ADB 就绪",
                "ok": restart_ok,
            })
            if restart_ok:
                time.sleep(3)
                for _ in range(15):
                    if orchestrator.adb_connect(serial):
                        adb_devs = orchestrator.list_adb_devices()
                        if any(d.get("serial") == serial and d.get("status") == "device"
                               for d in adb_devs):
                            result["ok"] = True
                            result["actions"].append("restart")
                            return result
                    time.sleep(1)
                result["steps"][-1]["detail"] = "容器已重启但 ADB 未恢复"
        except Exception as e:
            result["steps"].append({"name": "容器重启", "detail": f"失败: {e}", "ok": False})

    # 策略 3：销毁重建容器
    if port is not None:
        try:
            del_r = orchestrator.delete_redroid_container(port)
            result["steps"].append({
                "name": "销毁容器",
                "detail": "成功" if del_r.get("ok") else del_r.get("error", "失败"),
                "ok": del_r.get("ok", False),
            })
            time.sleep(2)

            create_r = orchestrator.create_redroid_container(port)
            recreate_ok = create_r.get("ok", False)
            new_serial = create_r.get("serial", "")
            result["steps"].append({
                "name": "重建容器",
                "detail": f"新 serial: {new_serial}" if recreate_ok else create_r.get("error", "失败"),
                "ok": recreate_ok,
            })
            if recreate_ok:
                result["ok"] = True
                result["actions"].append("recreate")
                # 更新设备 serial（如重建后端口映射变了）
                if new_serial and new_serial != serial:
                    device.serial = new_serial
                return result
        except Exception as e:
            result["steps"].append({"name": "销毁重建", "detail": f"失败: {e}", "ok": False})

    # 所有策略失败
    if not result["ok"]:
        result["error"] = "所有修复策略均失败"
    return result


# ────────────────────────── API 端点 ──────────────────────────

@diag_bp.get("/diag/system")
@require_platform
def system_diagnostics():
    """系统级诊断：SSH 连通性 / Docker 状态 / ADB 设备 / 服务器资源。"""
    report = _build_system_report()
    issue_count = len(report["issues"])

    # 持久化诊断报告
    try:
        diag = DiagnosticReport(
            scope="system",
            result=json.dumps(report, ensure_ascii=False),
            summary=f"检测到 {issue_count} 个问题" if issue_count else "系统状态正常",
            issues_found=issue_count,
            created_by=g.current_user.id,
        )
        db.session.add(diag)
        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify(report)


@diag_bp.get("/diag/device/<int:device_id>")
@require_platform
def device_diagnostics(device_id):
    """单设备深度诊断：ADB 延迟 / 截图测试 / 系统信息 / CPU 内存 / 网络出口。"""
    device = db.session.get(Device, device_id)
    if device is None:
        return jsonify(error="设备不存在"), 404

    report = _build_device_report(device)
    issue_count = len(report["issues"])

    # 持久化诊断报告
    try:
        diag = DiagnosticReport(
            scope="device",
            target_id=device_id,
            result=json.dumps(report, ensure_ascii=False),
            summary=f"[{device.name}] 检测到 {issue_count} 个问题" if issue_count else f"[{device.name}] 状态正常",
            issues_found=issue_count,
            created_by=g.current_user.id,
        )
        db.session.add(diag)
        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify(report)


@diag_bp.post("/diag/device/<int:device_id>/fix")
@require_platform
def fix_device(device_id):
    """一键修复单台设备：自动按顺序尝试 ADB 重连 / 容器重启 / 销毁重建。"""
    device = db.session.get(Device, device_id)
    if device is None:
        return jsonify(error="设备不存在"), 404

    fix_result = _try_fix_device(device)

    # 更新设备状态
    if fix_result["ok"]:
        device.status = "running"
        device.last_seen = datetime.utcnow()
        db.session.commit()

    # 记录维护日志
    action_str = ",".join(fix_result["actions"]) if fix_result["actions"] else "none"
    try:
        ml = MaintenanceLog(
            device_id=device_id,
            action=action_str,
            status="success" if fix_result["ok"] else "failed",
            detail=json.dumps(fix_result, ensure_ascii=False),
            created_by=g.current_user.id,
        )
        db.session.add(ml)
        db.session.commit()
    except Exception:
        db.session.rollback()

    # 审计
    record_audit(
        g.current_user.id, "fix_device", "device", device_id,
        {"actions": fix_result["actions"], "ok": fix_result["ok"]},
    )

    # 保存诊断报告
    try:
        diag = DiagnosticReport(
            scope="device",
            target_id=device_id,
            result=json.dumps(fix_result, ensure_ascii=False),
            summary=f"设备 {device.name} 修复{'成功' if fix_result['ok'] else '失败'}",
            issues_found=1,
            issues_fixed=1 if fix_result["ok"] else 0,
            created_by=g.current_user.id,
        )
        db.session.add(diag)
        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify(fix_result)


@diag_bp.post("/diag/batch-fix")
@require_platform
def batch_fix():
    """批量修复离线/异常设备。

    请求体:
        device_ids:  [int]       指定设备 ID 列表（与 auto_select 二选一）
        auto_select: bool        为 true 时自动选择所有 stopped / error 设备
        max_workers: int         并发线程数（默认 5，最大 10）
    """
    data = request.get_json(silent=True) or {}
    device_ids = data.get("device_ids") or []
    auto_select = data.get("auto_select", False)
    max_workers = min(max(data.get("max_workers", 5) if isinstance(data.get("max_workers"), int) else 5, 1), 10)

    # 确定待修复设备
    if auto_select:
        devices = (db.session.query(Device)
                   .filter(Device.status.in_(["stopped", "error"]))
                   .all())
        device_ids = [d.id for d in devices]
    elif device_ids:
        devices = []
        for did in device_ids:
            d = db.session.get(Device, int(did))
            if d is not None:
                devices.append(d)
    else:
        return jsonify(error="请指定 device_ids 或开启 auto_select"), 400

    if not devices:
        return jsonify(
            error="没有需要修复的设备",
            total=0, ok=0, failed=0, results=[],
        ), 400

    # 限制单次批量数量
    if len(devices) > 50:
        return jsonify(error="单次批量修复最多 50 台设备"), 400

    results = []
    ok_count = 0
    failed_count = 0

    def _fix_one(device) -> dict:
        """线程池中的单设备修复。"""
        try:
            fix_r = _try_fix_device(device)
            # 更新设备状态
            if fix_r["ok"]:
                device.status = "running"
                device.last_seen = datetime.utcnow()
            return {
                "device_id": device.id,
                "device_name": device.name,
                "ok": fix_r["ok"],
                "actions": fix_r["actions"],
                "steps": fix_r["steps"],
                "error": fix_r["error"],
            }
        except Exception as e:
            return {
                "device_id": device.id,
                "device_name": device.name,
                "ok": False,
                "actions": [],
                "steps": [],
                "error": str(e),
            }

    # 并发执行修复
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_fix_one, d): d for d in devices}
        for future in as_completed(future_map):
            device = future_map[future]
            try:
                r = future.result(timeout=180)
            except Exception as e:
                r = {
                    "device_id": device.id,
                    "device_name": device.name,
                    "ok": False,
                    "actions": [],
                    "steps": [],
                    "error": f"修复超时或异常: {e}",
                }

            if r["ok"]:
                ok_count += 1
            else:
                failed_count += 1
            results.append(r)

    # 提交设备状态更新
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    # 批量记录维护日志
    for r in results:
        try:
            ml = MaintenanceLog(
                device_id=r["device_id"],
                action=",".join(r["actions"]) if r["actions"] else "failed",
                status="success" if r["ok"] else "failed",
                detail=json.dumps(r, ensure_ascii=False),
                created_by=g.current_user.id,
            )
            db.session.add(ml)
        except Exception:
            pass
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    # 审计
    record_audit(
        g.current_user.id, "batch_fix_device", "device", None,
        {"total": len(devices), "ok": ok_count, "failed": failed_count},
    )

    # 保存诊断报告
    try:
        diag = DiagnosticReport(
            scope="all",
            result=json.dumps({
                "total": len(devices), "ok": ok_count,
                "failed": failed_count, "results": results,
            }, ensure_ascii=False),
            summary=f"批量修复完成: 成功 {ok_count}/{len(devices)}",
            issues_found=len(devices),
            issues_fixed=ok_count,
            created_by=g.current_user.id,
        )
        db.session.add(diag)
        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify(
        total=len(devices),
        ok=ok_count,
        failed=failed_count,
        results=results,
    )


@diag_bp.get("/diag/history")
@require_platform
def diag_history():
    """诊断历史：返回最近的诊断报告列表（分页）。

    查询参数:
        scope:     过滤范围 (system / device / all)
        device_id: 过滤指定设备
        page:      页码（默认 1）
        per_page:  每页数量（默认 20，最大 100）
    """
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    scope = request.args.get("scope")
    device_id = request.args.get("device_id", type=int)

    if per_page > 100:
        per_page = 100
    if page < 1:
        page = 1

    q = db.session.query(DiagnosticReport).order_by(DiagnosticReport.created_at.desc())
    if scope:
        q = q.filter(DiagnosticReport.scope == scope)
    if device_id is not None:
        q = q.filter(DiagnosticReport.target_id == device_id)

    total = q.count()
    reports = q.offset((page - 1) * per_page).limit(per_page).all()

    items = []
    for r in reports:
        try:
            result_data = json.loads(r.result) if r.result else {}
        except (json.JSONDecodeError, TypeError):
            result_data = {}
        items.append({
            "id": r.id,
            "scope": r.scope,
            "target_id": r.target_id,
            "summary": r.summary,
            "issues_found": r.issues_found,
            "issues_fixed": r.issues_fixed,
            "result": result_data,
            "created_by": r.created_by,
            "created_at": iso_utc(r.created_at),
        })

    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    })
