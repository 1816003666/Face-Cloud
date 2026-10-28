"""数据看板：/metrics/overview 聚合指标 + /metrics/sales 套餐销售（当日销量/金额）。"""
import time
from flask import jsonify, Blueprint, request, g as flask_g
dashboard_bp = Blueprint("dashboard", __name__)
from ..extensions import db
from ..models import Device, ScheduledTask, Group, AuditLog, TaskExecution
from ..auth import login_required, require_role, require_platform

# 服务器状态接口缓存：SSH 采集开销大（1-2s），前端 15s 轮询，缓存 15s 内直接命中秒回
_SERVER_METRICS_CACHE: dict = {"ts": 0.0, "data": None}
_SERVER_METRICS_TTL = 15.0


@dashboard_bp.get("/metrics/server")
@require_platform
def server_metrics():
    """服务器实时监控：CPU 负载 / 内存 / 磁盘 / Docker 容器 + 云手机 ADB 在线（15s 缓存）。"""
    now = time.time()
    if now - _SERVER_METRICS_CACHE["ts"] < _SERVER_METRICS_TTL and _SERVER_METRICS_CACHE["data"] is not None:
        return jsonify(_SERVER_METRICS_CACHE["data"])

    from ..scheduler import _collect_server_health
    from .. import orchestrator

    server = _collect_server_health()
    # 云手机在线统计
    adb_online = 0
    try:
        adb_devs = orchestrator.list_adb_devices()
        adb_online = sum(1 for x in adb_devs if x["status"] == "device")
    except Exception:  # noqa: BLE001
        pass
    from .. import scoping
    _cu = flask_g.current_user
    if not scoping.is_privileged(_cu):
        _dq = scoping.scope_query(
            db.session.query(Device).filter_by(backend="redroid"), _cu)
        _running = _dq.filter(Device.status == "running").count()
        return jsonify({
            "server": server,
            "devices": {"total": _dq.count(), "online": _running,
                        "running": _running,
                        "error": _dq.filter(Device.status == "error").count()},
            "collected_at": _utcnow_iso(),
        })
    total = db.session.query(Device).filter_by(backend="redroid").count()
    running = db.session.query(Device).filter(
        Device.backend == "redroid", Device.status == "running").count()
    error = db.session.query(Device).filter(
        Device.backend == "redroid", Device.status == "error").count()

    payload = {
        "server": server,
        "devices": {"total": total, "online": adb_online,
                    "running": running, "error": error},
        "collected_at": _utcnow_iso(),
    }
    _SERVER_METRICS_CACHE["ts"] = now
    _SERVER_METRICS_CACHE["data"] = payload
    return jsonify(payload)


def _utcnow_iso():
    from ..models import _utcnow
    return _utcnow().isoformat()


@dashboard_bp.get("/metrics/overview")
@require_platform
def overview():
    from .. import scoping
    _cu = flask_g.current_user
    _dq = scoping.scope_query(db.session.query(Device), _cu)
    total_devices = _dq.count()
    running = _dq.filter_by(status="running").count()
    error = _dq.filter_by(status="error").count()
    stopped = _dq.filter_by(status="stopped").count()
    creating = _dq.filter_by(status="creating").count()
    groups = db.session.query(Group).count()
    tasks = db.session.query(ScheduledTask).count()
    enabled_tasks = db.session.query(ScheduledTask).filter_by(enabled=True).count()

    # 状态分布
    status_dist = {
        "running": running, "error": error,
        "stopped": stopped, "creating": creating,
    }
    # 分组分布（按分组统计设备数）
    group_dist = []
    for g in db.session.query(Group).all():
        cnt = scoping.scope_query(
            db.session.query(Device).filter_by(group_id=g.id), _cu).count()
        group_dist.append({"group": g.name, "count": cnt})

    recent = scoping.scope_query(
        db.session.query(Device), _cu).order_by(Device.id.desc()).limit(5).all()
    recent_devices = [{"id": d.id, "name": d.name, "status": d.status,
                       "ip": d.ip} for d in recent]

    # 最近执行
    last_exec = db.session.query(TaskExecution).order_by(
        TaskExecution.id.desc()).first()

    return jsonify({
        "kpis": {
            "total_devices": total_devices,
            "running": running,
            "error": error,
            "groups": groups,
            "tasks": tasks,
            "enabled_tasks": enabled_tasks,
        },
        "status_distribution": status_dist,
        "group_distribution": group_dist,
        "recent_devices": recent_devices,
        "last_execution": {
            "id": last_exec.id, "ok": last_exec.ok,
            "failed": last_exec.failed
        } if last_exec else None,
    })


