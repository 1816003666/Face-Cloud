# -*- coding: utf-8 -*-
"""企业管理（平台级接口，仅平台账号可用）。

平台账号 = 没有企业归属（users.enterprise_id 为空）的 admin / superadmin，
用于开通、停用、查看各家企业的规模；企业自己的账号只能看到本企业。
"""
from flask import Blueprint, g, jsonify, request

from ..extensions import db
from ..models import Department, Device, Enterprise, RefundRequest, Subscription, User, record_audit
from ..auth import login_required, require_role
from .. import tenant

enterprises_bp = Blueprint("enterprises", __name__)


def _require_platform():
    cur = g.current_user
    if not tenant.is_platform(cur) or cur.role not in ("admin", "superadmin"):
        return jsonify(error="仅平台账号可管理企业"), 403
    return None


def _admins(ent_id):
    """该企业的管理员账号（登录角色 admin / superadmin）。"""
    rows = (db.session.query(User)
            .filter(User.enterprise_id == ent_id,
                    User.role.in_(("admin", "superadmin")))
            .order_by(User.id).all())
    return [{"id": u.id, "username": u.username, "role": u.role,
             "is_active": bool(u.is_active)} for u in rows]


def _stats_bulk(ent_ids):
    """一次聚合出所有企业的规模统计（原来每家企业 5 条 count，企业多了是 N+1）。"""
    from sqlalchemy import func

    if not ent_ids:
        return {}

    def grouped(q, key_col):
        return {k: n for k, n in q if k is not None}

    users = grouped(
        db.session.query(User.enterprise_id, func.count(User.id))
        .filter(User.enterprise_id.in_(ent_ids))
        .group_by(User.enterprise_id).all(), User.enterprise_id)

    devices = grouped(
        db.session.query(User.enterprise_id, func.count(Device.id))
        .join(User, Device.created_by == User.id)
        .filter(User.enterprise_id.in_(ent_ids))
        .group_by(User.enterprise_id).all(), User.enterprise_id)

    subs = grouped(
        db.session.query(User.enterprise_id, func.count(Subscription.id))
        .join(User, Subscription.user_id == User.id)
        .filter(User.enterprise_id.in_(ent_ids), Subscription.status == "active")
        .group_by(User.enterprise_id).all(), User.enterprise_id)

    depts = grouped(
        db.session.query(Department.enterprise_id, func.count(Department.id))
        .filter(Department.enterprise_id.in_(ent_ids))
        .group_by(Department.enterprise_id).all(), Department.enterprise_id)

    refunds = grouped(
        db.session.query(RefundRequest.enterprise_id, func.count(RefundRequest.id))
        .filter(RefundRequest.enterprise_id.in_(ent_ids), RefundRequest.status == "pending")
        .group_by(RefundRequest.enterprise_id).all(), RefundRequest.enterprise_id)

    admins = {}
    for u in (db.session.query(User)
              .filter(User.enterprise_id.in_(ent_ids),
                      User.role.in_(("admin", "superadmin")))
              .order_by(User.id).all()):
        admins.setdefault(u.enterprise_id, []).append(
            {"id": u.id, "username": u.username, "role": u.role,
             "is_active": bool(u.is_active)})

    return {eid: {
        "users": users.get(eid, 0),
        "devices": devices.get(eid, 0),
        "active_subs": subs.get(eid, 0),
        "departments": depts.get(eid, 0),
        "pending_refunds": refunds.get(eid, 0),
        "admins": admins.get(eid, []),
    } for eid in ent_ids}


@enterprises_bp.get("/enterprises")
@login_required
def list_enterprises():
    cur = g.current_user
    if tenant.is_platform(cur):
        rows = db.session.query(Enterprise).order_by(Enterprise.id).all()
    else:
        row = db.session.get(Enterprise, cur.enterprise_id) if cur.enterprise_id else None
        rows = [row] if row is not None else []
    out = []
    bulk = _stats_bulk([e.id for e in rows])
    for e in rows:
        d = e.to_dict()
        d.update(bulk.get(e.id, {}))
        out.append(d)
    return jsonify(out)


@enterprises_bp.post("/enterprises")
@login_required
def create_enterprise():
    denied = _require_platform()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    code = str(data.get("code") or "").strip().upper()
    if not name or not code:
        return jsonify(error="企业名称和编码必填"), 400
    if db.session.query(Enterprise).filter_by(code=code).first() is not None:
        return jsonify(error="企业编码已存在"), 409
    e = Enterprise(name=name[:64], code=code[:32], status="active",
                   contact=str(data.get("contact") or "").strip()[:64],
                   remark=str(data.get("remark") or "").strip()[:255])
    db.session.add(e)
    db.session.flush()
    # 每家企业一个根部门（组织树的根）
    db.session.add(Department(name=name[:64], parent_id=None, sort=0, enterprise_id=e.id))
    # 新企业自带一套默认角色（权限矩阵该企业可自行调整）
    import json as _json
    from .. import org as _org
    from ..models import OrgRole
    for i, r in enumerate(_org.DEFAULT_ROLES):
        db.session.add(OrgRole(name=r["name"], code=r["code"],
                               description=r["description"],
                               perms=_json.dumps(r["perms"], ensure_ascii=False),
                               sort=(i + 1) * 10, enterprise_id=e.id))
    db.session.commit()
    record_audit(g.current_user.id, "create_enterprise", "enterprise", e.id,
                 {"name": e.name, "code": e.code})
    d = e.to_dict()
    d.update(_stats_bulk([e.id]).get(e.id, {}))
    return jsonify(d), 201


@enterprises_bp.patch("/enterprises/<int:eid>")
@login_required
def update_enterprise(eid):
    denied = _require_platform()
    if denied:
        return denied
    e = db.session.get(Enterprise, eid)
    if e is None:
        return jsonify(error="企业不存在"), 404
    data = request.get_json(silent=True) or {}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            return jsonify(error="企业名称不能为空"), 400
        e.name = name[:64]
    if "contact" in data:
        e.contact = str(data.get("contact") or "").strip()[:64]
    if "remark" in data:
        e.remark = str(data.get("remark") or "").strip()[:255]
    if "status" in data:
        status = str(data.get("status") or "").strip()
        if status not in ("active", "disabled"):
            return jsonify(error="状态只能是 active / disabled"), 400
        e.status = status
    db.session.commit()
    record_audit(g.current_user.id, "update_enterprise", "enterprise", e.id,
                 {"name": e.name, "status": e.status})
    d = e.to_dict()
    d.update(_stats_bulk([e.id]).get(e.id, {}))
    return jsonify(d)


@enterprises_bp.delete("/enterprises/<int:eid>")
@login_required
def delete_enterprise(eid):
    denied = _require_platform()
    if denied:
        return denied
    e = db.session.get(Enterprise, eid)
    if e is None:
        return jsonify(error="企业不存在"), 404
    st = _stats_bulk([e.id]).get(e.id, {})
    if st["users"] or st["devices"] or st["active_subs"]:
        return jsonify(error="该企业下还有账号/设备/订阅，不能删除；可改为「停用」"), 400
    name = e.name
    db.session.query(Department).filter(Department.enterprise_id == eid).delete(
        synchronize_session=False)
    db.session.delete(e)
    db.session.commit()
    record_audit(g.current_user.id, "delete_enterprise", "enterprise", eid, {"name": name})
    return jsonify(message="已删除")
