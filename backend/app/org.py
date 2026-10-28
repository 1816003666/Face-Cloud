# -*- coding: utf-8 -*-
"""组织架构：部门树 / 角色 / 成员档案。

对应前端「部门与角色」页面（源自「部门成员与角色管理」原型）：
- 部门：树形，成员归属某个部门，数据范围相对部门计算；
- 角色：只配置功能权限（perms），可多选、权限取并集；
- 成员：平台用户（users）的组织档案，含部门 / 角色 / 数据范围 / 启停。
"""
from __future__ import annotations

import json

from .extensions import db
from .models import Department, OrgMember, OrgRole, User

# 功能权限模块（前后端共用这份定义，避免两边漂移）
# 按项目真实能力划分：每个模块对应侧边栏的一到两个页面，权限项对应实际接口
MODULES = [
    {"name": "设备管理", "items": [
        ["device.view", "查看设备"],
        ["device.create", "创建/批量创建"],
        ["device.delete", "删除/批量删除"],
        ["device.rename", "重命名"],
        ["device.power", "开机/关机/重启"],
        ["device.control", "远程控制（adb/截图/投屏）"],
        ["device.group", "分组设置"],
        ["device.import", "扫描/导入/同步"],
        ["device.apk", "安装 APK"],
        ["device.rotation", "轮次运行"],
        ["device.assign", "资源分派"]]},
    {"name": "文件与脚本", "items": [
        ["file.view", "查看文件"],
        ["file.upload", "上传文件"],
        ["file.push", "推送/安装到设备"],
        ["file.delete", "删除文件"],
        ["file.browse", "浏览设备文件"],
        ["script.view", "查看脚本"],
        ["script.manage", "脚本编排"],
        ["script.run", "执行脚本"]]},
    {"name": "任务与告警", "items": [
        ["task.view", "查看任务"],
        ["task.manage", "任务编排"],
        ["task.run", "立即执行任务"],
        ["alert.view", "查看告警"],
        ["alert.handle", "告警处理"]]},
    {"name": "计费与套餐", "items": [
        ["plan.view", "查看套餐"],
        ["plan.manage", "套餐管理（增删改/上下架）"],
        ["order.view", "查看订单"],
        ["sub.view", "查看订阅"],
        ["refund.audit", "退款审核"],
        ["bill.stats", "消费统计"]]},
    {"name": "组织与成员", "items": [
        ["org.view", "查看部门"],
        ["org.create", "创建/编辑部门"],
        ["org.member", "成员管理"],
        ["org.role", "角色管理"]]},
    {"name": "服务器与网络", "items": [
        ["server.view", "查看服务器"],
        ["server.manage", "服务器管理"],
        ["line.manage", "出口线路"],
        ["vpc.manage", "VPC 开通"]]},
    {"name": "系统与审计", "items": [
        ["settings.manage", "系统设置"],
        ["image.manage", "镜像管理"],
        ["diag.run", "诊断修复"],
        ["audit.view", "查看审计日志"],
        ["audit.export", "数据导出"]]},
]

ALL_PERMS = [code for m in MODULES for code, _label in m["items"]]


def is_platform_account(user) -> bool:
    """平台账号 = 无企业归属的 admin / superadmin。"""
    if user is None:
        return False
    return (getattr(user, "enterprise_id", None) is None
            and getattr(user, "role", "") in ("admin", "superadmin"))


def _platform_only() -> frozenset:
    from .perms import PLATFORM_ONLY
    return PLATFORM_ONLY


def modules_for(user) -> list:
    """按账号身份过滤功能分组。

    企业账号永远拿不到平台专属能力（服务器/线路/VPC、系统设置/镜像/诊断/审计、
    套餐与退款管控、告警处理），所以「部门与角色」里也不该出现这些分组——
    显示了也勾不了、勾了也用不了，只会让企业管理员困惑。
    """
    if is_platform_account(user):
        return MODULES
    only = _platform_only()
    out = []
    for m in MODULES:
        items = [it for it in m["items"] if it[0] not in only]
        if items:
            out.append({"name": m["name"], "items": items})
    return out


def all_perms_for(user) -> list:
    return [code for m in modules_for(user) for code, _label in m["items"]]

SCOPES = [
    {"code": "all", "name": "全公司", "desc": "全公司所有成员名下的数据"},
    {"code": "dept_sub", "name": "本部门及下级成员", "desc": "所在部门 + 全部下级部门的成员"},
    {"code": "dept", "name": "本部门成员", "desc": "仅所在部门的成员"},
    {"code": "self", "name": "仅我名下", "desc": "仅归属人是自己的数据"},
]

