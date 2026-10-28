# -*- coding: utf-8 -*-
"""部门与角色管理 API（对应前端 /org 页面）。

- 部门：树形增删改，删除时下级成员转入上级部门；
- 成员：平台用户 + 组织档案（部门 / 角色 / 数据范围 / 启停）；
- 角色：功能权限矩阵（perms），成员多选、权限取并集；
- 资源分派：把云手机（Device.created_by）分给成员或收回。
"""
import json
import secrets

from flask import Blueprint, g, jsonify, request

from ..extensions import db
from ..models import Device, Department, Enterprise, OrgMember, OrgRole, User, record_audit
from ..auth import hash_password, login_required, require_role, validate_password, require_perm
from .. import org
from .. import tenant
from .users import create_role_error, delete_user_error, role_change_error

org_bp = Blueprint("org", __name__)


def _member_or_404(uid):
    u = db.session.get(User, uid)
    if u is None:
        return None
    return u


def _profile(uid) -> OrgMember:
    p = db.session.get(OrgMember, uid)
    if p is None:
        p = OrgMember(user_id=uid, display_name="", role_ids="[]", scope="self")
        db.session.add(p)
        db.session.commit()
    return p


def _gen_password() -> str:
    return "Cp@" + secrets.token_hex(4)


# ---------------------------------------------------------------------------
# 概览：部门树 + 角色 + 权限定义
# ---------------------------------------------------------------------------
@org_bp.get("/org/overview")
@require_perm("org.view")
def org_overview():
    org.ensure_seed()
    depts = tenant.scope(
        db.session.query(Department), Department
    ).order_by(Department.sort, Department.id).all()
    profiles = {p.user_id: p for p in db.session.query(OrgMember).all()}

    member_count = {}
    for p in profiles.values():
        if p.department_id:
            member_count[p.department_id] = member_count.get(p.department_id, 0) + 1
    for u in tenant.scope(db.session.query(User), User).all():
        if u.id not in profiles:
            member_count[0] = member_count.get(0, 0) + 1

    roles = tenant.scope(
        db.session.query(OrgRole), OrgRole
    ).order_by(OrgRole.sort, OrgRole.id).all()
    role_count = {}
    for p in profiles.values():
        try:
            ids = json.loads(p.role_ids or "[]")
        except (TypeError, ValueError):
            ids = []
        for rid in ids:
            role_count[rid] = role_count.get(rid, 0) + 1

    total_users = tenant.scope(db.session.query(User), User).count()
    allowed = set(org.all_perms_for(g.current_user))

    def _role_out(r):
        """角色序列化：企业账号只返回它「拿得到」的权限。

        否则历史角色里残留的平台专属权限会让页面显示「已选 43 / 30 项」这种
        自相矛盾的计数（43 里有 13 项根本渲染不出来）。
        """
        d = dict(r.to_dict(), member_count=role_count.get(r.id, 0))
        perms = [c for c in (d.get("perms") or []) if c in allowed]
        d["perms"] = perms
        d["perm_count"] = len(perms)
        return d

    return jsonify(
        modules=org.modules_for(g.current_user),
        scopes=org.SCOPES,
        all_perms=org.all_perms_for(g.current_user),
        departments=[dict(d.to_dict(), member_count=member_count.get(d.id, 0)) for d in depts],
        roles=[_role_out(r) for r in roles],
        total_members=total_users,
    )


# ---------------------------------------------------------------------------
# 部门
# ---------------------------------------------------------------------------
@org_bp.post("/org/departments")
@require_perm("org.create")
def create_department():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify(error="部门名称必填"), 400
    parent_id = data.get("parent_id")
    if parent_id in ("", None):
        parent_id = None
    else:
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            parent_id = None
        if parent_id is not None and db.session.get(Department, parent_id) is None:
            return jsonify(error="上级部门不存在"), 400
    if parent_id is None:
        return jsonify(error="只能有一个根部门"), 400
    d = tenant.stamp(Department(name=name[:64], parent_id=parent_id,
                                sort=int(data.get("sort") or 0)))
    db.session.add(d)
    db.session.commit()
    record_audit(g.current_user.id, "create_department", "department", d.id, {"name": name})
    return jsonify(d.to_dict()), 201


@org_bp.patch("/org/departments/<int:did>")
@require_perm("org.create")
def update_department(did):
    d = db.session.get(Department, did)
    if d is None:
        return jsonify(error="部门不存在"), 404
    data = request.get_json(silent=True) or {}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            return jsonify(error="部门名称不能为空"), 400
        d.name = name[:64]
    if "parent_id" in data and d.parent_id is not None:
        pid = data.get("parent_id")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return jsonify(error="上级部门不合法"), 400
        if pid in org.subtree_ids(did):
            return jsonify(error="不能把部门移动到它自己或下级部门下面"), 400
        if db.session.get(Department, pid) is None:
            return jsonify(error="上级部门不存在"), 400
        d.parent_id = pid
    db.session.commit()
    record_audit(g.current_user.id, "update_department", "department", d.id, {"name": d.name})
    return jsonify(d.to_dict())


