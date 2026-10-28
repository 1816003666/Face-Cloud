"""VPC 网络模块：客户自助开通虚拟网络、加入云手机、分配内网 IP。

面向客户（客户端）：
- GET  /vpc            查询我的 VPC（未开通返回空）
- POST /vpc/open       一键开通（自动分配网段）
- GET  /vpc/<id>       VPC 详情（含设备列表与内网 IP）
- POST /vpc/<id>/devices   加入云手机（自动分配内网 IP）
- DELETE /vpc/<id>/devices/<device_id>  移出云手机

多租户：VPC 归属创建者（owner_id），普通用户只能操作自己的 VPC。
"""
from flask import request, jsonify, Blueprint, g
vpc_bp = Blueprint("vpc", __name__)

from .. import orchestrator
from ..extensions import db
from ..models import Vpc, VpcDevice, VpcLine, Device, User, record_audit, _utcnow, iso_utc
from ..auth import login_required, require_role, require_perm

DEFAULT_CIDR = "10.0.0.0/24"
# 自动分配内网 IP 的起始与范围（10.0.0.1 为网关，从 .2 开始）
IP_BASE = 2
IP_MAX = 254


def _device_port(d):
    if d.backend != "redroid" or ":" not in (d.serial or ""):
        return None
    try:
        return int(d.serial.split(":")[-1])
    except (TypeError, ValueError):
        return None


def _apply_line(d, line):
    """给一台 redroid 挂出口线路。非真机或无端口时跳过，不报错。"""
    port = _device_port(d)
    if port is None or line is None or line.status != "running":
        return ""
    result = orchestrator.apply_vpc_sidecar(
        port, line.host, line.port, line.username or "", line.password or "",
    )
    if not result.get("ok"):
        return result.get("error") or "sidecar 启动失败"
    probed = orchestrator.probe_device_egress_ip(d.serial)
    d.egress_ip = probed or line.last_egress_ip or ""
    return ""


def _clear_line(d):
    port = _device_port(d)
    if port is not None:
        orchestrator.remove_vpc_sidecar(port)
    d.egress_ip = ""


def _next_free_ip(vpc_id):
    """在 VPC 内取第一个空闲内网 IP：10.0.0.<n>。"""
    used = set()
    rows = db.session.query(VpcDevice).filter_by(vpc_id=vpc_id).all()
    for r in rows:
        try:
            used.add(int(r.ip.rsplit(".", 1)[1]))
        except Exception:
            pass
    for n in range(IP_BASE, IP_MAX + 1):
        if n not in used:
            return f"10.0.0.{n}"
    return None


def _vpc_payload(vpc):
    devices = db.session.query(VpcDevice).filter_by(vpc_id=vpc.id).all()
    dev_rows = []
    for vd in devices:
        d = db.session.get(Device, vd.device_id)
        dev_rows.append({
            "id": vd.id,
            "device_id": vd.device_id,
            "name": d.name if d else f"设备#{vd.device_id}",
            "serial": d.serial if d else "",
            "status": d.status if d else "unknown",
            "backend": d.backend if d else "",
            "ip": vd.ip,
            "egress_ip": (d.egress_ip if d else "") or "",
            "created_at": iso_utc(vd.created_at),
        })
    return {
        "id": vpc.id,
        "name": vpc.name,
        "cidr": vpc.cidr,
        "status": vpc.status,
        "egress_ip": (vpc.vpc_line.last_egress_ip if vpc.vpc_line else "") or vpc.egress_ip,
        "vpc_line_id": vpc.vpc_line_id,
        "vpc_line_name": vpc.vpc_line.name if vpc.vpc_line else "",
        "security_level": vpc.security_level or "private",
        "security_applied": False,
        "device_count": len(dev_rows),
        "devices": dev_rows,
        "created_at": iso_utc(vpc.created_at),
    }


@vpc_bp.get("/vpc")
@login_required
def my_vpc():
    """查询当前用户的 VPC（多租户：只看自己的）。"""
    vpc = db.session.query(Vpc).filter_by(owner_id=g.current_user.id).first()
    if not vpc:
        return jsonify(None)
    return jsonify(_vpc_payload(vpc))


@vpc_bp.post("/vpc/open")
@login_required
def open_vpc():
    """一键开通 VPC：自动命名、分配网段、固定出口 IP 占位。"""
    if db.session.query(Vpc).filter_by(owner_id=g.current_user.id).first():
        return jsonify(error="您已开通网络，请勿重复创建"), 409
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or "我的网络"
    vpc = Vpc(name=name, cidr=DEFAULT_CIDR,
              status="running", egress_ip="",
              owner_id=g.current_user.id)
    db.session.add(vpc)
    db.session.commit()
    record_audit(g.current_user.id, "open_vpc", "vpc", vpc.id, {"cidr": vpc.cidr})
    return jsonify(_vpc_payload(vpc)), 201


@vpc_bp.get("/vpc/<int:vpc_id>")
@login_required
def vpc_detail(vpc_id):
    vpc = db.session.get(Vpc, vpc_id)
    if not vpc or vpc.owner_id != g.current_user.id:
        return jsonify(error="网络不存在"), 404
    return jsonify(_vpc_payload(vpc))