# 基础功能：个人用户和企业用户都有（不依赖组织角色）
# 对应菜单：我的套餐 / 设备管理 / 任务调度 / 文件管理 / 脚本管理 / 分组管理
# 企业角色只能在此基础上"加分"（导入、编排、退款审核等），不能把基础功能拿掉。
BASIC_PERMS = frozenset({
    "device.view", "device.control", "device.create", "device.delete",
    "device.rename", "device.power", "device.group", "device.apk",
    "device.rotation",
    "file.view", "file.upload", "file.push", "file.delete", "file.browse",
    "script.view", "script.run", "script.manage",
    "task.view", "task.run", "task.manage",
    "plan.view", "sub.view", "order.view",
})


def effective_perms(user) -> set:
    """账号最终生效的权限 = 基础功能 + 组织角色/账号级授权。"""
    return set(BASIC_PERMS) | user_perms(user.id)


# 只读审计类角色：各模块的"查看"权限
_VIEW_ONLY = ["device.view", "file.view", "script.view", "task.view", "alert.view",
              "plan.view", "order.view", "sub.view", "org.view", "server.view"]

DEFAULT_ROLES = [
    {"name": "超级管理员", "code": "super_admin", "description": "拥有全部功能权限",
     "perms": ALL_PERMS},
    {"name": "部门负责人", "code": "dept_owner",
     "description": "管理本部门的设备、文件脚本与成员，可处理告警",
     "perms": ["device.view", "device.create", "device.delete", "device.rename",
               "device.power", "device.control", "device.group", "device.assign",
               "file.view", "file.upload", "file.push", "file.browse",
               "script.view", "script.run",
               "task.view", "task.run", "alert.view", "alert.handle",
               "order.view", "sub.view", "org.view", "org.member", "audit.view"]},
    {"name": "普通用户", "code": "normal_user",
     "description": "使用分配给自己的云手机，查看自己的套餐与账单",
     "perms": ["device.view", "device.control", "file.view", "file.upload",
               "plan.view", "sub.view", "order.view"]},
    {"name": "财务", "code": "finance", "description": "管理套餐与账单，审核退款",
     "perms": ["plan.view", "plan.manage", "order.view", "sub.view",
               "refund.audit", "bill.stats", "org.view", "audit.view"]},
    {"name": "只读审计", "code": "auditor", "description": "只读角色，用于内部审计与合规检查",
     "perms": _VIEW_ONLY + ["audit.view", "audit.export"]},
]

# 老权限码 → 新权限码（原型里那套通用云主机口径），用于已有角色的一次性迁移
LEGACY_PERMS = {
    "res.view": "device.view",
    "res.create": "device.create",
    "res.resize": None,          # 项目没有改规格
    "res.stop": "device.power",
    "res.release": "device.delete",
    "res.assign": "device.assign",
    "bill.view": "sub.view",
    "bill.export": "audit.export",
    "fund.view": None,           # 项目没有钱包体系
    "fund.recharge": None,
    "fund.adjust": None,
    "org.view": "org.view",
    "org.create": "org.create",
    "org.member": "org.member",
    "org.role": "org.role",
    "audit.view": "audit.view",
}

ROOT_DEPT_NAME = "薪约科技"


def _migrate_role_perms() -> int:
    """把角色上的老权限码迁移到新口径，返回改动的角色数。

    - 默认角色（code 命中 DEFAULT_ROLES）直接回落成新默认，避免逐条映射的语义偏差；
    - 自定义角色按 LEGACY_PERMS 逐条映射，映射不到的项目没有对应功能，丢弃；
    - 已经是新口径的角色不动。
    """
    valid = set(ALL_PERMS)
    defaults = {r["code"]: r["perms"] for r in DEFAULT_ROLES}
    changed = 0
    for role in db.session.query(OrgRole).all():
        try:
            perms = json.loads(role.perms or "[]")
        except (TypeError, ValueError):
            perms = []
        if not perms or all(p in valid for p in perms):
            continue
        if role.code in defaults:
            new = list(defaults[role.code])
        else:
            new = []
            for code in perms:
                mapped = LEGACY_PERMS.get(code, code)
                if mapped and mapped in valid and mapped not in new:
                    new.append(mapped)
                elif code in valid and code not in new:
                    new.append(code)
        role.perms = json.dumps(new, ensure_ascii=False)
        changed += 1
    if changed:
        db.session.commit()
    return changed


def ensure_seed() -> None:
    """首次访问时准备根部门与默认角色（幂等）。"""
    # 用户被删除后清理孤儿档案，避免统计里出现"看不见的成员"
    orphans = db.session.query(OrgMember).filter(
        ~OrgMember.user_id.in_(db.session.query(User.id))
    ).delete(synchronize_session=False)
    if orphans:
        db.session.commit()   # delete 必须跟一次 commit，否则请求结束就丢了
    if db.session.query(Department.id).first() is None:
        db.session.add(Department(name=ROOT_DEPT_NAME, parent_id=None, sort=0))
        db.session.commit()
    if db.session.query(OrgRole.id).first() is None:
        for i, r in enumerate(DEFAULT_ROLES):
            db.session.add(OrgRole(name=r["name"], code=r["code"],
                                   description=r["description"],
                                   perms=json.dumps(r["perms"], ensure_ascii=False),
                                   sort=(i + 1) * 10))
        db.session.commit()
    else:
        _migrate_role_perms()