@org_bp.delete("/org/departments/<int:did>")
@require_perm("org.create")
def delete_department(did):
    d = db.session.get(Department, did)
    if d is None:
        return jsonify(error="部门不存在"), 404
    if d.parent_id is None:
        return jsonify(error="根部门不能删除"), 400
    kids = db.session.query(Department.id).filter(Department.parent_id == did).count()
    if kids:
        return jsonify(error="该部门下还有下级部门，请先删除或移走"), 400
    moved = 0
    for p in db.session.query(OrgMember).filter(OrgMember.department_id == did).all():
        p.department_id = d.parent_id
        moved += 1
    db.session.delete(d)
    db.session.commit()
    record_audit(g.current_user.id, "delete_department", "department", did,
                 {"name": d.name, "moved_members": moved})
    return jsonify(message="已删除", moved_members=moved)


# ---------------------------------------------------------------------------
# 成员
# ---------------------------------------------------------------------------
@org_bp.get("/org/members")
@require_perm("org.member")
def list_members():
    org.ensure_seed()
    q = (request.args.get("q") or "").strip().lower()
    dept_arg = (request.args.get("department_id") or "").strip()

    ent_id = tenant.current_enterprise_id()
    profiles = {p.user_id: p for p in db.session.query(OrgMember).all()}
    counts = dict(db.session.query(Device.created_by, db.func.count(Device.id))
                  .group_by(Device.created_by).all())

    # 只允许看到「企业成员」：
    # - 企业账号：严格等于本企业；
    # - 平台账号：只看有企业归属的账号，绝不显示个人用户（enterprise_id 为空）与平台账号。
    uq = db.session.query(User)
    if ent_id is None:
        uq = uq.filter(User.enterprise_id.isnot(None))
        want_ent = (request.args.get("enterprise_id") or "").strip()
        if want_ent:
            try:
                uq = uq.filter(User.enterprise_id == int(want_ent))
            except (TypeError, ValueError):
                pass
    else:
        uq = uq.filter(User.enterprise_id == ent_id)
    # 角色 id 只保留本企业仍存在的角色，避免出现 "#8" 这种已删角色的编号
    from ..models import OrgRole as _OrgRole
    rq = db.session.query(_OrgRole)
    if ent_id is not None:
        rq = rq.filter(_OrgRole.enterprise_id == ent_id)
    valid_roles = {r.id for r in rq.all()}

    out = []
    for u in uq.order_by(User.id).all():
        m = org.member_dict(u, profiles.get(u.id), counts.get(u.id, 0),
                            valid_role_ids=valid_roles)
        if dept_arg:
            try:
                want = int(dept_arg)
            except (TypeError, ValueError):
                want = None
            if want is not None and m["department_id"] != want:
                continue
        if q and q not in m["name"].lower() and q not in m["account"].lower():
            continue
        out.append(m)
    return jsonify(out)