@vpc_bp.post("/vpc/<int:vpc_id>/devices")
@login_required
def vpc_add_devices(vpc_id):
    """加入云手机：自动分配内网 IP（10.0.0.2 起）。"""
    vpc = db.session.get(Vpc, vpc_id)
    if not vpc or vpc.owner_id != g.current_user.id:
        return jsonify(error="网络不存在"), 404
    data = request.get_json(silent=True) or {}
    device_ids = data.get("device_ids") or []
    if not isinstance(device_ids, list) or not device_ids:
        return jsonify(error="请选择要加入的云手机"), 400
    # 去重 + 排除已加入的
    line = db.session.get(VpcLine, vpc.vpc_line_id) if vpc.vpc_line_id else None
    added, skipped, no_ip, sidecar_errors = [], [], [], []
    for did in device_ids:
        if db.session.query(VpcDevice).filter_by(vpc_id=vpc_id, device_id=did).first():
            skipped.append(did)
            continue
        d = db.session.get(Device, did)
        if not d or d.created_by != g.current_user.id:
            skipped.append(did)
            continue
        ip = _next_free_ip(vpc_id)
        if not ip:
            no_ip.append(did)
            continue
        vd = VpcDevice(vpc_id=vpc_id, device_id=did, ip=ip)
        db.session.add(vd)
        err = _apply_line(d, line)
        if err:
            sidecar_errors.append({"device_id": did, "error": err})
        added.append({"device_id": did, "ip": ip, "egress_ip": d.egress_ip or ""})
    db.session.commit()
    if added:
        record_audit(g.current_user.id, "vpc_add_device", "vpc", vpc_id,
                     {"added": [a["device_id"] for a in added]})
    return jsonify(added=added, skipped=skipped, no_ip=no_ip, sidecar_errors=sidecar_errors)


@vpc_bp.delete("/vpc/<int:vpc_id>/devices/<int:device_id>")
@login_required
def vpc_remove_device(vpc_id, device_id):
    vpc = db.session.get(Vpc, vpc_id)
    if not vpc or vpc.owner_id != g.current_user.id:
        return jsonify(error="网络不存在"), 404
    vd = db.session.query(VpcDevice).filter_by(vpc_id=vpc_id, device_id=device_id).first()
    if not vd:
        return jsonify(error="该云手机不在网络中"), 404
    d = db.session.get(Device, device_id)
    if d:
        _clear_line(d)
    db.session.delete(vd)
    db.session.commit()
    record_audit(g.current_user.id, "vpc_remove_device", "vpc", vpc_id,
                 {"device_id": device_id})
    return jsonify(ok=True)


@vpc_bp.patch("/vpc/<int:vpc_id>")
@login_required
def vpc_update(vpc_id):
    """更新网络：安全级别（open/private/secure）、名称、出口 IP。"""
    vpc = db.session.get(Vpc, vpc_id)
    if not vpc or vpc.owner_id != g.current_user.id:
        return jsonify(error="网络不存在"), 404
    data = request.get_json(silent=True) or {}
    if "security_level" in data:
        lv = str(data["security_level"]).strip()
        if lv not in ("open", "private", "secure"):
            return jsonify(error="安全级别无效"), 400
        vpc.security_level = lv
    if "name" in data:
        name = (data.get("name") or "").strip()
        if name:
            vpc.name = name
    db.session.commit()
    record_audit(g.current_user.id, "update_vpc", "vpc", vpc.id, data)
    return jsonify(_vpc_payload(vpc))


def _set_vpc_line(vpc, raw):
    """管理员指定或解除出口线路。返回 (error, status) 或 (None, 200)。"""
    if raw in (None, "", 0, "0"):
        vpc.vpc_line_id = None
        return None, 200
    try:
        lid = int(raw)
    except (TypeError, ValueError):
        return "vpc_line_id 无效", 400
    line = db.session.get(VpcLine, lid)
    if line is None:
        return "出口线路不存在", 404
    if line.status != "running":
        return "请先测试并运行该线路", 400
    vpc.vpc_line_id = lid
    if line.last_egress_ip:
        vpc.egress_ip = line.last_egress_ip
    return None, 200


@vpc_bp.get("/admin/vpcs")
@require_perm("vpc.manage")
def admin_list_vpcs():
    """管理员查看全部客户网络，用于指定出口线路。"""
    rows = []
    for vpc in db.session.query(Vpc).order_by(Vpc.id).all():
        owner = db.session.get(User, vpc.owner_id) if vpc.owner_id else None
        item = _vpc_payload(vpc)
        item["owner_name"] = owner.username if owner else ""
        rows.append(item)
    return jsonify(rows)


@vpc_bp.patch("/admin/vpcs/<int:vpc_id>/line")
@require_perm("vpc.manage")
def admin_set_vpc_line(vpc_id):
    """管理员给客户网络指定或解除出口线路。客户不能调用。"""
    vpc = db.session.get(Vpc, vpc_id)
    if vpc is None:
        return jsonify(error="网络不存在"), 404
    data = request.get_json(silent=True) or {}
    err, status = _set_vpc_line(vpc, data.get("vpc_line_id"))
    if err:
        return jsonify(error=err), status
    db.session.commit()
    record_audit(g.current_user.id, "admin_set_vpc_line", "vpc", vpc.id,
                 {"vpc_line_id": vpc.vpc_line_id})
    return jsonify(_vpc_payload(vpc))