# ---------------------------------------------------------------------------
# 趋势数据：60 秒缓存 + 降采样（24h 原始快照约 2200 条 / 200KB，看板折线图根本用不上这么密）
# ---------------------------------------------------------------------------
_TREND_CACHE = {}          # (ent, hours, max_points) -> (expires_ts, payload)
_TREND_TTL_SEC = 60
_TREND_MAX_POINTS = 360


def _downsample(rows, max_points):
    """按时间等距抽稀到 max_points 以内，保留首尾点。"""
    n = len(rows)
    if n <= max_points:
        return rows
    step = n / float(max_points)
    picked = [rows[int(i * step)] for i in range(max_points)]
    if picked[-1] is not rows[-1]:
        picked[-1] = rows[-1]
    return picked


@dashboard_bp.get("/metrics/trend")
@require_platform
def metrics_trend():
    """历史趋势数据（看板折线图）：默认最近 24h，可 ?hours= 调整。

    - 结果缓存 60 秒（同一企业/时间窗口重复打开看板不再查库）；
    - 超过 360 个点做等距抽稀，避免一次传 200KB。
    """
    import time
    from datetime import timedelta
    from ..models import MetricsSnapshot

    try:
        hours = min(max(int(request.args.get("hours", 24)), 1), 168)
    except (TypeError, ValueError):
        hours = 24
    ent = getattr(flask_g, "enterprise_id", None)
    key = (ent, hours)

    now_ts = time.time()
    hit = _TREND_CACHE.get(key)
    if hit and hit[0] > now_ts:
        return jsonify(hit[1])

    since = _utcnow_iso_dt() - timedelta(hours=hours)
    rows = db.session.query(MetricsSnapshot).filter(
        MetricsSnapshot.ts >= since).order_by(MetricsSnapshot.ts).all()
    rows = _downsample(rows, _TREND_MAX_POINTS)
    payload = [{
        "ts": r.ts.isoformat(),
        "server_cpu": r.server_cpu,
        "server_mem": r.server_mem,
        "server_disk": r.server_disk,
        "containers": r.containers,
        "devices_online": r.devices_online,
        "devices_total": r.devices_total,
    } for r in rows]

    _TREND_CACHE[key] = (now_ts + _TREND_TTL_SEC, payload)
    # 顺手清掉过期项，避免长期运行堆积
    for k in [k for k, v in _TREND_CACHE.items() if v[0] <= now_ts]:
        _TREND_CACHE.pop(k, None)
    return jsonify(payload)


def _utcnow_iso_dt():
    from ..models import _utcnow
    return _utcnow()


