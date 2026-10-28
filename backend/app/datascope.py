"""数据范围（仅我名下 / 本部门 / 全公司）服务端实现。

角色里的 scope 之前只影响设备列表，文件、任务、脚本、分组都没过滤——
测试报告 ENT-002 就是"仅我名下"的用户能看到别人上传的文件。
统一在这里算"这个账号能看哪些人的数据"，各路由复用。
"""
from __future__ import annotations

from .extensions import db
from .models import Department, OrgMember, User


def _dept_members(dept_id: int, include_children: bool) -> set:
    from . import org as org_mod

    if dept_id is None:
        return set()
    ids = org_mod.subtree_ids(dept_id) if include_children else [dept_id]
    rows = (db.session.query(OrgMember.user_id)
            .filter(OrgMember.department_id.in_(ids)).all())
    return {r[0] for r in rows}


def visible_owner_ids(user) -> set | None:
    """返回可见的"归属人"用户 id 集合；None 表示不限（平台账号 / 全公司）。"""
    if user is None:
        return set()
    # 平台账号（无企业归属的 admin/superadmin）看全部
    if getattr(user, "enterprise_id", None) is None and user.role in ("admin", "superadmin"):
        return None

    profile = db.session.get(OrgMember, user.id)
    scope = (profile.scope if profile is not None else "self") or "self"

    if scope == "all":
        # 本企业全部成员 + 自己
        rows = (db.session.query(User.id)
                .filter(User.enterprise_id == user.enterprise_id).all())
        ids = {r[0] for r in rows}
        ids.add(user.id)
        return ids

    if scope in ("dept", "dept_sub"):
        ids = _dept_members(profile.department_id if profile else None,
                            include_children=(scope == "dept_sub"))
        ids.add(user.id)
        return ids

    return {user.id}


def apply_owner_scope(query, column, user):
    """按数据范围过滤查询；column 是"归属人"字段（如 FileRecord.uploader_id）。"""
    ids = visible_owner_ids(user)
    if ids is None:
        return query
    if not ids:
        return query.filter(db.literal(False))
    return query.filter(column.in_(ids))


def can_access_owner(owner_id, user) -> bool:
    ids = visible_owner_ids(user)
    if ids is None:
        return True
    if owner_id is None:
        return False
    return owner_id in ids