@org_bp.post("/org/members")
@require_perm("org.member")
def create_member():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    account = str(data.get("account") or "").strip()
    if not name or not account:
        return jsonify(error="姓名和登录账号必填"), 400
    if len(account) < 2:
        return jsonify(error="登录账号至少 2 个字符"), 400
    if db.session.query(User).filter_by(username=account).first() is not None:
        return jsonify(error="登录账号已存在"), 409
    password = str(data.get("password") or "").strip() or _gen_password()
    ok, msg = validate_password(password)
    if not ok:
        return jsonify(error=msg), 400

    dept_id = org.normalize_dept(data.get("department_id"))
    role_ids = org.normalize_roles(data.get("role_ids"))
    if not role_ids:
        return jsonify(error="至少选择一个角色"), 400

    platform_role = str(data.get("platform_role") or "viewer").strip()
    err = create_role_error(g.current_user, platform_role)
    if err:
        return jsonify(error=err), 403

    # 企业归属：企业管理员只能在自己企业下建人；平台账号可指定企业，
    # 没指定就跟随所选部门（测试报告 ENT-001：从"部门与角色"建的人不能变成平台级账号）
    if tenant.is_platform(g.current_user):
        ent_id = data.get("enterprise_id")
        try:
            ent_id = int(ent_id) if ent_id not in (None, "", 0) else None
        except (TypeError, ValueError):
            return jsonify(error="企业不合法"), 400
        if ent_id is not None and db.session.get(Enterprise, ent_id) is None:
            return jsonify(error="企业不存在"), 400
        if ent_id is None and dept_id is not None:
            dept = db.session.get(Department, dept_id)
            if dept is not None:
                ent_id = dept.enterprise_id
    else:
        ent_id = g.current_user.enterprise_id

    # 普通成员（viewer/operator）必须有企业归属，不能落成平台级账号
    if ent_id is None and platform_role not in ("admin", "superadmin"):
        return jsonify(error="普通成员必须归属某个企业：请选择该企业的部门，或显式指定企业"), 400

    # 部门必须属于同一个企业，避免跨企业串部门
    if dept_id is not None and ent_id is not None:
        dept = db.session.get(Department, dept_id)
        if dept is not None and dept.enterprise_id is not None and dept.enterprise_id != ent_id:
            return jsonify(error="所选部门不属于该企业"), 400

    u = tenant.stamp(User(username=account, hashed_password=hash_password(password),
                          role=platform_role, is_active=bool(data.get("active", True)),
                          enterprise_id=ent_id))
    db.session.add(u)
    db.session.flush()
    p = OrgMember(user_id=u.id, display_name=name[:64], department_id=dept_id,
                  role_ids=json.dumps(role_ids), scope=org.normalize_scope(data.get("scope")))
    db.session.add(p)
    db.session.commit()
    record_audit(g.current_user.id, "create_member", "user", u.id,
                 {"account": account, "name": name})
    payload = org.member_dict(u, p, 0)
    payload["password"] = password     # 只在创建时返回一次，便于交付给成员
    return jsonify(payload), 201


@org_bp.patch("/org/members/<int:uid>")
@require_perm("org.member")
def update_member(uid):
    u = _member_or_404(uid)
    if u is None:
        return jsonify(error="成员不存在"), 404
    p = _profile(uid)
    data = request.get_json(silent=True) or {}

    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            return jsonify(error="姓名不能为空"), 400
        p.display_name = name[:64]
    if "department_id" in data:
        p.department_id = org.normalize_dept(data.get("department_id"))
    if "role_ids" in data:
        role_ids = org.normalize_roles(data.get("role_ids"))
        if not role_ids:
            return jsonify(error="至少选择一个角色"), 400
        p.role_ids = json.dumps(role_ids)
    if "scope" in data:
        p.scope = org.normalize_scope(data.get("scope"))
    if "active" in data:
        u.is_active = bool(data.get("active"))
    if "platform_role" in data:
        new_role = str(data.get("platform_role") or "").strip()
        err = role_change_error(g.current_user, u, new_role)
        if err:
            return jsonify(error=err), 403
        u.role = new_role
    db.session.commit()
    record_audit(g.current_user.id, "update_member", "user", uid,
                 {"account": u.username, "scope": p.scope, "role": u.role})
    counts = db.session.query(Device).filter_by(created_by=uid).count()
    return jsonify(org.member_dict(u, p, counts))


@org_bp.delete("/org/members/<int:uid>")
@require_perm("org.member")
def delete_member(uid):
    """删除成员账号（等同删用户）：连同组织档案一起去掉。"""
    u = _member_or_404(uid)
    if u is None:
        return jsonify(error="成员不存在"), 404
    err = delete_user_error(g.current_user, u)
    if err:
        return jsonify(error=err), 403
    from ..models import AuditLog
    AuditLog.query.filter_by(actor_id=uid).update({"actor_id": g.current_user.id})
    # 名下云手机归还公共池，否则会挂在已删除的 user_id 上谁也拿不到
    released = db.session.query(Device).filter(Device.created_by == uid).update(
        {"created_by": None, "sub_id": None}, synchronize_session=False)
    profile = db.session.get(OrgMember, uid)
    if profile is not None:
        db.session.delete(profile)
    account = u.username
    db.session.delete(u)
    db.session.commit()
    record_audit(g.current_user.id, "delete_member", "user", uid,
                 {"account": account, "released_devices": released})
    return jsonify(message="已删除", released_devices=released)


@org_bp.post("/org/members/<int:uid>/reset-password")
@require_perm("org.member")
def reset_member_password(uid):
    u = _member_or_404(uid)
    if u is None:
        return jsonify(error="成员不存在"), 404
    password = _gen_password()
    u.hashed_password = hash_password(password)
    u.failed_attempts = 0
    u.locked_until = None
    db.session.commit()
    record_audit(g.current_user.id, "reset_member_password", "user", uid,
                 {"account": u.username})
    return jsonify(password=password)