# ===========================================================================
# 套餐销售统计 /metrics/sales（数据看板：当日销量与金额）
# ===========================================================================
@dashboard_bp.get("/metrics/sales")
@require_platform
def metrics_sales():
    """套餐销售看板数据：今日 / 昨日销量与金额、近 7 天按天序列、今日分套餐明细、本月合计。

    - 订单时间戳是 naive UTC（models._utcnow），"当日"按北京时间（UTC+8）切分，
      避免板子系统时区为 UTC 时把凌晨 0-8 点的订单算到前一天；可用 SALES_TZ_OFFSET_MIN 覆盖；
    - 销量只统计 status=paid 的订单，按 paid_at 归属日期（历史数据缺失 paid_at 时回落到 created_at）；
    - pending 为今日新建、尚未支付的订单数（提示运营跟单）。
    """
    import os
    from datetime import datetime as _dt, timedelta
    from sqlalchemy import or_, and_
    from ..models import Order

    now_utc = _dt.utcnow()
    try:
        tz_min = int(os.environ.get("SALES_TZ_OFFSET_MIN", "480"))
    except ValueError:
        tz_min = 480
    offset = timedelta(minutes=tz_min)
    local_now = now_utc + offset
    today_local = _dt(local_now.year, local_now.month, local_now.day)
    week_start_local = today_local - timedelta(days=6)
    month_start_local = _dt(local_now.year, local_now.month, 1)
    window_start_utc = min(week_start_local, month_start_local) - offset

    orders = db.session.query(Order).filter(
        Order.status == "paid",
        or_(Order.paid_at >= window_start_utc,
            and_(Order.paid_at.is_(None), Order.created_at >= window_start_utc)),
    ).all()

    # 门户开通记录也计入销售（金额按 SKU 匹配本地套餐价；门户未回传真实金额）
    from ..models import PortalProvision, Plan
    _plans_by_sku = {p.sku_id: p for p in db.session.query(Plan).all() if p.sku_id}
    provisions = db.session.query(PortalProvision).filter(
        PortalProvision.status == "success",
        PortalProvision.created_at >= window_start_utc,
    ).all()

    def prov_amount(pv):
        plan = _plans_by_sku.get(pv.sku_id)
        return int(plan.price_cents or 0) if plan else 0

    def day_of(o):
        return ((o.paid_at or o.created_at) + offset).date()

    today_key = today_local.date()
    buckets: dict = {}
    for o in orders:
        b = buckets.setdefault(day_of(o), {"orders": 0, "amount": 0})
        b["orders"] += 1
        b["amount"] += o.amount_cents
    for pv in provisions:
        b = buckets.setdefault((pv.created_at + offset).date(), {"orders": 0, "amount": 0})
        b["orders"] += 1
        b["amount"] += prov_amount(pv)

    days = []
    for i in range(7):
        d = (week_start_local + timedelta(days=i)).date()
        b = buckets.get(d, {"orders": 0, "amount": 0})
        days.append({
            "date": d.isoformat(),
            "orders": b["orders"],
            "amount_yuan": round(b["amount"] / 100, 2),
        })

    today_b = buckets.get(today_key, {"orders": 0, "amount": 0})
    yesterday_b = buckets.get(today_key - timedelta(days=1), {"orders": 0, "amount": 0})

    month_start_key = month_start_local.date()
    month_orders = [o for o in orders if day_of(o) >= month_start_key]
    month_provs = [pv for pv in provisions if (pv.created_at + offset).date() >= month_start_key]

    # 今日分套餐明细（按金额降序）
    plan_agg: dict = {}
    for o in orders:
        if day_of(o) != today_key:
            continue
        p = plan_agg.setdefault(o.plan_id, {
            "plan_id": o.plan_id,
            "plan_name": o.plan.name if o.plan else ("套餐#%s" % o.plan_id),
            "orders": 0,
            "amount": 0,
        })
        p["orders"] += 1
        p["amount"] += o.amount_cents
    for pv in provisions:
        if (pv.created_at + offset).date() != today_key:
            continue
        plan = _plans_by_sku.get(pv.sku_id)
        pid = plan.id if plan else 0
        pname = plan.name if plan else (pv.sku_id or "门户订单")
        p = plan_agg.setdefault(pid, {
            "plan_id": pid, "plan_name": pname, "orders": 0, "amount": 0,
        })
        p["orders"] += 1
        p["amount"] += prov_amount(pv)
    today_plans = []
    for p in sorted(plan_agg.values(), key=lambda x: (-x["amount"], x["plan_name"])):
        today_plans.append({
            "plan_id": p["plan_id"],
            "plan_name": p["plan_name"],
            "orders": p["orders"],
            "amount_yuan": round(p["amount"] / 100, 2),
        })

    pending_today = db.session.query(Order).filter(
        Order.status == "pending",
        Order.created_at >= today_local - offset,
    ).count()

    return jsonify({
        "today": {
            "date": today_key.isoformat(),
            "orders": today_b["orders"],
            "amount_yuan": round(today_b["amount"] / 100, 2),
            "pending": pending_today,
        },
        "yesterday": {
            "date": (today_key - timedelta(days=1)).isoformat(),
            "orders": yesterday_b["orders"],
            "amount_yuan": round(yesterday_b["amount"] / 100, 2),
        },
        "days": days,
        "today_plans": today_plans,
        "month": {
            "month": today_key.strftime("%Y-%m"),
            "orders": len(month_orders) + len(month_provs),
            "amount_yuan": round((sum(o.amount_cents for o in month_orders)
                                  + sum(prov_amount(pv) for pv in month_provs)) / 100, 2),
        },
        "updated_at": now_utc.isoformat(),
    })


