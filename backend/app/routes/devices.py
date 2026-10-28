"""设备生命周期：创建 / 批量建 N 台 / 启停 / 删除 / 列表 / 详情 / 单台操控 / 截图 / 导入真实设备。"""
import io
import json
import os
import uuid
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from flask import request, jsonify, g, Blueprint, send_file, current_app
from sqlalchemy import select, or_
devices_bp = Blueprint("devices", __name__)
from ..extensions import db
from ..models import Device, DeviceLog, Group, record_audit, raise_alert
from ..auth import login_required, require_role, require_perm, require_platform
from .. import orchestrator
from .. import scan as net_scan


def _billing_block(cur):
    """预付费计费拦截：平台账号（无企业归属的 admin/superadmin）放行；其余须有有效套餐。

    企业管理员虽然 role=admin，但也是"用户"，仍要走计费拦截。
    返回 None 表示放行；否则返回 403 响应（reason 给前端提示）。
    """
    if cur.role in ("admin", "superadmin") and cur.enterprise_id is None:
        return None
    from ..billing import check_device_access
    r = check_device_access(cur.id)
    if r.get("allowed"):
        return None
    return jsonify(error=r.get("reason") or "套餐已到期，请先购买/续费套餐",
                   status=r.get("status")), 403


def _current_subscription(cur):
    """当前用户最近一条生效中的订阅（用于取套餐规格、绑定 sub_id）。"""
    from ..models import Subscription
    now = datetime.utcnow()
    return (Subscription.query
            .filter_by(user_id=cur.id, status="active")
            .filter(Subscription.expires_at > now)
            .order_by(Subscription.id.desc())
            .first())


def _active_sub_quotas(cur):
    """该用户每条生效订阅的「剩余额度」，按购买顺序排列。

    用户可能同时有多个套餐（标准 + 畅玩），创建云手机时要**按套餐分别分配**：
    标准套餐的额度配标准规格的机器、畅玩套餐的额度配畅玩规格，
    而不是都用最新套餐的规格（否则买 1 台标准 + 1 台畅玩会配出 2 台畅玩）。
    """
    import json as _json
    from ..models import Device, Subscription
    now = datetime.utcnow()
    subs = (Subscription.query
            .filter_by(user_id=cur.id, status="active")
            .filter(Subscription.expires_at > now)
            .order_by(Subscription.id.asc())
            .all())
    out = []
    for s in subs:
        used = Device.query.filter_by(sub_id=s.id).count()
        specs = {}
        if s.plan:
            try:
                specs = _json.loads(s.plan.specs or "{}")
            except (TypeError, ValueError):
                specs = {}
        qty = max(0, int(s.qty or 1))
        out.append({"sub": s, "plan": s.plan, "specs": specs,
                    "qty": qty, "used": used, "left": qty - used})
    return out


def _plan_quota(cur):
    """当前生效套餐的额度信息。

    quota = 所有生效订阅的 qty 之和（同套餐重复购买是"台数叠加"）
    used  = 该用户已创建的云手机数
    specs = 最近购买的生效订阅的规格（分辨率/CPU/内存/存储/Android）
    """
    from ..models import Subscription
    now = datetime.utcnow()
    subs = (Subscription.query
            .filter_by(user_id=cur.id, status="active")
            .filter(Subscription.expires_at > now)
            .order_by(Subscription.id.desc())
            .all())
    quota = sum((s.qty or 1) for s in subs)
    used = Device.query.filter_by(created_by=cur.id).count()
    specs = {}
    if subs and subs[0].plan:
        try:
            specs = json.loads(subs[0].plan.specs or "{}")
        except Exception:
            specs = {}
    return quota, used, specs


# ---------- 网络扫描：自动发现局域网云手机（本地后端模式） ----------
@devices_bp.post("/devices/scan")
@require_platform
def scan_network_devices():
    """扫描局域网网段，发现运行 adb 服务的云手机并导入设备列表。

    body: {"hosts": ["192.168.9.121"], "cidrs": ["192.168.9.0/24", ...]}  可选，缺省自动探测本机网段
    """
    data = request.get_json(silent=True) or {}
    cidrs = data.get("cidrs") or None
    hosts = data.get("hosts") or None
    try:
        result = net_scan.discover(cidrs, hosts, connect=True)
    except Exception as e:
        return jsonify(error=f"扫描失败: {e}"), 500

    # 导入设备库：serial=ip:port 的设备已存在则更新信息，否则创建
    imported = []
    for d in result["devices"]:
        serial = d["serial"]
        dev = db.session.execute(
            select(Device).where(Device.serial == serial)
        ).scalar_one_or_none()
        if dev is None:
            dev = Device(
                serial=serial,
                name=d.get("model") or serial,
                backend="redroid",
                ip=d.get("ip", ""),
                status="running" if d.get("online") else "stopped",
            )
            db.session.add(dev)
        else:
            if d.get("model"):
                dev.name = dev.name or d["model"]
            dev.status = "running" if d.get("online") else dev.status
            if d.get("ip"):
                dev.ip = d["ip"]
        imported.append(serial)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(error="设备导入失败"), 500

    return jsonify(
        cidrs=result["cidrs"],
        scanned=result["scanned"],
        elapsed_ms=result["elapsed_ms"],
        devices=result["devices"],
        imported=imported,
    )



@devices_bp.get("/devices")
@login_required
def list_devices():
    group_id = request.args.get("group_id", type=int)
    status = request.args.get("status")
    q = request.args.get("q")
    servers = request.args.get("servers")
    stmt = select(Device)
    if group_id is not None:
        stmt = stmt.where(Device.group_id == group_id)
    if status:
        stmt = stmt.where(Device.status == status)
    if q:
        stmt = stmt.where(Device.name.ilike(f"%{q}%"))
    if servers:
        # 逗号分隔的服务器 IP 列表，如 ?servers=36.156.109.237,36.156.109.204
        # 匹配 Device.serial 以 "IP:" 开头 或 Device.ip == IP
        ip_list = [s.strip() for s in servers.split(",") if s.strip()]
        if ip_list:
            conditions = []
            for ip in ip_list:
                conditions.append(Device.serial.like(f"{ip}:%"))
                conditions.append(Device.ip == ip)
            stmt = stmt.where(or_(*conditions))
    from .. import scoping
    stmt = scoping.scope_query(stmt, g.current_user)
    devices = db.session.execute(stmt.order_by(Device.id)).scalars().all()
    return jsonify([_dev_to_dict(d) for d in devices])


@devices_bp.get("/devices/server-ips")
@login_required
def list_server_ips():
    """设备库里出现过的所有服务器 IP（去重）。

    设备管理页的「服务器」过滤区用它来渲染列表——不能只从当前筛选结果里取，
    否则一旦筛掉某台服务器，它就再也回不到列表里了。
    经 SSH 中继接入的设备 ip 记的是服务器本身，这里取的就是那个（不是 127.0.0.1）。
    """
    from .. import scoping
    if not scoping.is_privileged(g.current_user) and not scoping.has_active_plan(g.current_user):
        return jsonify([])
    ips = set()
    _ips_q = db.session.query(Device.ip, Device.serial)
    if not scoping.is_privileged(g.current_user):
        _ips_q = _ips_q.filter(Device.created_by == g.current_user.id)
    for ip, serial in _ips_q.all():
        ip = (ip or "").strip()
        if ip and ip not in ("127.0.0.1", "localhost"):
            ips.add(ip)
            continue
        # 兼容 ip 缺失或记成本机回环的老数据 → 退回从 serial 里取
        if serial and ":" in serial:
            host = serial.split(":")[0].strip()
            if host and host not in ("127.0.0.1", "localhost"):
                ips.add(host)
    return jsonify(sorted(ips, key=_ip_sort_key))


def _ip_sort_key(ip):
    parts = []
    for seg in (ip or "").split("."):
        parts.append(int(seg) if seg.isdigit() else 0)
    return parts


