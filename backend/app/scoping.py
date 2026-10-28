# -*- coding: utf-8 -*-
"""设备归属隔离（租户隔离）。

规则：
- admin / superadmin：可见并可管理**全部**云手机；
- 其他角色（viewer / operator，含门户影子用户）：
    * 必须持有**有效套餐订阅**，否则看不到任何云手机；
    * 只能看到 / 操作 created_by == 自己 的云手机。

用法：
- enforce_tenant_scope(app)：注册 before_request 守卫，拦截"按设备 id 直取"的请求
  （/api/devices/<id>... 与 /api/files/devices/<id>...），无需逐个改路由；
- scope_query(q, user)：给列表/统计查询套上可见范围（兼容 Select 与 Query）；
- scoped_device_ids / foreign_device_ids：批量接口过滤设备 id。
"""
from __future__ import annotations

import re

from flask import request, jsonify
from sqlalchemy.orm import Query

from .extensions import db
from .models import Device

# 可见全部设备的角色
PRIVILEGED_ROLES = ("admin", "superadmin")

# 按设备 id 访问的路径：/api/devices/<id>... 与 /api/files/devices/<id>...
_DEVICE_PATH_RE = re.compile(r"^/api/(?:devices|files/devices)/(\d+)(?:/|$)")


def is_privileged(user) -> bool:
    return bool(user) and getattr(user, "role", "") in PRIVILEGED_ROLES


def is_platform_admin(user) -> bool:
    """平台管理员：管理员角色 + 没有企业归属，才能跨企业看全部数据。

    企业管理员（role=admin 但挂在某企业下）只能看本企业的设备。
    """
    return is_privileged(user) and getattr(user, "enterprise_id", None) is None


def has_active_plan(user) -> bool:
    """是否持有有效套餐订阅。计费模块异常时按无权限处理（fail-closed）。"""
    if user is None:
        return False
    try:
        from .billing import has_valid_subscription
        return bool(has_valid_subscription(user.id))
    except Exception:  # noqa: BLE001
        return False


def device_in_enterprise(user, device) -> bool:
    """设备是否属于该账号所在企业（以设备上的企业标记或当前持有人的企业为准）。"""
    ent = getattr(user, "enterprise_id", None)
    if ent is None:
        return True          # 平台账号不限
    if getattr(device, "enterprise_id", None) == ent:
        return True
    owner_id = getattr(device, "created_by", None)
    if not owner_id:
        return False
    from .models import User
    owner = db.session.get(User, owner_id)
    return owner is not None and owner.enterprise_id == ent


def can_access_device(user, device) -> bool:
    if device is None:
        return False
    if is_platform_admin(user):
        return True
    if not device_in_enterprise(user, device):
        return False
    if is_privileged(user):
        return True          # 企业管理员：本企业内全部设备
    if not has_active_plan(user):
        return False
    return device.created_by is not None and device.created_by == user.id


def _ent_cond(user):
    """企业过滤条件（平台账号返回 None）。

    设备的归属企业以「当前持有人的企业」为准（created_by → users.enterprise_id），
    同时兼容设备上显式写入的 enterprise_id；公共池设备（无归属）对企业不可见。
    """
    ent = getattr(user, "enterprise_id", None)
    if ent is None:
        return None
    from .models import User
    owners = db.session.query(User.id).filter(User.enterprise_id == ent)
    return (Device.enterprise_id == ent) | (Device.created_by.in_(owners))


def _apply(q, cond):
    if isinstance(q, Query):
        return q.filter(cond)
    return q.where(cond)


def scope_query(q, user):
    """限定可见范围；无套餐时返回恒空查询。"""
    if is_platform_admin(user):
        return q
    if is_privileged(user):
        # 企业管理员：本企业全部设备
        ent_cond = _ent_cond(user)
        return _apply(q, ent_cond) if ent_cond is not None else _apply(q, Device.id < 0)
    if not has_active_plan(user):
        return _apply(q, Device.id < 0)   # 恒不成立
    q = _apply(q, Device.created_by == user.id)
    ent_cond = _ent_cond(user)
    return _apply(q, ent_cond) if ent_cond is not None else q


def scoped_device_ids(user, ids):
    """从 ids 中筛出可操作的设备 id（保持原顺序）。"""
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return []
    if is_platform_admin(user):
        return ids
    if is_privileged(user):
        ent_cond = _ent_cond(user)
        q = db.session.query(Device.id).filter(Device.id.in_(ids))
        if ent_cond is not None:
            q = q.filter(ent_cond)
        allowed = {r[0] for r in q.all()}
        return [i for i in ids if i in allowed]
    if not has_active_plan(user):
        return []
    q = (db.session.query(Device.id)
         .filter(Device.id.in_(ids), Device.created_by == user.id))
    ent_cond = _ent_cond(user)
    if ent_cond is not None:
        q = q.filter(ent_cond)
    allowed = {r[0] for r in q.all()}
    return [i for i in ids if i in allowed]


def foreign_device_ids(user, ids):
    """返回 ids 中当前用户无权操作的部分（用于直接 403）。"""
    ids = [int(i) for i in (ids or [])]
    if not ids or is_privileged(user):
        return []
    allowed = set(scoped_device_ids(user, ids))
    return [i for i in ids if i not in allowed]


def enforce_tenant_scope(app):
    """注册归属隔离守卫。"""

    @app.before_request
    def _tenant_scope_guard():  # noqa: ANN202
        m = _DEVICE_PATH_RE.match(request.path or "")
        if not m:
            return None
        from .auth import _resolve_user
        user = _resolve_user()
        if user is None:
            return None  # 未登录：交给各路由的 login_required 统一 401
        if is_platform_admin(user):
            return None
        did = int(m.group(1))
        d = db.session.get(Device, did)
        if d is None:
            return jsonify(error="设备不存在"), 404
        # 企业管理员：本企业内放行，跨企业一律 403
        if is_privileged(user):
            if not device_in_enterprise(user, d):
                return jsonify(error="无权访问该设备", code="FORBIDDEN_TENANT"), 403
            return None
        if not has_active_plan(user):
            return jsonify(error="套餐已到期或未购买，请先购买/续费套餐",
                           code="NO_SUBSCRIPTION"), 403
        if d.created_by is None or d.created_by != user.id:
            return jsonify(error="无权访问该设备", code="FORBIDDEN_DEVICE"), 403
        return None