# ===========================================================================
# 多服务器聚合接口 /metrics/nodes
# ===========================================================================
# 兜底清单：即使某台暂时没有设备，也要在看板上显示它的在线状态。
# 真正的统计范围由 _node_ips() 从数据库算出来（设备所在机器 + 已登记的服务器），
# 不再写死这一组 IP —— 写死会导致"设备管理 261 台、看板只有 240 台"这种对不上。
_NODE_IPS = [
    "36.156.109.204", "36.156.109.209", "36.156.109.213",
    "36.156.109.238", "36.156.109.239",
    "36.156.109.240", "36.156.109.242", "36.156.109.243",
]


def _node_ips() -> list:
    """看板要统计的服务器 IP：设备实际所在机器 ∪ 兜底清单（只算承载云手机的机器）。"""
    from ..models import Device

    ips = set(_NODE_IPS)
    try:
        for (ip,) in db.session.query(Device.ip).filter(Device.ip.isnot(None)).distinct():
            ip = (ip or "").strip()
            if ip:
                ips.add(ip)
    except Exception:  # noqa: BLE001
        pass
    # 注意：只算「承载云手机的服务器」，不要把 servers 表里所有登记项都算进来
    # （192.168.99.x 那些是宿主/跳板机，没有云手机，算进来会让"服务器在线 x/17"失真）
    return sorted(ips)
_JUMP_HOST = "36.156.109.237"
_NODES_CACHE: dict = {"ts": 0.0, "status_ts": 0.0, "data": None}
_NODES_STATUS_TTL = 12.0
_NODES_METRICS_TTL = 20.0


def _nodes_jump_connect():
    """连接跳板机（237），返回 paramiko.SSHClient；失败返回 None。"""
    try:
        from ..orchestrator import _ssh_connect
        return _ssh_connect()
    except Exception:  # noqa: BLE001
        return None