@devices_bp.post("/devices")
@login_required
def create_device():
    """管理员：新建一台容器；普通用户：从公共池分配一台（购买时已不预分配）。"""
    cur = g.current_user
    block = _billing_block(cur)
    if block:
        return block
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    group_id = data.get("group_id")
    backend = data.get("backend") or "simulator"
    if not name:
        return jsonify(error="设备名必填"), 400

    # 只有"平台账号"才真的是新起容器；企业管理员也是用户，走公共池分配
    # （以前任何 admin 角色都被当成运维，企业管理员点"创建云手机"会去起容器，
    #   serial 撞车直接 500，企业也永远分不到设备）
    is_platform = cur.role in ("admin", "superadmin") and cur.enterprise_id is None

    quota, used, specs = _plan_quota(cur)

    if not is_platform:
        if quota > 0 and used >= quota:
            return jsonify(error="套餐额度已用完（已用 %d / 共 %d 台），请续费后再创建云手机" % (used, quota)), 400
        # 从公共池分配一台：按**各套餐各自的剩余额度**找规格匹配的机器。
        # 公共池是平台的，必须绕过企业数据隔离，否则企业看不到任何可售设备。
        from ..portal_provision import _available_query, _spec_ok
        items = _active_sub_quotas(cur)
        d = None
        chosen = None
        for it in items:
            if it["left"] <= 0:
                continue
            pool = _available_query(it["specs"], it["plan"]) \
                .execution_options(bypass_tenant=True)
            for cand in pool.order_by(Device.id).all():
                if _spec_ok(cand, it["specs"]):
                    d = cand
                    chosen = it
                    break
            if d is not None:
                break
        if d is None:
            return jsonify(
                error="当前没有可分配的云手机（库存不足），请联系客服或稍后再试",
                code="NO_RESOURCE"), 409
        d.name = name
        d.created_by = cur.id
        d.sub_id = chosen["sub"].id if chosen is not None else None
        # 分配后归属买家的企业，企业自己的列表里才看得到
        d.enterprise_id = cur.enterprise_id
        db.session.commit()
        try:
            from .. import egress as _egress
            _egress.maybe_bind(d)
        except Exception:  # noqa: BLE001
            db.session.rollback()
        record_audit(cur.id, "allocate_device", "device", d.id,
                     {"name": name, "plan_id": chosen["plan"].id if chosen else None})
        return jsonify(_dev_to_dict(d)), 201

    try:
        state = orchestrator.create_device(backend, name)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if not state.get("serial"):
        return jsonify(error=state.get("error") or "设备创建失败"), 400

    d = Device(name=name, group_id=group_id, status=state["status"],
               serial=state["serial"], backend=backend, ip=state["ip"],
               fingerprint=state["fingerprint"], created_by=cur.id,
               last_seen=datetime.utcnow(), cpu=20.0, mem=30.0,
               spec_resolution=specs.get("resolution", ""),
               spec_cpu=specs.get("cpu", ""),
               spec_memory=specs.get("memory", ""),
               spec_storage=specs.get("storage", ""),
               spec_android=specs.get("android", ""))
    db.session.add(d)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(error="该容器地址已被占用（serial 冲突），请先在设备列表里处理旧记录"), 409
    record_audit(cur.id, "create_device", "device", d.id, {"name": name, "backend": backend})
    return jsonify(_dev_to_dict(d)), 201


@devices_bp.post("/devices/batch")
@login_required
def batch_create():
    """管理员：批量新建容器；普通用户：一次从公共池分配多台（受套餐额度限制）。"""
    cur = g.current_user
    block = _billing_block(cur)
    if block:
        return block
    data = request.get_json(silent=True) or {}
    count = data.get("count", 0)
    prefix = data.get("prefix", "phone")
    group_id = data.get("group_id")
    backend = data.get("backend") or "simulator"
    if not (1 <= count <= 200):
        return jsonify(error="count 需在 1..200"), 400

    # 只有平台账号（无企业归属的 admin/superadmin）才走"新起容器"路径；
    # 企业管理员也是用户，和普通用户一样按套餐额度从公共池分配。
    is_platform = cur.role in ("admin", "superadmin") and cur.enterprise_id is None
    if not is_platform:
        # 普通用户：按套餐额度从池子里挑，最多分配到额度上限
        quota, used, specs = _plan_quota(cur)
        remain = max(0, quota - used)
        if remain <= 0:
            return jsonify(error="套餐额度已用完（已用 %d / 共 %d 台），请续费后再创建云手机" % (used, quota)), 400
        want = min(count, remain)
        sub = _current_subscription(cur)
        from ..portal_provision import _pick_devices
        picked = _pick_devices(specs, want, sub.plan if sub is not None else None)
        if not picked:
            return jsonify(error="当前没有可分配的云手机（库存不足），请联系客服或稍后再试",
                           code="NO_RESOURCE"), 409
        for i, d in enumerate(picked, start=1):
            d.name = f"{prefix}-{i}"
            d.created_by = cur.id
            d.sub_id = sub.id if sub is not None else None
            try:
                from .. import egress as _egress
                _egress.maybe_bind(d)
            except Exception:  # noqa: BLE001 - 绑出口失败不影响分配
                db.session.rollback()
        db.session.commit()
        record_audit(cur.id, "batch_allocate_device", "device", None,
                     {"count": len(picked), "quota": quota, "used": used})
        return jsonify([_dev_to_dict(d) for d in picked]), 201

    created = []
    for i in range(count):
        state = orchestrator.create_device(backend, f"{prefix}-{i+1}")
        if not state.get("serial"):
            continue  # 该台容器创建失败（如端口已被占用），跳过不落库
        exist = db.session.query(Device).filter_by(
            backend=backend, serial=state["serial"]).first()
        if exist:
            from .. import scoping as _scoping
            if not _scoping.is_privileged(cur) and exist.created_by != cur.id:
                continue  # 已属于他人：不复用、不泄露
            created.append(exist)  # 已存在该 serial，复用，避免重复
            continue
        d = Device(name=f"{prefix}-{i+1}", group_id=group_id, status=state["status"],
                   serial=state["serial"], backend=backend, ip=state["ip"],
                   fingerprint=state["fingerprint"], created_by=cur.id)
        db.session.add(d)
        created.append(d)
    db.session.commit()
    record_audit(cur.id, "batch_create_device", "device", None,
                 {"count": count, "backend": backend})
    return jsonify([_dev_to_dict(d) for d in created]), 201


@devices_bp.post("/devices/batch-delete")
@require_perm("device.delete")
def batch_delete():
    """批量删除云手机。

    - **平台管理员**：真删容器 + 删记录（运维动作）；
    - **个人用户 / 企业用户**：只清数据 + 归还公共池，不销毁服务器上的容器
      （用户点"删除"只是不用了，容器是平台的资产，不能由用户销毁）。
    """
    cur = g.current_user
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in (data.get("device_ids") or [])]
    if not ids:
        return jsonify(error="请选择至少一台设备"), 400
    from .. import scoping
    _bad = scoping.foreign_device_ids(cur, ids)
    if _bad:
        return jsonify(error="无权操作部分设备", invalid_ids=_bad), 403

    is_platform = cur.role in ("admin", "superadmin") and cur.enterprise_id is None
    ok, failed_ids, released = 0, [], 0
    for did in ids:
        d = db.session.get(Device, did)
        if d is None:
            failed_ids.append(did)
            continue
        if not is_platform:
            # 用户侧：只能删除自己名下的，且只做"归还"
            if d.created_by != cur.id:
                failed_ids.append(did)
                continue
            try:
                from ..billing import wipe_device_data
                wipe_device_data(d)
                d.created_by = None
                d.sub_id = None
                d.enterprise_id = None
                released += 1
                ok += 1
            except Exception:  # noqa: BLE001
                failed_ids.append(did)
            continue
        try:
            orchestrator.delete_device(d.backend, d.serial)
            db.session.delete(d)
            ok += 1
        except Exception:  # noqa: BLE001
            failed_ids.append(did)
    db.session.commit()
    record_audit(cur.id, "batch_delete_device", "device", None,
                 {"count": ok, "failed": failed_ids, "released": released})
    if failed_ids:
        raise_alert("operation_failure", "warning",
                    f"批量删除失败 {len(failed_ids)} 台设备",
                    detail={"failed_device_ids": failed_ids})
    return jsonify(ok=ok, failed=len(failed_ids))


@devices_bp.post("/devices/batch-set-group")
@require_perm("device.group")
def batch_set_group():
    """把选中的设备批量加入指定分组（group_id 传 null 表示移出分组/未分组）。"""
    cur = g.current_user
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in (data.get("device_ids") or [])]
    group_id = data.get("group_id")
    if not ids:
        return jsonify(error="请选择至少一台设备"), 400
    if group_id is not None:
        g_ = db.session.get(Group, int(group_id))
        if g_ is None:
            return jsonify(error="分组不存在"), 404
        group_id = g_.id
    from .. import scoping
    _bad = scoping.foreign_device_ids(cur, ids)
    if _bad:
        return jsonify(error="无权操作部分设备", invalid_ids=_bad), 403
    ok, failed_ids = 0, []
    for did in ids:
        d = db.session.get(Device, did)
        if d is None:
            failed_ids.append(did)
            continue
        d.group_id = group_id
        ok += 1
    db.session.commit()
    record_audit(cur.id, "batch_set_group", "device", None,
                 {"count": ok, "group_id": group_id})
    if failed_ids:
        raise_alert("operation_failure", "warning",
                    f"批量设置分组失败 {len(failed_ids)} 台设备",
                    detail={"failed_device_ids": failed_ids})
    return jsonify(ok=ok, failed=len(failed_ids))


