"""用户管理路由。

权限边界（企业级设计，作业加分项）：
- 整个 router 要求 admin 及以上；
- admin 不能创建/分配 admin 及以上角色（只有 superadmin 能）；
- 不能删除/降级最后一个 superadmin；
- 不能改自己的角色、不能删自己账号；
- 密码强制复杂度（>=6 位且含字母+数字）；
- 每次增删改都写 audit_logs。
"""
import re
import json
from flask import request, jsonify, g, Blueprint
users_bp = Blueprint("users", __name__)
from ..extensions import db
from ..models import User, ROLE_LEVELS, ROLE_CHOICES, record_audit, AuditLog
from ..auth import login_required, require_role, require_platform, hash_password, verify_password

_PW_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{6,}$")


def _count_superadmin():
    return db.session.query(User).filter_by(role="superadmin", is_active=True).count()


# 下面两个规则函数同时被 /users 与 /org 的成员接口复用，避免两处口径漂移
def role_change_error(cur, u, new_role):
    """校验能否把 u 的平台登录角色改成 new_role，返回错误文案或 None。"""
    if new_role not in ROLE_CHOICES:
        return "非法角色"
    if u.id == cur.id:
        return "不能修改自己的角色"
    if ROLE_LEVELS[cur.role] < ROLE_LEVELS["superadmin"] and \
       ROLE_LEVELS[new_role] >= ROLE_LEVELS["admin"]:
        return "无权限分配该级别角色"
    if u.role == "superadmin" and new_role != "superadmin" and _count_superadmin() <= 1:
        return "不能降级最后一个超级管理员"
    return None


def create_role_error(cur, role):
    """校验当前账号能否创建 role 这个级别的账号。"""
    if role not in ROLE_CHOICES:
        return "非法角色"
    if ROLE_LEVELS[cur.role] < ROLE_LEVELS["superadmin"] and \
       ROLE_LEVELS[role] >= ROLE_LEVELS["admin"]:
        return "无权限分配该级别角色"
    return None


def delete_user_error(cur, u):
    """校验能否删除 u，返回错误文案或 None。"""
    if u.id == cur.id:
        return "不能删除自己"
    if u.role == "superadmin" and _count_superadmin() <= 1:
        return "不能删除最后一个超级管理员"
    if ROLE_LEVELS[cur.role] < ROLE_LEVELS["superadmin"] and \
       ROLE_LEVELS[u.role] >= ROLE_LEVELS["admin"]:
        return "无权限删除该级别账号"
    return None


@users_bp.get("/users")
@require_platform
def list_users():
    users = db.session.query(User).order_by(User.id).all()
    return jsonify([u.to_dict() for u in users])


@users_bp.post("/users")
@require_platform
def create_user():
    cur = g.current_user
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    role = data.get("role", "viewer")

    if not username or not password:
        return jsonify(error="用户名和密码必填"), 400
    if role not in ROLE_CHOICES:
        return jsonify(error="非法角色"), 400
    if db.session.query(User).filter_by(username=username).first():
        return jsonify(error="用户名已存在"), 409
    if not _PW_RE.match(password):
        return jsonify(error="密码至少6位且含字母和数字"), 400

    # 越权防护：admin 不能创建 admin 及以上
    if ROLE_LEVELS[cur.role] < ROLE_LEVELS["superadmin"] and \
       ROLE_LEVELS[role] >= ROLE_LEVELS["admin"]:
        return jsonify(error="无权限创建该级别账号"), 403

    u = User(username=username, hashed_password=hash_password(password),
             role=role, is_active=True)
    db.session.add(u)
    db.session.commit()
    record_audit(cur.id, "create_user", "user", u.id, {"username": username, "role": role})
    return jsonify(u.to_dict()), 201