def root_department() -> Department | None:
    return (db.session.query(Department)
            .filter(Department.parent_id.is_(None))
            .order_by(Department.id).first())


def department_path(dept_id, _guard=None) -> str:
    """部门全路径，如「薪约科技 / 技术部 / 运维组」。"""
    seen = _guard or set()
    if dept_id in seen:
        return ""
    seen.add(dept_id)
    d = db.session.get(Department, dept_id)
    if d is None:
        return ""
    if d.parent_id is None:
        return d.name
    parent = department_path(d.parent_id, seen)
    return (parent + " / " + d.name) if parent else d.name


def subtree_ids(dept_id) -> list:
    out = [dept_id]
    kids = db.session.query(Department.id).filter(Department.parent_id == dept_id).all()
    for (kid,) in kids:
        out += subtree_ids(kid)
    return out


def normalize_dept(department_id):
    """校验部门存在且属于当前企业；不合法返回 None。"""
    if department_id in (None, "", 0):
        return None
    try:
        did = int(department_id)
    except (TypeError, ValueError):
        return None
    d = db.session.get(Department, did)
    if d is None:
        return None
    from . import tenant
    ent = tenant.current_enterprise_id()
    if ent is not None and d.enterprise_id not in (ent, None):
        return None      # 不能把成员挂到别的企业的部门
    return did


def normalize_roles(role_ids) -> list:
    """过滤掉不存在的角色 id，保序去重。"""
    out, seen = [], set()
    for rid in (role_ids or []):
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        if rid in seen:
            continue
        role = db.session.get(OrgRole, rid)
        if role is None:
            continue
        from . import tenant as _tenant
        ent = _tenant.current_enterprise_id()
        if ent is not None and role.enterprise_id not in (ent, None):
            continue     # 不能用别的企业的角色
        seen.add(rid)
        out.append(rid)
    return out


def normalize_scope(scope) -> str:
    scope = str(scope or "").strip()
    return scope if any(s["code"] == scope for s in SCOPES) else "self"


def get_member(user_id) -> OrgMember | None:
    return db.session.get(OrgMember, user_id)


def user_perms(user_id: int) -> set:
    """该账号生效的功能权限。

    优先级：**账号级授权**（平台管理员在「用户管理 → 功能权限」里分配）优先，
    没有账号级授权时才回落到组织角色的权限并集。这样平台管理员把某人的档位
    调低会立即生效，不会被他身上那个"超级管理员"角色顶回去。
    """
    from .models import UserPerm

    up = db.session.get(UserPerm, user_id)
    if up is not None:
        try:
            granted = json.loads(up.perms or "[]")
        except (TypeError, ValueError):
            granted = []
        if granted:
            return set(granted)

    perms = set()
    profile = db.session.get(OrgMember, user_id)
    if profile is None:
        return perms
    try:
        role_ids = json.loads(profile.role_ids or "[]")
    except (TypeError, ValueError):
        return perms
    if not role_ids:
        return perms
    rows = db.session.query(OrgRole).filter(OrgRole.id.in_(role_ids)).all()
    for r in rows:
        try:
            perms.update(json.loads(r.perms or "[]"))
        except (TypeError, ValueError):
            continue
    # 企业账号永远不持有平台专属权限（历史角色数据里可能残留，这里统一裁掉）
    from .models import User
    u = db.session.get(User, user_id)
    if u is not None and not is_platform_account(u):
        perms -= set(_platform_only())
    return perms


def member_dict(user: User, profile: OrgMember | None, device_count: int = 0,
                valid_role_ids=None) -> dict:
    roles = []
    if profile is not None:
        try:
            roles = json.loads(profile.role_ids or "[]")
        except (TypeError, ValueError):
            roles = []
    if valid_role_ids is not None:
        roles = [r for r in roles if r in valid_role_ids]
    dept_id = profile.department_id if profile is not None else None
    return {
        "id": user.id,
        "account": user.username,
        "name": (profile.display_name if profile and profile.display_name else user.username),
        "platform_role": user.role,
        "department_id": dept_id,
        "department_path": department_path(dept_id) if dept_id else "",
        "role_ids": roles,
        "scope": (profile.scope if profile is not None and profile.scope else "self"),
        "active": bool(user.is_active),
        "devices": device_count,
    }


def overview() -> dict:
    ensure_seed()
    return {
        "modules": MODULES,
        "scopes": SCOPES,
        "all_perms": ALL_PERMS,
    }