def _nodes_remote_exec(jump, target_ip: str, command: str, timeout: int = 5):
    """在目标服务器上执行命令。

    - target_ip == 跳板机：直接在 jump 上执行
    - 其他服务器：通过 jump 用 ssh -i /root/j.txt 跳转执行
    返回 (rc, stdout) 或 (None, '') 失败。
    """
    import shlex
    if target_ip == _JUMP_HOST:
        full_cmd = command
    else:
        # 跳板机上用私钥跳转，StrictHostKeyChecking 避免首次连接卡住
        full_cmd = (
            f"ssh -o ConnectTimeout={timeout} -o StrictHostKeyChecking=no "
            f"-o BatchMode=yes -i /root/j.txt root@{target_ip} "
            f"{shlex.quote(command)}"
        )
    try:
        stdin, stdout, stderr = jump.exec_command(full_cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out
    except Exception:  # noqa: BLE001
        return None, ""


def _nodes_parse_cpu(top_out: str) -> float | None:
    """解析 top -bn1 | grep Cpu 输出，返回 CPU 使用率百分比。"""
    for line in (top_out or "").splitlines():
        if "Cpu" in line or "cpu" in line:
            # %Cpu(s):  2.0 us,  1.0 sy,  0.0 ni, 97.0 id, ...
            parts = line.replace(",", " ").split()
            for i, p in enumerate(parts):
                if p in ("id",) and i > 0:
                    try:
                        idle = float(parts[i - 1])
                        return round(100.0 - idle, 1)
                    except (ValueError, IndexError):
                        return None
            # 兼容：直接找第一个数字作为 us+sy
            nums = []
            for p in parts:
                try:
                    nums.append(float(p.replace("%", "")))
                except ValueError:
                    pass
            if len(nums) >= 4:
                return round(nums[0] + nums[1] + nums[2], 1)
    return None


def _nodes_parse_mem(free_out: str) -> float | None:
    """解析 free | grep Mem 输出，返回内存使用率百分比。"""
    for line in (free_out or "").splitlines():
        if line.strip().startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    total = float(parts[1])
                    used = float(parts[2])
                    if total > 0:
                        return round(used / total * 100, 1)
                except (ValueError, IndexError):
                    return None
    return None


def _nodes_probe(jump, ip: str, refresh_status: bool, refresh_metrics: bool) -> dict:
    """单台服务器状态+指标采集（供并行调用）。"""
    node = {"ip": ip, "online": False, "devices": 0, "devices_total": 0,
            "cpu": None, "mem": None}
    if jump is None:
        return node
    # —— 在线探测（短超时，避免离线节点拖慢整体）——
    if refresh_status:
        rc, out = _nodes_remote_exec(jump, ip, "echo OK", timeout=2)
        node["online"] = (rc == 0 and "OK" in out)
    else:
        # 状态未过期时，沿用缓存中的 online
        node["online"] = True  # 占位，下面会用缓存覆盖
    # —— 指标采集（仅在线且需要刷新时）——
    if refresh_metrics and node["online"]:
        rc, out = _nodes_remote_exec(
            jump, ip,
            "top -bn1 | grep 'Cpu(s)' ; free | grep Mem",
            timeout=5,
        )
        if rc == 0 and out:
            lines = out.splitlines()
            cpu_line = next((l for l in lines if "Cpu" in l), "")
            mem_line = next((l for l in lines if l.strip().startswith("Mem:")), "")
            node["cpu"] = _nodes_parse_cpu(cpu_line)
            node["mem"] = _nodes_parse_mem(mem_line)
    return node


def _nodes_tcp_probe(ip: str, port: int = 22, timeout: float = 2.0) -> bool:
    """本机直连 TCP 探测服务器在线状态（不依赖 237 跳板 SSH）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect((ip, port))
            return True
        except Exception:
            return False
    finally:
        s.close()


def _nodes_collect(refresh_status: bool, refresh_metrics: bool) -> dict:
    """并行采集多台服务器状态 + 设备数。

    - 在线状态：本机直连 TCP 探测 22 端口（237 跳板已下线，不再依赖 SSH）
    - CPU/内存：无跳板 SSH 凭据，置 None（沿用缓存旧值，不阻塞看板）
    - 设备数：数据库按 IP 分组统计
    """
    from concurrent.futures import ThreadPoolExecutor
    from ..models import Device, Alert

    node_ips = _node_ips()
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(node_ips)))) as ex:
        online_flags = dict(zip(node_ips, ex.map(_nodes_tcp_probe, node_ips)))

    nodes = []
    for ip in node_ips:
        nodes.append({
            "ip": ip,
            "online": online_flags.get(ip, False),
            "devices": 0,
            "devices_total": 0,
            "cpu": None,
            "mem": None,
        })

    # —— 设备数（DB 按 ip 分组，无缓存）——
    dev_counts = {}
    for ip, cnt in db.session.query(Device.ip, db.func.count(Device.id)).filter(
        Device.backend == "redroid"
    ).group_by(Device.ip).all():
        dev_counts[ip] = cnt
    running_counts = {}
    for ip, cnt in db.session.query(Device.ip, db.func.count(Device.id)).filter(
        Device.backend == "redroid", Device.status == "running"
    ).group_by(Device.ip).all():
        running_counts[ip] = cnt

    for n in nodes:
        n["devices_total"] = dev_counts.get(n["ip"], 0)
        n["devices"] = running_counts.get(n["ip"], 0)

    return {"nodes": nodes}


def _nodes_build_payload(raw: dict) -> dict:
    """从采集原始数据构建完整响应（KPI 聚合 + Top 异常）。"""
    from ..models import Alert

    nodes = raw["nodes"]
    online_nodes = [n for n in nodes if n["online"]]
    online_count = len(online_nodes)

    # 平均 CPU / 内存（仅在线且有数据的节点）
    cpu_vals = [n["cpu"] for n in online_nodes if n["cpu"] is not None]
    mem_vals = [n["mem"] for n in online_nodes if n["mem"] is not None]
    # 没有采集到指标就返回 None：返回 0.0 会让界面显示"CPU 0%"这种假数据
    avg_cpu = round(sum(cpu_vals) / len(cpu_vals), 1) if cpu_vals else None
    avg_mem = round(sum(mem_vals) / len(mem_vals), 1) if mem_vals else None

    # 直接数设备表，避免"设备 ip 不在节点清单里 → 总数少算"
    try:
        from ..models import Device
        devices_total = (db.session.query(db.func.count(Device.id))
                         .filter(Device.backend == "redroid").scalar() or 0)
        devices_running = (db.session.query(db.func.count(Device.id))
                           .filter(Device.backend == "redroid",
                                   Device.status == "running").scalar() or 0)
    except Exception:  # noqa: BLE001
        devices_total = sum(n["devices_total"] for n in nodes)
        devices_running = sum(n["devices"] for n in nodes)

    # 活跃告警数
    alerts_active = db.session.query(Alert).filter(Alert.status == "active").count()

    # Top 异常：离线 / CPU>80 / 内存>85，最多3个
    offenders = []
    for n in nodes:
        reasons = []
        if not n["online"]:
            reasons.append("离线")
        if n["cpu"] is not None and n["cpu"] > 80:
            reasons.append(f"CPU {n['cpu']:.0f}%")
        if n["mem"] is not None and n["mem"] > 85:
            reasons.append(f"内存 {n['mem']:.0f}%")
        if reasons:
            offenders.append({
                "ip": n["ip"],
                "cpu": n["cpu"],
                "mem": n["mem"],
                "reason": "、".join(reasons),
            })
    # 排序：离线优先，然后 CPU+mem 降序
    offenders.sort(key=lambda x: (
        0 if "离线" in x["reason"] else 1,
        -( (x["cpu"] or 0) + (x["mem"] or 0) ),
    ))
    top_offenders = offenders[:3]

    return {
        "kpis": {
            "servers_online": online_count,
            "servers_total": len(nodes),
            "devices_total": devices_total,
            "devices_running": devices_running,
            "avg_cpu": avg_cpu,
            "avg_mem": avg_mem,
            "alerts_active": alerts_active,
        },
        "nodes": nodes,
        "top_offenders": top_offenders,
        "collected_at": _utcnow_iso(),
    }


@dashboard_bp.get("/metrics/nodes")
@require_platform
def metrics_nodes():
    """多服务器聚合数据：状态5s缓存、指标15s缓存，本机TCP探测+DB统计。"""
    now = time.time()
    cached = _NODES_CACHE["data"]
    status_fresh = (now - _NODES_CACHE["status_ts"]) < _NODES_STATUS_TTL
    metrics_fresh = (now - _NODES_CACHE["ts"]) < _NODES_METRICS_TTL

    # 完全命中缓存
    if status_fresh and metrics_fresh and cached is not None:
        return jsonify(cached)

    refresh_status = not status_fresh
    refresh_metrics = not metrics_fresh

    # 如果只有状态过期，需要保留缓存中的指标
    prev_nodes = {n["ip"]: n for n in (cached["nodes"] if cached else [])}

    raw = _nodes_collect(refresh_status=refresh_status, refresh_metrics=refresh_metrics)

    # 状态未刷新时，沿用缓存的 online；指标未刷新时，沿用缓存的 cpu/mem
    if not refresh_status or not refresh_metrics:
        for n in raw["nodes"]:
            prev = prev_nodes.get(n["ip"], {})
            if not refresh_status:
                n["online"] = prev.get("online", False)
            if not refresh_metrics:
                if n["cpu"] is None:
                    n["cpu"] = prev.get("cpu")
                if n["mem"] is None:
                    n["mem"] = prev.get("mem")

    payload = _nodes_build_payload(raw)

    # 采集完成后再打时间戳，避免长采集导致缓存立即过期
    finished_at = time.time()
    if refresh_status:
        _NODES_CACHE["status_ts"] = finished_at
    if refresh_metrics:
        _NODES_CACHE["ts"] = finished_at
    _NODES_CACHE["data"] = payload

    return jsonify(payload)

