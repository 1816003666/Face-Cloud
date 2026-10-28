"""分组管理模块：设备分组 + 分组权限 + 分组批量操作。

权限：查看登录即可；创建/编辑/删除需 admin 及以上（或分组 owner）；
      分组批量操作（开机/关机/销毁）需 operator 及以上。
分组权限模型：owner 或 admin/superadmin 可管理该分组。
"""
from flask import request, jsonify, Blueprint, g
groups_bp = Blueprint("groups", __name__)

from ..extensions import db
from ..models import Group, Device, DeviceLog, record_audit, ROLE_LEVELS, iso_utc
from ..auth import login_required, require_role, require_perm

GROUP_BATCH_ACTIONS = ["start", "stop", "destroy", "delete"]


@groups_bp.get("/groups")
@login_required
def list_groups():
    rows = db.session.query(Group).order_by(Group.id).all()
    out = []
    from .. import scoping
    for g_ in rows:
        count = scoping.scope_query(
            db.session.query(Device).filter_by(group_id=g_.id),
            g.current_user).count()
        out.append({
            "id": g_.id,
            "name": g_.name,
            "description": g_.description,
            "owner_id": g_.owner_id,
            "device_count": count,
            "created_at": iso_utc(g_.created_at),
        })
    return jsonify(out)


@groups_bp.post("/groups")
@require_perm("device.group")
def create_group():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(error="分组名必填"), 400
    if db.session.query(Group).filter_by(name=name).first():
        return jsonify(error="分组名已存在"), 409
    g_ = Group(name=name, description=data.get("description", ""),
               owner_id=g.current_user.id)
    db.session.add(g_)
    db.session.commit()
    record_audit(g.current_user.id, "create_group", "group", g_.id,
                {"name": name})
    return jsonify(id=g_.id, name=g_.name), 201


@groups_bp.patch("/groups/<int:gid>")
@require_perm("device.group")
def update_group(gid):
    g_ = db.session.get(Group, gid)
    if g_ is None:
        return jsonify(error="分组不存在"), 404
    data = request.get_json(silent=True) or {}
    if "name" in data:
        g_.name = data["name"]
    if "description" in data:
        g_.description = data["description"]
    db.session.commit()
    record_audit(g.current_user.id, "update_group", "group", gid,
                {"name": g_.name})
    return jsonify(ok=True)


@groups_bp.delete("/groups/<int:gid>")
@require_perm("device.group")
def delete_group(gid):
    g_ = db.session.get(Group, gid)
    if g_ is None:
        return jsonify(error="分组不存在"), 404
    db.session.delete(g_)
    db.session.commit()
    record_audit(g.current_user.id, "delete_group", "group", gid,
                {"name": g_.name})
    return jsonify(ok=True)


@groups_bp.post("/groups/<int:gid>/batch-action")
@login_required
def group_batch_action(gid):
    """对分组内设备执行批量动作：start / stop / delete / destroy。

    - start / stop：需要 device.power（基础功能）
    - delete：需要 device.delete（基础功能）；用户侧只归还公共池，不销毁容器
    - destroy：只有平台管理员可用（真正的销毁运维动作）
    """
    g_ = db.session.get(Group, gid)
    if g_ is None:
        return jsonify(error="分组不存在"), 404
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in GROUP_BATCH_ACTIONS:
        return jsonify(error=f"action 必须是 {GROUP_BATCH_ACTIONS}"), 400

    from ..auth import has_perm
    cur = g.current_user
    need = "device.power" if action in ("start", "stop") else "device.delete"
    if action == "destroy":
        if not (cur.role in ("admin", "superadmin") and cur.enterprise_id is None):
            return jsonify(error="只有平台管理员可以销毁云手机"), 403
    elif not has_perm(cur, need):
        return jsonify(error=f"权限不足（缺少 {need}）"), 403

    device_ids = data.get("device_ids")  # 不传则对分组内全部设备
    from .. import scoping
    if device_ids:
        _bad = scoping.foreign_device_ids(g.current_user, device_ids)
        if _bad:
            return jsonify(error="无权操作部分设备", invalid_ids=_bad), 403
    query = scoping.scope_query(
        db.session.query(Device).filter_by(group_id=gid), g.current_user)
    if device_ids:
        query = query.filter(Device.id.in_([int(i) for i in device_ids]))
    devices = query.all()

    ok = 0
    for dev in devices:
        if action == "start":
            dev.status = "running"
            msg = "批量开机"
        elif action == "stop":
            dev.status = "stopped"
            msg = "批量关机"
        elif action == "delete":
            # 删除云手机：用户侧只清数据 + 归还公共池，不销毁服务器容器
            is_platform = (g.current_user.role in ("admin", "superadmin")
                           and g.current_user.enterprise_id is None)
            if is_platform:
                try:
                    from .. import orchestrator
                    orchestrator.delete_device(dev.backend, dev.serial)
                except Exception:  # noqa: BLE001
                    pass
                db.session.delete(dev)
                ok += 1
                continue
            if dev.created_by != g.current_user.id:
                continue
            from ..billing import wipe_device_data
            wipe_device_data(dev)
            dev.created_by = None
            dev.sub_id = None
            dev.enterprise_id = None
            msg = "批量删除（归还公共池）"
        else:
            dev.status = "error"
            msg = "批量销毁"
        db.session.add(DeviceLog(device_id=dev.id, level="info",
                                 message=f"分组[{g_.name}] {msg}（模拟）"))
        ok += 1
    db.session.commit()
    record_audit(g.current_user.id, "group_batch_action", "group", gid,
                {"action": action, "count": ok})
    return jsonify(ok=ok, failed=len(devices) - ok)