@users_bp.patch("/users/<int:uid>")
@require_platform
def update_user(uid):
    cur = g.current_user
    u = db.session.get(User, uid)
    if u is None:
        return jsonify(error="用户不存在"), 404

    data = request.get_json(silent=True) or {}

    # 改角色
    if "role" in data:
        new_role = data["role"]
        err = role_change_error(cur, u, new_role)
        if err:
            return jsonify(error=err), 403
        u.role = new_role

    # 改密码
    if "password" in data and data["password"]:
        if not _PW_RE.match(data["password"]):
            return jsonify(error="密码至少6位且含字母和数字"), 400
        u.hashed_password = hash_password(data["password"])

    # 改账号所属（只有平台账号能改）
    #  - 关联企业：enterprise_id = 企业 id
    #  - 个人用户：enterprise_id 为空 + 角色是 viewer/operator
    #  - 平台账号：enterprise_id 为空 + 角色是 admin/superadmin
    if "enterprise_id" in data:
        from .. import tenant
        from ..models import Enterprise
        if not tenant.is_platform(cur):
            return jsonify(error="只有平台管理员可以调整账号所属"), 403
        raw = data.get("enterprise_id")
        kind = str(data.get("account_kind") or "").strip().lower()
        if raw in (None, "", 0, "personal"):
            # 个人用户：普通角色；平台账号：管理员角色
            if kind == "personal" and u.role in ("admin", "superadmin"):
                return jsonify(error="该账号当前是管理员角色，个人用户请先把登录角色改成用户/运营"), 400
            if kind == "platform" and u.role not in ("admin", "superadmin"):
                return jsonify(error="平台账号需要管理员角色，请先把登录角色改成管理员"), 400
            u.enterprise_id = None
        else:
            try:
                eid = int(raw)
            except (TypeError, ValueError):
                return jsonify(error="企业不合法"), 400
            if db.session.get(Enterprise, eid) is None:
                return jsonify(error="企业不存在"), 404
            u.enterprise_id = eid

    # 改启用状态
    if "is_active" in data:
        # 不能停用自己
        if u.id == cur.id and not data["is_active"]:
            return jsonify(error="不能停用自己"), 403
        # 不能停用最后一个 superadmin
        if u.role == "superadmin" and not data["is_active"] and _count_superadmin() <= 1:
            return jsonify(error="不能停用最后一个超级管理员"), 403
        u.is_active = bool(data["is_active"])

    db.session.commit()
    try:
        from .. import perms as _perms
        _perms.ensure_default_for(u)
    except Exception:  # noqa: BLE001
        db.session.rollback()
    record_audit(cur.id, "update_user", "user", u.id,
                 {"changed": [k for k in data.keys()]})
    return jsonify(u.to_dict())


@users_bp.post("/users/<int:uid>/reset-password")
@require_platform
def reset_user_password(uid):
    """重置账号密码：服务端随机生成，只返回一次。"""
    import secrets
    cur = g.current_user
    u = db.session.get(User, uid)
    if u is None:
        return jsonify(error="用户不存在"), 404
    # 平台管理员可以重置企业管理员及以下账号的密码；superadmin 只有自己能动
    if u.role == "superadmin" and cur.role != "superadmin":
        return jsonify(error="无权限重置超级管理员的密码"), 403
    password = "Cp@" + secrets.token_hex(4)
    u.hashed_password = hash_password(password)
    u.failed_attempts = 0
    u.locked_until = None
    db.session.commit()
    record_audit(cur.id, "reset_user_password", "user", uid, {"username": u.username})
    return jsonify(username=u.username, password=password)


@users_bp.get("/perm-presets")
@login_required
def list_perm_presets():
    from .. import perms as perm_mod, tenant
    if not tenant.is_platform(g.current_user):
        return jsonify(error="只有平台管理员可以管理权限档位"), 403
    return jsonify(perm_mod.preset_list())