@devices_bp.post("/devices/batch-power")
@require_perm("device.power")
def batch_power():
    """批量电源控制：start | stop | restart（真实 redroid 容器）。"""
    cur = g.current_user
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    ids = [int(i) for i in (data.get("device_ids") or [])]
    if action not in ("start", "stop", "restart"):
        return jsonify(error="action 必须是 start/stop/restart"), 400
    if not ids:
        return jsonify(error="请选择至少一台设备"), 400
    # 计费拦截：开机/重启需要有效套餐（关机放行）
    if action in ("start", "restart"):
        block = _billing_block(cur)
        if block:
            return block
    from .. import scoping
    _bad = scoping.foreign_device_ids(cur, ids)
    if _bad:
        return jsonify(error="无权操作部分设备", invalid_ids=_bad), 403
    ok, failed_ids = 0, []
    for did in ids:
        d = db.session.get(Device, did)
        if d is None:
            failed_ids.append(did)
            continue
        try:
            if d.backend == "redroid" and ":" in d.serial:
                port = int(d.serial.split(":")[-1])
                result = orchestrator.container_power(port, action)
                if not result.get("ok"):
                    failed_ids.append(did)
                    continue
            if action == "start":
                d.status = "running"
            elif action == "stop":
                d.status = "stopped"
            # restart：状态保持 running
            d.last_seen = datetime.utcnow()
            ok += 1
        except Exception:  # noqa: BLE001
            failed_ids.append(did)
    db.session.commit()
    record_audit(cur.id, f"batch_power_{action}", "device", None,
                 {"count": ok, "failed": len(failed_ids)})
    if failed_ids:
        raise_alert("operation_failure", "warning",
                    f"批量{'重启' if action == 'restart' else '开机' if action == 'start' else '关机'}失败 {len(failed_ids)} 台",
                    detail={"failed_device_ids": failed_ids})
    return jsonify(ok=ok, failed=len(failed_ids))


@devices_bp.post("/devices/install-apk")
@require_perm("device.apk")
def batch_install_apk():
    """上传 APK 并批量安装到所选设备（multipart: file + device_ids）。"""
    cur = g.current_user
    ids = [int(i) for i in request.form.get("device_ids", "").split(",") if i.strip()]
    if not ids:
        return jsonify(error="请选择至少一台设备"), 400
    from .. import scoping
    _bad = scoping.foreign_device_ids(cur, ids)
    if _bad:
        return jsonify(error="无权操作部分设备", invalid_ids=_bad), 403
    if "file" not in request.files:
        return jsonify(error="缺少 apk 文件字段 file"), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify(error="文件名不能为空"), 400
    # 不靠后缀判断：下载器给的临时名可能是 xxx.apk.1_tmp 之类，
    # 只认 APK 的 ZIP 头（PK\x03\x04）。
    ext = os.path.splitext(f.filename)[1].lower()
    _head = f.stream.read(4)
    try:
        f.stream.seek(0)
    except Exception:      # noqa: BLE001
        pass
    _is_zip = _head[:4] == b"PK\x03\x04"
    if ext != ".apk" and not _is_zip:
        return jsonify(error="这个文件不是 APK（既没有 .apk 后缀，也不是 ZIP 包）"), 400

    upload_dir = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_dir, exist_ok=True)
    stored = uuid.uuid4().hex + ".apk"   # 统一用 .apk 落地
    save_path = os.path.join(upload_dir, stored)
    f.save(save_path)

    ok, failed_ids = 0, []
    try:
        for did in ids:
            d = db.session.get(Device, did)
            if d is None or d.backend != "redroid":
                failed_ids.append(did)
                continue
            result = orchestrator.install_apk(d.serial, save_path)
            if result.get("ok"):
                ok += 1
            else:
                failed_ids.append(did)
    finally:
        try:
            os.remove(save_path)
        except OSError:
            pass
    db.session.commit()
    record_audit(cur.id, "install_apk", "device", None,
                 {"filename": f.filename, "ok": ok, "failed": len(failed_ids)})
    if failed_ids:
        raise_alert("operation_failure", "warning",
                    f"APK 安装失败 {len(failed_ids)} 台设备",
                    detail={"failed_device_ids": failed_ids})
    return jsonify(ok=ok, failed=len(failed_ids))


@devices_bp.get("/devices/<int:did>")
@login_required
def get_device(did):
    d = db.session.get(Device, did)
    if d is None:
        return jsonify(error="设备不存在"), 404
    return jsonify(_dev_to_dict(d))


@devices_bp.patch("/devices/<int:did>")
@login_required
def update_device(did):
    """改名：设备归属人自己可以改，管理员可以对任意设备改。"""
    cur = g.current_user
    d = db.session.get(Device, did)
    if d is None:
        return jsonify(error="设备不存在"), 404
    is_privileged = cur.role in ("admin", "superadmin")
    if not is_privileged and d.created_by != cur.id:
        return jsonify(error="只能修改自己名下的云手机"), 403
    data = request.get_json(silent=True) or {}
    if "name" not in data:
        return jsonify(error="没有需要修改的字段"), 400
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify(error="设备名不能为空"), 400
    if len(name) > 32:
        return jsonify(error="设备名最长 32 个字符"), 400
    old = d.name
    d.name = name
    db.session.commit()
    record_audit(cur.id, "rename_device", "device", d.id, {"from": old, "to": name})
    return jsonify(_dev_to_dict(d))


@devices_bp.post("/devices/<int:did>/control/<action>")
@login_required
def control(did, action):
    cur = g.current_user
    d = db.session.get(Device, did)
    if d is None:
        return jsonify(error="设备不存在"), 404
    # 管理员 / 运营可控制任意设备；普通用户只能操作自己名下的云手机
    from .. import scoping
    if not scoping.is_privileged(cur) and d.created_by != cur.id:
        return jsonify(error="无权操作该设备"), 403
    payload = request.get_json(silent=True) or {}
    try:
        result = orchestrator.control_device(d.backend, d.serial, action, payload)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        # 操作失败 → 产生一条 operation_failure 告警（dedup 防刷屏）
        try:
            raise_alert("operation_failure", "critical",
                        f"设备 {d.name} 操控动作 {action} 失败：{str(e)}",
                        device_id=d.id,
                        detail={"action": action, "error": str(e)})
        except Exception:
            db.session.rollback()
        return jsonify(error=str(e)), 501
    # orchestrator 内部会把个别失败包成 ok=False 返回（不抛异常），
    # 这里统一翻成非 2xx，否则前端只会看到"成功"却没有效果。
    if isinstance(result, dict) and result.get("ok") is False:
        detail = str(result.get("error") or result.get("result") or "操作失败")
        try:
            raise_alert("operation_failure", "critical",
                        f"设备 {d.name} 操控动作 {action} 失败：{detail}",
                        device_id=d.id,
                        detail={"action": action, "error": detail})
        except Exception:  # noqa: BLE001
            db.session.rollback()
        return jsonify(error=detail), 501
    # 操控成功 → 刷新心跳 + 写设备日志；这些次要写入失败不回滚控制结果
    try:
        d.last_seen = datetime.utcnow()
        db.session.add(DeviceLog(device_id=d.id, level="info",
                                 message=f"control {action}: {json.dumps(payload, ensure_ascii=False)}"))
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
    try:
        record_audit(cur.id, "control_device", "device", d.id, {"action": action})
    except Exception:  # noqa: BLE001
        db.session.rollback()
    return jsonify(result)
    return jsonify(result)


@devices_bp.delete("/devices/<int:did>")
@login_required
def delete_device(did):
    """管理员：真删容器和记录；普通用户：清数据并归还公共池（不删容器）。"""
    cur = g.current_user
    d = db.session.get(Device, did)
    if d is None:
        return jsonify(error="设备不存在"), 404
    if cur.role not in ("admin", "superadmin"):
        if d.created_by != cur.id:
            return jsonify(error="只能删除自己名下的云手机"), 403
        from ..billing import wipe_device_data
        wipe_device_data(d)
        d.created_by = None
        d.sub_id = None
        d.enterprise_id = None
        db.session.commit()
        record_audit(cur.id, "release_device", "device", did, {"name": d.name})
        return jsonify(message="已回收：用户数据已清理，云手机已归还公共池")
    orchestrator.delete_device(d.backend, d.serial)
    db.session.delete(d)
    db.session.commit()
    record_audit(cur.id, "delete_device", "device", did, {"name": d.name})
    return jsonify(message="已删除")