# ---------------------------------------------------------------------------
# 资源（云手机）分派
# ---------------------------------------------------------------------------
@org_bp.get("/org/members/<int:uid>/devices")
@require_perm("org.member")
def member_devices(uid):
    u = _member_or_404(uid)
    if u is None:
        return jsonify(error="成员不存在"), 404
    owned = (db.session.query(Device).filter(Device.created_by == uid)
             .order_by(Device.id).all())
    free = (db.session.query(Device).filter(Device.created_by.is_(None))
            .order_by(Device.id).limit(200).all())
    fmt = lambda d: {"id": d.id, "name": d.name, "status": d.status,
                     "ip": d.ip, "serial": d.serial}
    return jsonify(owned=[fmt(d) for d in owned], free=[fmt(d) for d in free])


@org_bp.post("/org/members/<int:uid>/devices")
@require_perm("org.member")
def assign_member_devices(uid):
    u = _member_or_404(uid)
    if u is None:
        return jsonify(error="成员不存在"), 404
    data = request.get_json(silent=True) or {}
    wanted = set()
    for did in (data.get("device_ids") or []):
        try:
            wanted.add(int(did))
        except (TypeError, ValueError):
            continue

    owned = db.session.query(Device).filter(Device.created_by == uid).all()
    owned_ids = {d.id for d in owned}
    # 收回：原来属于他、这次没勾选的
    for d in owned:
        if d.id not in wanted:
            d.created_by = None
    # 分派：勾选的空闲设备（或本来就属于他的）
    for did in wanted:
        if did in owned_ids:
            continue
        d = db.session.get(Device, did)
        if d is None:
            continue
        if d.created_by is not None and d.created_by != uid:
            continue    # 已被他人占用，跳过
        d.created_by = uid
    db.session.commit()
    try:
        from .. import egress as _egress
        for did in wanted:
            _dev = db.session.get(Device, did)
            if _dev is not None and _dev.created_by == uid:
                _egress.maybe_bind(_dev)
    except Exception:  # noqa: BLE001 - 绑出口失败不影响分派
        db.session.rollback()
    record_audit(g.current_user.id, "assign_member_devices", "user", uid,
                 {"count": len(wanted)})
    return jsonify(message="已保存", count=len(wanted))


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
def _clean_perms(raw) -> list:
    # 企业账号只能授予"自己拿得到"的权限：平台专属权限直接丢弃
    valid = set(org.all_perms_for(getattr(g, "current_user", None)))
    out, seen = [], set()
    for code in (raw or []):
        code = str(code or "").strip()
        if code in valid and code not in seen:
            seen.add(code)
            out.append(code)
    return out


@org_bp.post("/org/roles")
@require_perm("org.role")
def create_role():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify(error="角色名称必填"), 400
    r = tenant.stamp(OrgRole(name=name[:64], code=str(data.get("code") or "").strip()[:64],
                    description=str(data.get("description") or "").strip()[:255],
                    perms=json.dumps(_clean_perms(data.get("perms")), ensure_ascii=False),
                    sort=int(data.get("sort") or 0)))
    db.session.add(r)
    db.session.commit()
    record_audit(g.current_user.id, "create_role", "org_role", r.id, {"name": name})
    return jsonify(r.to_dict()), 201


@org_bp.patch("/org/roles/<int:rid>")
@require_perm("org.role")
def update_role(rid):
    r = db.session.get(OrgRole, rid)
    if r is None:
        return jsonify(error="角色不存在"), 404
    data = request.get_json(silent=True) or {}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            return jsonify(error="角色名称不能为空"), 400
        r.name = name[:64]
    if "code" in data:
        r.code = str(data.get("code") or "").strip()[:64]
    if "description" in data:
        r.description = str(data.get("description") or "").strip()[:255]
    if "perms" in data:
        r.perms = json.dumps(_clean_perms(data.get("perms")), ensure_ascii=False)
    db.session.commit()
    record_audit(g.current_user.id, "update_role", "org_role", rid, {"name": r.name})
    return jsonify(r.to_dict())


@org_bp.delete("/org/roles/<int:rid>")
@require_perm("org.role")
def delete_role(rid):
    r = db.session.get(OrgRole, rid)
    if r is None:
        return jsonify(error="角色不存在"), 404
    # 从成员身上摘掉这个角色
    for p in db.session.query(OrgMember).all():
        try:
            ids = json.loads(p.role_ids or "[]")
        except (TypeError, ValueError):
            ids = []
        if rid in ids:
            p.role_ids = json.dumps([x for x in ids if x != rid])
    db.session.delete(r)
    db.session.commit()
    record_audit(g.current_user.id, "delete_role", "org_role", rid, {"name": r.name})
    return jsonify(message="已删除")