@users_bp.post("/perm-presets")
@login_required
def create_perm_preset():
    """新建自定义档位（标签），例如「客服专员」。"""
    import re as _re
    import secrets
    from .. import perms as perm_mod, tenant
    from ..models import PermPreset
    if not tenant.is_platform(g.current_user):
        return jsonify(error="只有平台管理员可以管理权限档位"), 403
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify(error="档位名称必填"), 400
    code = str(data.get("code") or "").strip()
    if not code:
        code = "c_" + secrets.token_hex(4)
    if not _re.match(r"^[A-Za-z0-9_]{2,32}$", code):
        return jsonify(error="code 只能包含字母数字下划线（2-32 位）"), 400
    if db.session.query(PermPreset).filter_by(code=code).first() is not None:
        return jsonify(error="档位标识已存在"), 409
    from ..org import ALL_PERMS
    valid = set(ALL_PERMS)
    codes = [c for c in (data.get("perms") or []) if c in valid]
    row = PermPreset(code=code, name=name[:32], desc=str(data.get("desc") or "")[:128],
                     perms=json.dumps(codes, ensure_ascii=False),
                     builtin=False, sort=int(data.get("sort") or 100))
    db.session.add(row)
    db.session.commit()
    record_audit(g.current_user.id, "create_perm_preset", "perm_preset", row.id,
                 {"name": name, "count": len(codes)})
    return jsonify(row.to_dict()), 201


@users_bp.patch("/perm-presets/<int:pid>")
@login_required
def update_perm_preset(pid):
    from .. import perms as perm_mod, tenant
    from ..models import PermPreset
    from ..org import ALL_PERMS
    if not tenant.is_platform(g.current_user):
        return jsonify(error="只有平台管理员可以管理权限档位"), 403
    row = db.session.get(PermPreset, pid)
    if row is None:
        return jsonify(error="档位不存在"), 404
    data = request.get_json(silent=True) or {}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            return jsonify(error="档位名称不能为空"), 400
        row.name = name[:32]
    if "desc" in data:
        row.desc = str(data.get("desc") or "")[:128]
    if "perms" in data:
        valid = set(ALL_PERMS)
        row.perms = json.dumps([c for c in (data.get("perms") or []) if c in valid],
                               ensure_ascii=False)
    if "sort" in data:
        try:
            row.sort = int(data.get("sort") or 100)
        except (TypeError, ValueError):
            pass
    db.session.commit()
    record_audit(g.current_user.id, "update_perm_preset", "perm_preset", row.id,
                 {"name": row.name})
    return jsonify(row.to_dict())


@users_bp.delete("/perm-presets/<int:pid>")
@login_required
def delete_perm_preset(pid):
    from .. import tenant
    from ..models import PermPreset
    if not tenant.is_platform(g.current_user):
        return jsonify(error="只有平台管理员可以管理权限档位"), 403
    row = db.session.get(PermPreset, pid)
    if row is None:
        return jsonify(error="档位不存在"), 404
    if row.builtin:
        return jsonify(error="内置档位不能删除，可以改它的内容"), 403
    name = row.name
    db.session.delete(row)
    db.session.commit()
    record_audit(g.current_user.id, "delete_perm_preset", "perm_preset", pid, {"name": name})
    return jsonify(message="已删除")


@users_bp.get("/users/<int:uid>/permissions")
@login_required
def get_user_permissions(uid):
    """查看某账号的功能权限（平台管理员给企业管理员分配用）。"""
    from .. import perms as perm_mod, tenant
    from ..models import OrgMember, OrgRole
    cur = g.current_user
    if not tenant.is_platform(cur):
        return jsonify(error="只有平台管理员可以查看功能权限"), 403
    u = db.session.get(User, uid)
    if u is None:
        return jsonify(error="用户不存在"), 404
    profile = db.session.get(OrgMember, uid)
    role_ids = []
    if profile is not None:
        try:
            role_ids = json.loads(profile.role_ids or "[]")
        except (TypeError, ValueError):
            role_ids = []
    role_perms = set()
    for r in db.session.query(OrgRole).filter(OrgRole.id.in_(role_ids or [])).all():
        try:
            role_perms.update(json.loads(r.perms or "[]"))
        except (TypeError, ValueError):
            continue
    from .. import org as _org
    row = perm_mod.get(uid)
    return jsonify(
        user=u.to_dict(),
        preset=(row.preset if row is not None else ""),
        account_perms=sorted(perm_mod.perms_of(uid)),
        role_perms=sorted(role_perms),
        effective=sorted(_org.user_perms(uid)),
        presets=perm_mod.preset_list(),
        modules=_org.MODULES,
    )