@devices_bp.post("/devices/<int:did>/power/<action>")
@login_required
def device_power(did, action):
    """redroid 容器电源控制：start | stop | restart。"""
    cur = g.current_user
    d = db.session.get(Device, did)
    if d is None:
        return jsonify(error="设备不存在"), 404
    # 管理员 / 运营可操作任意设备；普通用户只能开关自己名下的云手机
    from .. import scoping
    if not scoping.is_privileged(cur) and d.created_by != cur.id:
        return jsonify(error="无权操作该设备"), 403
    if d.backend != "redroid" or ":" not in d.serial:
        return jsonify(error="仅支持 redroid 设备"), 400
    if action not in ("start", "stop", "restart"):
        return jsonify(error="未知电源操作"), 400
    # 计费拦截：开机/重启需要有效套餐（关机放行）
    if action in ("start", "restart"):
        block = _billing_block(cur)
        if block:
            return block

    try:
        # 中继设备（127.0.0.1:36xxx）走服务器凭据；直连/本地容器仍走 orchestrator
        if (d.serial or "").startswith("127.0.0.1:"):
            result = _container_power_via_server(d, action)
        else:
            port = int(d.serial.split(":")[-1])
            result = orchestrator.container_power(port, action)
        if not result.get("ok"):
            return jsonify(error=result.get("error", "操作失败")), 501

        # 更新设备状态
        if action == "start":
            d.status = "running"
            d.last_seen = datetime.utcnow()
        elif action == "stop":
            d.status = "stopped"
        db.session.commit()

        record_audit(cur.id, f"power_{action}", "device", did,
                     {"serial": d.serial})
        return jsonify(result)
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 501


# ---------- 真实设备截图 ----------
@devices_bp.get("/devices/<int:did>/screenshot")
@login_required
def device_screenshot(did):
    """获取设备实时截图（PNG）。redroid 设备走 ADB screencap，simulator 返回占位图。"""
    d = db.session.get(Device, did)
    if d is None:
        return jsonify(error="设备不存在"), 404

    if d.backend == "redroid":
        img = orchestrator.get_screenshot(d.serial)
        if img is None:
            return jsonify(error="截图失败，设备可能离线"), 502
        # 后端已压成 JPEG 缩略图（Pillow 不可用时会是原 PNG），按实际格式返回
        is_jpeg = img[:3] == b"\xff\xd8\xff"
        return send_file(io.BytesIO(img),
                         mimetype="image/jpeg" if is_jpeg else "image/png",
                         download_name=f"device_{did}.{'jpg' if is_jpeg else 'png'}")

    # simulator：返回 SVG 占位图
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="360" height="640">
<rect width="360" height="640" fill="#1a1a2e"/>
<text x="180" y="320" fill="#666" font-size="16" text-anchor="middle" font-family="sans-serif">Simulator Device</text>
<text x="180" y="350" fill="#444" font-size="12" text-anchor="middle" font-family="sans-serif">{d.name}</text>
</svg>'''
    return send_file(io.BytesIO(svg.encode()), mimetype="image/svg+xml")


# ---------- 设备详情（实时读取，不读缓存/配置值） ----------
# 一次 adb shell 往返拿全部字段；容器没有单独资源限额时（生产容器即如此），
# nproc / MemTotal 反映的是容器可见的宿主机资源，df 取 /data 所在分区总容量。
_SPEC_PROBE_CMD = (
    "echo @@android=$(getprop ro.build.version.release); "
    "echo @@sdk=$(getprop ro.build.version.sdk); "
    "echo @@model=$(getprop ro.product.model); "
    "echo @@size=$(wm size | head -n1); "
    "echo @@cores=$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo); "
    "echo @@mem=$(grep MemTotal /proc/meminfo); "
    "echo @@disk=$(df -k /data 2>/dev/null | tail -n1)"
)


def _fmt_gb(kb: float) -> str:
    """KB → GB 文本：接近整数就取整（8GB），否则保留一位小数（62.7GB）。"""
    gb = kb / 1048576.0
    if abs(gb - round(gb)) < 0.05:
        return "%dGB" % round(gb)
    return "%.1fGB" % gb


def _parse_spec_probe(raw: str) -> dict:
    vals = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith("@@"):
            key, _, value = line[2:].partition("=")
            vals[key.strip()] = value.strip()
    return vals


@devices_bp.get("/devices/<int:did>/specs")
@login_required
def device_specs(did):
    """设备详情：实时从设备读取（Android / 分辨率 / 核数 / 内存 / 存储）。

    数据来源为 adb shell 现场探测，不是数据库里的记录值；设备离线时返回 502，
    前端仍可用列表里的基础字段渲染。CPU / 内存 / 存储为容器可见资源，
    容器未单独限额时与同服务器云手机共享宿主机。
    """
    import re
    d = db.session.get(Device, did)
    if d is None:
        return jsonify(error="设备不存在"), 404
    if d.backend != "redroid" or not d.serial:
        return jsonify(error="该设备不支持实时详情（仅真实云手机）"), 400

    serial = d.serial
    try:
        orchestrator._ensure_adb_connected(serial)
    except Exception:  # noqa: BLE001
        pass
    rc, out, err = orchestrator._run_adb(
        ["-s", serial, "shell", _SPEC_PROBE_CMD], timeout=20)
    vals = _parse_spec_probe((out or b"").decode("utf-8", errors="replace"))
    if rc != 0 or "android" not in vals:
        detail = (err or b"").decode("utf-8", errors="replace").strip()
        return jsonify(error="实时信息读取失败（设备可能离线）%s"
                       % (("：" + detail[:120]) if detail else "")), 502

    m = re.search(r"(\d+)\s*x\s*(\d+)", vals.get("size", ""))
    resolution = "%s×%s" % (m.group(1), m.group(2)) if m else ""

    cores = int(vals["cores"]) if vals.get("cores", "").strip().isdigit() else 0

    mem_kb = 0
    mm = re.search(r"(\d+)", vals.get("mem", ""))
    if mm:
        mem_kb = int(mm.group(1))

    disk_kb = 0
    parts = vals.get("disk", "").split()
    if len(parts) >= 2 and parts[1].isdigit():
        disk_kb = int(parts[1])

    spec = {
        "model": vals.get("model", "") or _dev_model(d),
        "android": vals.get("android", ""),
        "sdk": vals.get("sdk", ""),
        "resolution": resolution,
        "cpu": ("%d核" % cores) if cores else "",
        "memory": _fmt_gb(mem_kb) if mem_kb else "",
        "storage": _fmt_gb(disk_kb) if disk_kb else "",
    }
    spec["summary"] = " · ".join(p for p in [
        ("Android " + spec["android"]) if spec["android"] else "",
        spec["resolution"], spec["cpu"], spec["memory"], spec["storage"]] if p)
    return jsonify(ok=True, device_id=d.id, serial=serial, **spec)


# ---------- 发现 / 导入真实 ADB 设备 ----------
@devices_bp.get("/devices/discover")
@require_platform
def discover_devices():
    """扫描当前已连接的 ADB 设备，返回可导入列表。"""
    adb_devices = orchestrator.list_adb_devices()
    # 过滤出在线设备
    online = [d for d in adb_devices if d["status"] == "device"]
    # 检查哪些已经在数据库中
    existing_serials = {d.serial for d in db.session.query(Device).all()}
    result = []
    for d in online:
        info = orchestrator.get_device_info(d["serial"])
        result.append({
            "serial": d["serial"],
            "model": info.get("model", "unknown"),
            "android_version": info.get("android_version", "unknown"),
            "already_imported": d["serial"] in existing_serials,
        })
    return jsonify(result)


@devices_bp.post("/devices/import")
@require_platform
def import_device():
    """导入一台已连接的真实 ADB 设备到数据库。"""
    cur = g.current_user
    data = request.get_json(silent=True) or {}
    serial = (data.get("serial") or "").strip()
    name = (data.get("name") or "").strip()
    if not serial:
        return jsonify(error="serial 必填"), 400

    # 检查是否已导入
    existing = db.session.query(Device).filter_by(serial=serial).first()
    if existing:
        return jsonify(error="该设备已导入", device_id=existing.id), 409

    # 确保 ADB 已连接
    if ":" in serial:
        orchestrator.adb_connect(serial)

    info = orchestrator.get_device_info(serial)
    server = orchestrator._server_ip()
    fp = json.dumps({
        "model": info.get("model", "redroid"),
        "android_version": info.get("android_version", "12"),
        "brand": info.get("brand", ""),
    }, ensure_ascii=False)

    device_name = name or f"redroid-{serial.split(':')[-1]}"
    d = Device(
        name=device_name,
        status="running",
        serial=serial,
        backend="redroid",
        ip=server,
        fingerprint=fp,
        created_by=cur.id,
        last_seen=datetime.utcnow(),
        cpu=0.0,
        mem=0.0,
    )
    db.session.add(d)
    db.session.commit()
    record_audit(cur.id, "import_device", "device", d.id,
                 {"serial": serial, "name": device_name})
    return jsonify(_dev_to_dict(d)), 201


# ---------- 批量导入多服务器设备 ----------
@devices_bp.post("/devices/bulk-import")
@require_platform
def bulk_import_devices():
    """批量导入 redroid 设备（按 serial 去重，已存在则跳过）。

    请求体: {"devices": [{"serial": "IP:port", "name": "xxx", "ip": "IP"}, ...]}
    返回: {"added": N, "skipped": N, "devices": [...]}
    """
    cur = g.current_user
    data = request.get_json(silent=True) or {}
    items = data.get("devices") or []
    if not items:
        return jsonify(error="devices 列表不能为空"), 400

    # 默认 Pixel 6 设备指纹
    default_fp = json.dumps({
        "model": "Pixel 6",
        "android_version": "12",
        "brand": "Google",
    }, ensure_ascii=False)

    added_count = 0
    skipped_count = 0
    result_devices = []

    for item in items:
        serial = (item.get("serial") or "").strip()
        name = (item.get("name") or "").strip()
        ip = (item.get("ip") or "").strip()
        if not serial:
            skipped_count += 1
            continue
        # 按 serial 去重，已存在则跳过
        existing = db.session.query(Device).filter_by(serial=serial).first()
        if existing:
            skipped_count += 1
            result_devices.append(_dev_to_dict(existing))
            continue
        device_name = name or f"redroid-{serial.split(':')[-1]}"
        d = Device(
            name=device_name,
            status="running",
            serial=serial,
            backend="redroid",
            ip=ip,
            fingerprint=default_fp,
            created_by=cur.id,
            last_seen=datetime.utcnow(),
            cpu=0.0,
            mem=0.0,
        )
        db.session.add(d)
        added_count += 1
        result_devices.append(_dev_to_dict(d))

    db.session.commit()
    record_audit(cur.id, "bulk_import_devices", "device", None,
                 {"added": added_count, "skipped": skipped_count})
    return jsonify(added=added_count, skipped=skipped_count, devices=result_devices)


# ---------- 服务器列表 ----------
@devices_bp.get("/devices/servers")
@login_required
def list_servers():
    """返回数据库中所有不同的服务器 IP 列表（从 Device.ip 或 serial 前缀提取）。""" 
    from .. import scoping
    servers = set()
    devices = scoping.scope_query(db.session.query(Device), g.current_user).all()
    for d in devices:
        if d.ip:
            servers.add(d.ip)
        if d.serial and ":" in d.serial:
            prefix = d.serial.split(":")[0]
            if prefix:
                servers.add(prefix)
    return jsonify(sorted(servers))


# ---------- 同步服务器设备 ----------
@devices_bp.post("/devices/sync")
@require_platform
def sync_devices():
    """扫描服务器上实际运行的 ADB 设备，与数据库同步。

    - 服务器上有但数据库中没有的 redroid 设备 → 自动导入
    - 数据库中有但服务器上没有的 redroid 设备 → 标记为 error
    - simulator 设备不受影响
    """
    cur = g.current_user
    # 1. 扫描服务器真实运行的 redroid 容器端口（以容器状态为准，而非易失的 adb 连接）
    server = orchestrator._server_ip()
    adb_host = orchestrator._adb_host()
    container_ports = orchestrator.list_server_redroid_ports()
    tunnel_map = orchestrator.list_server_tunnel_ports()
    online_serials = {f"{adb_host}:{tunnel_map.get(p, p)}" for p in container_ports}
    # 对在线设备重新 adb_connect，确保可操控
    for _serial in online_serials:
        try:
            orchestrator.adb_connect(_serial)
        except Exception:
            pass

    # 2. 获取数据库中当前服务器的 redroid 设备（多节点下只同步本服务器，避免误标其他节点）
    server_ip = orchestrator._server_ip()
    db_devices = [d for d in db.session.query(Device).filter_by(backend="redroid").all()
                  if (d.serial and d.serial.startswith(f"{server_ip}:")) or d.ip == server_ip]
    db_serials = {d.serial for d in db_devices}

    # 3. 需要新增的设备（服务器有，数据库没有）
    to_add = online_serials - db_serials
    added = []
    for serial in to_add:
        # 只同步 IP:PORT 格式的 redroid 设备
        if ":" not in serial:
            continue
        # 插入前再查一次（并发请求可能已插入，配合唯一索引兜底）
        if db.session.query(Device).filter_by(backend="redroid", serial=serial).first():
            continue
        orchestrator.adb_connect(serial)
        info = orchestrator.get_device_info(serial)
        server = orchestrator._server_ip()
        fp = json.dumps({
            "model": info.get("model", "redroid"),
            "android_version": info.get("android_version", "12"),
            "brand": info.get("brand", ""),
        }, ensure_ascii=False)
        port = serial.split(":")[-1]
        d = Device(
            name=f"redroid-{port}",
            status="running",
            serial=serial,
            backend="redroid",
            ip=server,
            fingerprint=fp,
            created_by=cur.id,
            last_seen=datetime.utcnow(),
            cpu=0.0,
            mem=0.0,
        )
        db.session.add(d)
        added.append(serial)

    # 4. 需要标记离线的设备（数据库有，服务器没有）
    to_offline = db_serials - online_serials
    offlined = []
    for d in db_devices:
        if d.serial in to_offline and d.status != "error":
            d.status = "error"
            offlined.append(d.serial)

    # 5. 恢复在线的设备（之前标记为 error，现在又在线了）
    restored = []
    for d in db_devices:
        if d.serial in online_serials and d.status == "error":
            d.status = "running"
            d.last_seen = datetime.utcnow()
            restored.append(d.serial)

    try:
        db.session.commit()
    except Exception:
        # 并发/重复插入被唯一索引拦截：回滚后按当前库状态返回，避免 500
        db.session.rollback()
        db_devices = db.session.query(Device).filter_by(backend="redroid").all()
        live = {d.serial for d in db_devices}
        added = [s for s in added if s in live]
        offlined = [s for s in offlined if s not in live]
        restored = [s for s in restored if s in live]
    record_audit(cur.id, "sync_devices", "device", None,
                 {"added": len(added), "offlined": len(offlined), "restored": len(restored)})

    return jsonify({
        "added": added,
        "offlined": offlined,
        "restored": restored,
        "online_count": len(online_serials),
        "db_count": len(db_serials),
    })


def _dev_model(d: Device) -> str:
    """从设备指纹中解析手机型号。"""
    try:
        fp = json.loads(d.fingerprint or "{}")
        return (fp.get("model") or "").strip()
    except (json.JSONDecodeError, TypeError):
        return ""


def _dev_plan_name(d: Device) -> str:
    """设备归属套餐名（通过 sub_id 反查）。"""
    if not d.sub_id:
        return ""
    try:
        from ..models import Subscription
        sub = db.session.get(Subscription, d.sub_id)
        return sub.plan.name if (sub and sub.plan) else ""
    except Exception:  # noqa: BLE001
        return ""


def _scrcpy_stream_host(d: Device) -> str:
    """返回投屏用的 ws-scrcpy 主机。

    现状（实测）：122 上的 ws-scrcpy 通过 adb 连到了全部在线设备，投屏正常；
    而公网服务器（204/209/243）上那几套 ws-scrcpy 实例已经陈旧——端口能开，
    但 adb 里没有设备，进 stream 就是黑屏。
    所以统一走「网关门禁」（默认 192.168.9.122），可用环境变量覆盖。
    """
    import os

    # 哪些服务器上的 ws-scrcpy 真的「adb 里有这台设备」：
    #   · 122：adb 连到了全部在线容器（公网 204/209/243 与内网 99.x 都能播）
    #   · 117：USB 板子只挂在 117 自己的 adb 上，必须走 117
    # 其余服务器（204/209/243）的 ws-scrcpy 实例已陈旧，直连会黑屏。
    overrides = {"192.168.9.117": "192.168.9.117"}
    default = (os.getenv("SCRCPY_STREAM_HOST") or "192.168.9.122").strip()
    return overrides.get((d.ip or "").strip(), default)


def _scrcpy_udid(d: Device) -> str:
    """投屏（ws-scrcpy）用的设备 id —— 必须是**设备所在服务器** adb 里的那个名字。

    平台库里的 serial 是中继后的 `127.0.0.1:36xxx`，这个端口只存在于平台本机，
    服务器上的 ws-scrcpy 根本不认识它（所以 122 的机器一直黑屏）。
    服务器侧的真实名字在导入时存在 fingerprint.remote_serial：
      · 122 这类网络 ADB: 192.168.99.101:5555
      · 117 这类 USB 板子: 70503SUB01
    """
    import json as _json
    try:
        fp = _json.loads(d.fingerprint or "{}")
    except (TypeError, ValueError):
        fp = {}
    if isinstance(fp, dict):
        remote = str(fp.get("remote_serial") or "").strip()
        if remote:
            return remote
    return d.serial or ""


def _ssh_run(host, cmd, timeout=30):
    """直接 SSH 执行命令（host 需已登记在 servers 表）。"""
    from .. import ssh_probe
    from ..models import Server
    srv = db.session.query(Server).filter(Server.host == host).first()
    if srv is None:
        raise RuntimeError(f"{host} 未登记为服务器")
    cli = ssh_probe.ssh_connect(
        hostname=srv.host, port=int(srv.port or 22), username=srv.username,
        password=(srv.secret if (srv.auth_type or "password") != "key" else None),
        timeout=8, allow_agent=False, look_for_keys=False)
    try:
        return ssh_probe.exec_cmd(cli, cmd, timeout=timeout)
    finally:
        try:
            cli.close()
        except Exception:  # noqa: BLE001
            pass


def _ssh_run_via_jump(bastion_host, host, cmd, timeout=30):
    """经跳板机 SSH 到内网宿主（99.x 平台本机不可达，但 122 能通）。"""
    import paramiko
    from .. import ssh_probe
    from ..models import Server

    b = db.session.query(Server).filter(Server.host == bastion_host).first()
    t = db.session.query(Server).filter(Server.host == host).first()
    if b is None or t is None:
        raise RuntimeError("跳板机或目标宿主机未登记")

    outer = paramiko.SSHClient()
    outer.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    outer.connect(hostname=b.host, port=int(b.port or 22), username=b.username,
                  password=(b.secret if (b.auth_type or "password") != "key" else None),
                  timeout=8, allow_agent=False, look_for_keys=False)
    try:
        ch = outer.get_transport().open_channel("direct-tcpip",
                                                (t.host, int(t.port or 22)), ("127.0.0.1", 0))
        inner = paramiko.SSHClient()
        inner.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        inner.connect(hostname=t.host, port=int(t.port or 22), username=t.username,
                      password=(t.secret if (t.auth_type or "password") != "key" else None),
                      sock=ch, timeout=8, allow_agent=False, look_for_keys=False)
        try:
            return ssh_probe.exec_cmd(inner, cmd, timeout=timeout)
        finally:
            inner.close()
    finally:
        try:
            outer.close()
        except Exception:  # noqa: BLE001
            pass


def _container_power_via_server(dev, action: str) -> dict:
    """中继设备的电源控制。

    serial 是平台本地中继端口（127.0.0.1:36xxx），真正要操作的是
    fingerprint.remote_serial 指向的宿主机（如 192.168.99.121:5556）上的
    容器 redroid-<adb端口>。99.x 平台本机连不到，用设备登记时的服务器（dev.ip）
    当跳板机。
    """
    import json as _json

    try:
        fp = _json.loads(dev.fingerprint or "{}")
    except ValueError:
        fp = {}
    remote = (fp.get("remote_serial") or "") if isinstance(fp, dict) else ""
    if not remote or ":" not in remote:
        return {"ok": False, "error": "设备缺少服务器侧地址（remote_serial），无法控制电源"}
    rhost, rport = remote.rsplit(":", 1)
    name = f"redroid-{rport}"

    def _run(cmd, timeout=40):
        try:
            return _ssh_run(rhost, cmd, timeout=timeout)
        except Exception:  # noqa: BLE001 - 直连失败改走跳板机
            return _ssh_run_via_jump(dev.ip, rhost, cmd, timeout=timeout)

    try:
        rc, out, err = _run("docker ps -a --format '{{.Names}}' | grep -x " + name + " || true",
                            timeout=25)
        if not (out or "").strip():
            return {"ok": False, "error": f"宿主机 {rhost} 上找不到容器 {name}"}
        rc, out, err = _run(f"docker {action} {name}", timeout=60)
        if rc != 0:
            return {"ok": False, "error": f"{action} 失败：{(err or out).strip()[:120]}"}
        if action in ("start", "restart"):
            try:
                from .. import orchestrator as _orch
                _orch.adb_connect(dev.serial)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "action": action, "container": name, "host": rhost}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"连接宿主机 {rhost} 失败：{str(e)[:120]}"}


def _dev_to_dict(d: Device) -> dict:
    return {
        "scrcpy_udid": _scrcpy_udid(d),
        "scrcpy_host": _scrcpy_stream_host(d),
        "scrcpy_port": 8100,
        # 云手机规格（由创建时的套餐写入 / 或由 adb 采集回填）
        "spec_resolution": d.spec_resolution or "",
        "spec_cpu": d.spec_cpu or "",
        "spec_memory": d.spec_memory or "",
        "spec_storage": d.spec_storage or "",
        "spec_android": d.spec_android or "",
        "id": d.id,
        "name": d.name,
        "group_id": d.group_id,
        "status": d.status,
        "serial": d.serial,
        "backend": d.backend,
        "model": _dev_model(d) or ("Pixel-Cloud" if d.backend == "simulator" else "redroid"),
        "ip": d.ip,
        "fingerprint": d.fingerprint,
        "created_by": d.created_by,
        "sub_id": d.sub_id,
        "plan_name": _dev_plan_name(d),
        "last_seen": d.last_seen.isoformat() if d.last_seen else None,
        "cpu": d.cpu,
        "mem": d.mem,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


# ---------- 云手机轮次运行模式 ----------
def _rotation_dict(cfg) -> dict:
    per_hours = round(24 / max(cfg.rounds, 1), 2)
    return {
        "enabled": cfg.enabled,
        "rounds": cfg.rounds,
        "devices_per_round": cfg.devices_per_round,
        "round_index": cfg.round_index,
        "per_round_hours": per_hours,
        "started_at": cfg.started_at.isoformat() if cfg.started_at else None,
        "next_round_at": cfg.next_round_at.isoformat() if cfg.next_round_at else None,
    }


@devices_bp.get("/rotation")
@login_required
def get_rotation():
    """获取轮次运行配置：管理员看全局，普通用户看自己的。"""
    cur = g.current_user
    if cur.role in ("admin", "superadmin"):
        cfg = orchestrator.get_rotation_config()
        return jsonify(_rotation_dict(cfg))
    from .. import user_rotation as _ur
    return jsonify(_ur.config_dict(_ur.get_config(cur.id), cur.id))


@devices_bp.put("/rotation")
@login_required
def update_rotation():
    """开启/关闭轮次运行，或修改轮数 / 每轮云手机数量。

    管理员：全局轮次 = 销毁重建容器；
    普通用户：自己的轮次 = 每轮把名下若干台「清数据 → 收回 → 重新分配」。
    """
    import threading
    from datetime import timedelta
    cur = g.current_user
    data = request.get_json(silent=True) or {}

    if cur.role not in ("admin", "superadmin"):
        from .. import user_rotation as _ur
        cfg = _ur.get_config(cur.id)
        if "rounds" in data:
            try:
                rounds = int(data.get("rounds"))
            except (TypeError, ValueError):
                return jsonify(error="rounds 必须是整数"), 400
            if not (1 <= rounds <= 24):
                return jsonify(error="rounds 需在 1..24 之间"), 400
            cfg.rounds = rounds
        if "devices_per_round" in data:
            try:
                n = int(data.get("devices_per_round"))
            except (TypeError, ValueError):
                return jsonify(error="devices_per_round 必须是整数"), 400
            if not (1 <= n <= 200):
                return jsonify(error="devices_per_round 需在 1..200 之间"), 400
            cfg.devices_per_round = n
        if "enabled" in data:
            enabled = bool(data.get("enabled"))
            cfg.enabled = enabled
            if enabled:
                cfg.round_index = 1
                cfg.started_at = datetime.utcnow()
                cfg.next_round_at = cfg.started_at + timedelta(hours=24 / max(1, cfg.rounds))
                db.session.commit()
                from flask import current_app
                _ur.run_round_async(current_app._get_current_object(),
                                    cur.id, cfg.devices_per_round)
            else:
                cfg.started_at = None
                cfg.next_round_at = None
        db.session.commit()
        record_audit(cur.id, "update_user_rotation", "config", cfg.id,
                     {"enabled": cfg.enabled, "rounds": cfg.rounds,
                      "devices_per_round": cfg.devices_per_round})
        return jsonify(_ur.config_dict(cfg, cur.id))

    cfg = orchestrator.get_rotation_config()

    if "rounds" in data:
        rounds = data.get("rounds")
        try:
            rounds = int(rounds)
        except (TypeError, ValueError):
            return jsonify(error="rounds 必须是整数"), 400
        if not (1 <= rounds <= 24):
            return jsonify(error="rounds 需在 1..24 之间"), 400
        cfg.rounds = rounds

    if "devices_per_round" in data:
        n = data.get("devices_per_round")
        try:
            n = int(n)
        except (TypeError, ValueError):
            return jsonify(error="devices_per_round 必须是整数"), 400
        if not (1 <= n <= 200):
            return jsonify(error="devices_per_round 需在 1..200 之间"), 400
        cfg.devices_per_round = n

    if "enabled" in data:
        enabled = bool(data.get("enabled"))
        cfg.enabled = enabled
        if enabled:
            # 开启：重置为第 1 轮，从当前时刻起算
            cfg.round_index = 1
            cfg.started_at = datetime.utcnow()
            cfg.next_round_at = cfg.started_at + timedelta(hours=24 / cfg.rounds)
            # 立即按 devices_per_round 销毁重建一批（异步，避免阻塞 API）
            target = cfg.devices_per_round
            threading.Thread(
                target=orchestrator.rotate_redroid_devices,
                kwargs={"target_count": target}, daemon=True,
            ).start()
        else:
            cfg.started_at = None
            cfg.next_round_at = None

    db.session.commit()
    record_audit(g.current_user.id, "update_rotation", "config", 1,
                 {"enabled": cfg.enabled, "rounds": cfg.rounds,
                  "devices_per_round": cfg.devices_per_round})
    return jsonify(_rotation_dict(cfg))


@devices_bp.post("/devices/egress/bind-batch")
@require_perm("device.assign")
def bind_egress_batch():
    """给一批未绑出口的设备轮转绑定 SOCKS5 线路（一台一号，避免同 IP 聚类）。"""
    from .. import egress
    data = request.get_json(silent=True) or {}
    try:
        limit = int(data.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    result = egress.bind_batch(limit=limit, only_unbound=bool(data.get("only_unbound", True)))
    if not result.get("ok"):
        return jsonify(error=result.get("error") or "批量绑定失败"), 501
    record_audit(g.current_user.id, "bind_egress_batch", "device", None,
                 {"bound": result.get("bound"), "lines": result.get("lines")})
    return jsonify(result)


# ---------------------------------------------------------------------------
# 设备指纹：给已有云手机换一套真机指纹（会重启容器，数据保留）
# ---------------------------------------------------------------------------
@devices_bp.get("/devices/<int:did>/env")
@require_perm("device.manage")
def get_device_env(did):
    from .. import device_env
    d = db.session.get(Device, did)
    if d is None:
        return jsonify(error="设备不存在"), 404
    info = device_env.sensor_status(d.serial) if d.serial else {}
    return jsonify(profile=device_env.load(d), sensors=info,
                   serial=d.serial, backend=d.backend)


@devices_bp.post("/devices/<int:did>/env")
@require_perm("device.manage")
def rebuild_device_env(did):
    from .. import device_env
    d = db.session.get(Device, did)
    if d is None:
        return jsonify(error="设备不存在"), 404
    if d.backend != "redroid" or ":" not in (d.serial or ""):
        return jsonify(error="仅支持 redroid 设备"), 400
    data = request.get_json(silent=True) or {}
    if not data.get("recreate"):
        return jsonify(
            need_confirm=True,
            message="这会用新指纹重建容器（保留 /data），云手机会短暂重启",
            current=device_env.load(d),
            sensors=device_env.sensor_status(d.serial),
            next=device_env.generate_profile(int(d.serial.split(":")[-1])),
        ), 200
    try:
        port = int(d.serial.split(":")[-1])
    except ValueError:
        return jsonify(error="设备序列号异常"), 400
    import uuid
    profile = device_env.generate_profile(port, salt=uuid.uuid4().hex[:8])
    result = orchestrator.recreate_redroid_container(port, profile=profile)
    if not result.get("ok"):
        return jsonify(error=result.get("error") or "重建失败"), 501
    d.fingerprint = device_env.dump(profile)
    d.status = "running"
    db.session.commit()
    record_audit(g.current_user.id, "rebuild_device_env", "device", d.id,
                 {"model": profile.get("model"), "serial": profile.get("serial")})
    return jsonify(ok=True, profile=profile)


# ---------------------------------------------------------------------------
# 出口 IP（一台一号）：把云手机绑到某条 SOCKS5 出口线路
# ---------------------------------------------------------------------------
@devices_bp.get("/devices/<int:did>/egress")
@login_required
def get_device_egress(did):
    from ..models import VpcLine
    d = db.session.get(Device, did)
    if d is None:
        return jsonify(error="设备不存在"), 404
    lines = (db.session.query(VpcLine).filter(VpcLine.status == "running")
             .order_by(VpcLine.id).all())
    return jsonify(
        egress_ip=d.egress_ip or "",
        egress_line_id=d.egress_line_id,
        lines=[{"id": ln.id, "name": ln.name, "host": ln.host, "port": ln.port,
                "last_egress_ip": ln.last_egress_ip or ""} for ln in lines],
    )


@devices_bp.post("/devices/<int:did>/egress")
@require_perm("device.assign")
def bind_device_egress(did):
    from .. import egress
    from ..models import VpcLine
    d = db.session.get(Device, did)
    if d is None:
        return jsonify(error="设备不存在"), 404
    data = request.get_json(silent=True) or {}
    line = None
    if data.get("line_id"):
        line = db.session.get(VpcLine, int(data["line_id"]))
        if line is None:
            return jsonify(error="出口线路不存在"), 404
    result = egress.bind(d, line)
    if not result.get("ok"):
        return jsonify(error=result.get("error") or "绑定失败"), 501
    record_audit(g.current_user.id, "bind_egress", "device", d.id,
                 {"line": result.get("line"), "egress_ip": result.get("egress_ip")})
    return jsonify(result)


@devices_bp.delete("/devices/<int:did>/egress")
@require_perm("device.assign")
def unbind_device_egress(did):
    from .. import egress
    d = db.session.get(Device, did)
    if d is None:
        return jsonify(error="设备不存在"), 404
    egress.unbind(d)
    record_audit(g.current_user.id, "unbind_egress", "device", d.id, {})
    return jsonify(message="已解绑出口", egress_ip=d.egress_ip or "")


# ---------------------------------------------------------------------------
# PC 摄像头 → 云手机虚拟摄像头（客户端「流媒体 → PC摄像头」）
# ---------------------------------------------------------------------------
# 客户端把本机摄像头经 ffmpeg 推成 HLS（本机 8088），再调用这里把地址交给云手机。
# 要让云手机摄像头真正显示该画面，设备/宿主机上必须有虚拟摄像头管线
# （如 v4l2loopback：ffmpeg -re -i <HLS> -f v4l2 /dev/videoN），命令模板由
# VIRTUAL_CAMERA_CMD / VIRTUAL_CAMERA_STOP_CMD 配置并在云手机服务器上执行。
# 未配置管线时接口返回 501 并说明原因——只登记地址，不假装已经下发到设备。
def _virtual_camera_cmd(tpl: str, dev, url: str) -> str:
    return (tpl.replace("{url}", url or "")
               .replace("{serial}", dev.serial or "")
               .replace("{device_id}", str(dev.id or ""))
               .replace("{ip}", dev.ip or ""))


def _run_virtual_camera_cmd(cmd: str) -> tuple:
    """在云手机服务器执行管线命令；未配置 SSH 时退化为在后端本机执行。"""
    if orchestrator._ssh_enabled():
        client = None
        try:
            client = orchestrator._ssh_connect()
            return orchestrator._ssh_exec(client, cmd, timeout=25)
        except Exception as e:  # noqa: BLE001
            return 1, "", f"SSH 执行失败: {e}"
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
    import subprocess
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=25)
        return (p.returncode,
                (p.stdout or b"").decode("utf-8", "ignore"),
                (p.stderr or b"").decode("utf-8", "ignore"))
    except Exception as e:  # noqa: BLE001
        return 1, "", f"本机执行失败: {e}"


def fs_rtmp_url(room_id: str, request):
    from .. import forward_stream as _fs
    return _fs.rtmp_url(room_id, request)


# 常见浏览器及其主 Activity（多浏览器设备直接 VIEW 会弹选择框，必须显式指定组件）
KNOWN_BROWSERS = [
    ("com.mmbox.xbrowser", "com.mmbox.xbrowser/.BrowserActivity"),          # redroid 机队自带
    ("acr.browser.lightning", "acr.browser.lightning/.MainActivity"),        # RK3399 真机自带
    ("com.android.chrome", "com.google.android.apps.chrome.Main"),
    ("org.mozilla.firefox", "org.mozilla.firefox/.App"),
]


def _device_open_url(dev, url: str):
    """在云手机上打开播放地址；返回 (ok, message)。

    优先用设备上已装的常见浏览器（显式组件，避免"链接打开方式"选择框），
    都不可用时退回 resolve-activity / 默认 VIEW。
    """
    if not dev.serial:
        return False, "设备缺少 serial"
    if not url:
        return False, "缺少播放地址"

    # 一次拿到已安装包列表
    installed = ""
    try:
        _rc, out, _err = orchestrator._run_adb(
            ["-s", dev.serial, "shell", "pm list packages"], timeout=20)
        installed = (out or b"").decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        installed = ""

    flags = '-a android.intent.action.VIEW -f 0x10000000'
    candidates = []
    for pkg, comp in KNOWN_BROWSERS:
        if ("package:" + pkg) in installed:
            candidates.append(comp)
    # 兜底：resolve-activity 的结果（可能仍是选择器）
    try:
        rc0, out0, _e0 = orchestrator._run_adb(
            ["-s", dev.serial, "shell",
             f"cmd package resolve-activity --brief -a android.intent.action.VIEW -d '{url}'"],
            timeout=15)
        if rc0 == 0:
            for line in (out0 or b"").decode("utf-8", "ignore").strip().splitlines():
                line = line.strip()
                if "/" in line and " " not in line and "ResolverActivity" not in line:
                    candidates.append(line)
    except Exception:  # noqa: BLE001
        pass

    last = ""
    for comp in candidates:
        rc, out, err = orchestrator._run_adb(
            ["-s", dev.serial, "shell", f'am start -n {comp} {flags} -d "{url}"'], timeout=25)
        text = ((out or b"").decode("utf-8", "ignore")
                + (err or b"").decode("utf-8", "ignore")).strip()
        if rc == 0 and "Error" not in text and "Exception" not in text:
            return True, comp + " | " + text[-160:]
        last = text[-200:]

    # 最后再试默认 VIEW
    rc, out, err = orchestrator._run_adb(
        ["-s", dev.serial, "shell", f'am start {flags} -d "{url}"'], timeout=25)
    text = ((out or b"").decode("utf-8", "ignore")
            + (err or b"").decode("utf-8", "ignore")).strip()
    if rc == 0 and "Error" not in text and "Exception" not in text:
        return True, "默认 VIEW | " + text[-160:]
    return False, (text[-300:] or last or f"adb 返回 {rc}")


def _virtual_camera_payload(dev) -> dict:
    from datetime import datetime as _dt
    return {
        "device_id": dev.id,
        "url": dev.virtual_camera_url or "",
        "updated_at": (dev.virtual_camera_updated_at.isoformat()
                       if dev.virtual_camera_updated_at else None),
        "pipeline_configured": bool(
            (current_app.config.get("VIRTUAL_CAMERA_CMD") or "").strip()),
        "checked_at": _dt.utcnow().isoformat(),
    }


@devices_bp.get("/devices/<int:device_id>/virtual-camera-stream")
@login_required
def get_virtual_camera_stream(device_id):
    """查询该云手机当前的虚拟摄像头源。"""
    dev = db.session.get(Device, device_id)
    if dev is None:
        return jsonify(error="设备不存在"), 404
    return jsonify(_virtual_camera_payload(dev))


@devices_bp.post("/devices/<int:device_id>/virtual-camera-stream")
@login_required
def set_virtual_camera_stream(device_id):
    """把 PC 摄像头 HLS 地址接入该云手机的虚拟摄像头（body: {"url": "http://..."}）。"""
    dev = db.session.get(Device, device_id)
    if dev is None:
        return jsonify(error="设备不存在"), 404
    data = request.get_json(silent=True) or {}
    url = str(data.get("url") or "").strip()
    if not url:
        return jsonify(error="缺少推流地址 url"), 400
    if not url.lower().startswith(("http://", "https://")):
        return jsonify(error="推流地址需为 http(s) 地址（HLS）"), 400

    dev.virtual_camera_url = url
    dev.virtual_camera_updated_at = datetime.utcnow()
    db.session.commit()

    tpl = (current_app.config.get("VIRTUAL_CAMERA_CMD") or "").strip()

    # 情况一：配了系统级虚拟摄像头管线（v4l2loopback 等）→ 真正把它接进相机
    # 情况二（当前部署）：云手机镜像没有相机 HAL，做不了系统相机；
    #   退化为「把画面投到云手机屏幕上」——在云手机浏览器里打开后端播放页（WHEP）。
    if not tpl:
        room = str(data.get("room") or "").strip()
        target = ""
        mode = ""
        note = ""

        # 方式一：云手机装了自带软解的播放器（VLC 等）→ 直接交给播放器播 RTMP，
        # 不依赖浏览器/系统解码，是最可靠的方式。
        pkg = (current_app.config.get("VIRTUAL_CAMERA_PLAYER_PKG") or "").strip()
        activity = (current_app.config.get("VIRTUAL_CAMERA_PLAYER_ACTIVITY") or "").strip()
        if pkg and activity and room and dev.serial:
            rc, out, _err = orchestrator._run_adb(
                ["-s", dev.serial, "shell", f"pm list packages {pkg}"], timeout=15)
            installed = pkg in (out or b"").decode("utf-8", "ignore")
            play_url = fs_rtmp_url(room, request) or url
            if installed and play_url:
                cmd = (f'am start -n {activity} -a android.intent.action.VIEW '
                       f'-d "{play_url}" -f 0x10000000')
                rc2, out2, err2 = orchestrator._run_adb(
                    ["-s", dev.serial, "shell", cmd], timeout=25)
                info = ((out2 or b"").decode("utf-8", "ignore")
                        + (err2 or b"").decode("utf-8", "ignore")).strip()
                if rc2 == 0:
                    record_audit(g.current_user.id, "set_virtual_camera", "device", dev.id,
                                 {"url": play_url, "mode": "player-app"})
                    return jsonify(ok=True, device_id=dev.id, url=url, applied=True,
                                   mode="player-app", target=play_url,
                                   player=pkg,
                                   note="已用云手机上的播放器打开本机摄像头画面（非系统相机）")
                note = "播放器启动失败：" + info[-200:]

        # 方式二：用云手机浏览器打开后端 WHEP 播放页
        player = ""
        if room:
            from urllib.parse import quote as _quote
            base = (current_app.config.get("STREAM_PLAYER_BASE") or "").strip().rstrip("/")
            if not base:
                base = request.host_url.rstrip("/")
            player = f"{base}/api/streams/player/{_quote(room, safe='')}"
        target = player or url
        ok, info = _device_open_url(dev, target)
        if not ok:
            return jsonify(error="在云手机上打开画面失败：" + (note or info),
                           device_id=dev.id, url=url, applied=False), 502
        record_audit(g.current_user.id, "set_virtual_camera", "device", dev.id,
                     {"url": url, "mode": "player" if player else "url"})
        return jsonify(ok=True, device_id=dev.id, url=url, applied=True,
                       mode="player" if player else "url", target=target,
                       note=(note + "；" if note else "")
                            + "云手机镜像无相机 HAL，已在云手机浏览器中打开画面（系统相机不受影响）")

    rc, out, err = _run_virtual_camera_cmd(_virtual_camera_cmd(tpl, dev, url))
    if rc != 0:
        return jsonify(
            error="虚拟摄像头管线执行失败：" + ((err or out or "").strip()[-300:] or "未知错误"),
            device_id=dev.id, url=url, applied=False, pipeline_configured=True,
        ), 502
    record_audit(g.current_user.id, "set_virtual_camera", "device", dev.id, {"url": url})
    return jsonify(ok=True, device_id=dev.id, url=url, applied=True,
                   output=(out or "").strip()[-500:])


@devices_bp.post("/devices/<int:device_id>/stop-virtual-camera-stream")
@login_required
def stop_virtual_camera_stream(device_id):
    """断开虚拟摄像头（清除登记地址，并在配置了停止命令时执行）。"""
    dev = db.session.get(Device, device_id)
    if dev is None:
        return jsonify(error="设备不存在"), 404
    prev_url = dev.virtual_camera_url or ""
    dev.virtual_camera_url = ""
    dev.virtual_camera_updated_at = datetime.utcnow()
    db.session.commit()

    tpl = (current_app.config.get("VIRTUAL_CAMERA_STOP_CMD") or "").strip()
    if not tpl:
        # 没有系统级管线时，"停止"就是把播放页退到后台（回桌面），不影响设备其它使用
        if dev.serial:
            try:
                orchestrator._run_adb(["-s", dev.serial, "shell",
                                       "input keyevent KEYCODE_HOME"], timeout=15)
            except Exception:  # noqa: BLE001
                pass
        return jsonify(ok=True, device_id=dev.id, applied=False,
                       note="已清除登记地址并把云手机退回桌面")
    rc, out, err = _run_virtual_camera_cmd(_virtual_camera_cmd(tpl, dev, prev_url))
    if rc != 0:
        return jsonify(error="停止虚拟摄像头失败：" + ((err or out or "").strip()[-300:] or "未知错误"),
                       device_id=dev.id, applied=False), 502
    record_audit(g.current_user.id, "stop_virtual_camera", "device", dev.id, {"url": prev_url})
    return jsonify(ok=True, device_id=dev.id, applied=True, output=(out or "").strip()[-500:])