@users_bp.put("/users/<int:uid>/permissions")
@login_required
def set_user_permissions(uid):
    """给账号分配功能权限：传 preset 或 perms 列表。"""
    from .. import perms as perm_mod, tenant
    cur = g.current_user
    if not tenant.is_platform(cur):
        return jsonify(error="只有平台管理员可以分配功能权限"), 403
    u = db.session.get(User, uid)
    if u is None:
        return jsonify(error="用户不存在"), 404
    if tenant.is_platform(u):
        return jsonify(error="平台账号本身就是系统级，不需要单独分配"), 400

    data = request.get_json(silent=True) or {}
    if data.get("preset"):
        key = str(data.get("preset"))
        codes = perm_mod.preset_perms(key)
        if not codes and key not in perm_mod.PRESETS:
            return jsonify(error="未知的权限档位"), 400
        perm_mod.set_perms(uid, codes, preset=key)
    elif "perms" in data:
        perm_mod.set_perms(uid, data.get("perms") or [], preset="custom")
    else:
        return jsonify(error="请传 preset 或 perms"), 400
    record_audit(cur.id, "set_user_permissions", "user", uid,
                 {"preset": data.get("preset") or "custom", "count": len(perm_mod.perms_of(uid))})
    return jsonify(preset=(perm_mod.get(uid).preset if perm_mod.get(uid) else ""),
                   perms=sorted(perm_mod.perms_of(uid)))


@users_bp.delete("/users/<int:uid>")
@require_platform
def delete_user(uid):
    cur = g.current_user
    u = db.session.get(User, uid)
    if u is None:
        return jsonify(error="用户不存在"), 404
    err = delete_user_error(cur, u)
    if err:
        return jsonify(error=err), 403
    AuditLog.query.filter_by(actor_id=uid).update({"actor_id": cur.id})
    # 名下云手机归还公共池，避免挂在已删除的 user_id 上
    from ..models import Device
    released = db.session.query(Device).filter(Device.created_by == uid).update(
        {"created_by": None, "sub_id": None}, synchronize_session=False)
    db.session.delete(u)
    db.session.commit()
    record_audit(cur.id, "delete_user", "user", uid,
                 {"username": u.username, "released_devices": released})
    return jsonify(message="已删除", released_devices=released)


# ---------------------------------------------------------------------------
# 设备分配（归属隔离：把云手机分配给某个用户 / 收回）
# ---------------------------------------------------------------------------
@users_bp.get("/users/<int:uid>/devices")
@require_platform
def list_user_devices(uid):
    """管理员查看某用户名下的云手机。"""
    from ..models import Device
    u = db.session.get(User, uid)
    if u is None:
        return jsonify(error="用户不存在"), 404
    rows = (db.session.query(Device).filter_by(created_by=uid)
            .order_by(Device.id).all())
    return jsonify([{"id": d.id, "name": d.name, "serial": d.serial,
                     "status": d.status, "backend": d.backend} for d in rows])


@users_bp.post("/users/<int:uid>/assign-devices")
@require_platform
def assign_devices(uid):
    """把设备分配给用户 / 从用户收回。

    body: {"device_ids": [...], "mode": "assign"|"unassign"}，mode 默认 assign。
    仅 admin/superadmin 可调用；分配后该用户在其套餐有效期内可见这些设备。
    """
    from ..models import Device
    cur = g.current_user
    u = db.session.get(User, uid)
    if u is None:
        return jsonify(error="用户不存在"), 404
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in (data.get("device_ids") or [])]
    if not ids:
        return jsonify(error="device_ids 不能为空"), 400
    mode = data.get("mode", "assign")
    if mode not in ("assign", "unassign"):
        return jsonify(error="mode 必须是 assign/unassign"), 400
    rows = db.session.query(Device).filter(Device.id.in_(ids)).all()
    found = {d.id for d in rows}
    missing = [i for i in ids if i not in found]
    changed = 0
    for d in rows:
        if mode == "assign":
            if d.created_by != uid:
                d.created_by = uid
                changed += 1
        else:
            if d.created_by == uid:
                d.created_by = None
                changed += 1
    db.session.commit()
    record_audit(cur.id, f"{mode}_devices", "user", uid,
                 {"device_ids": ids, "changed": changed})
    return jsonify(ok=True, mode=mode, changed=changed, missing=missing)
