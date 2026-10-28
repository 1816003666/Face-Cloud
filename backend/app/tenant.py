# -*- coding: utf-8 -*-
"""多企业（租户）硬隔离。

隔离由三层共同保证：
1. 数据层：14 张业务表都有 enterprise_id；
2. 查询层：Session 级 do_orm_execute 钩子——**任何 ORM 查询自动追加**
   `enterprise_id = 当前企业`（不需要每个路由记得写过滤）；
3. 写入层：before_flush 钩子给新对象自动打企业标记，并拒绝改/删别家企业的行。

平台账号（enterprise_id 为空）不过滤；平台账号还可以带 `X-Enterprise-Id`
请求头"以某企业视角"查看，用于平台运维排查。


约定：
- `enterprises` 一行 = 一家企业；
- `*.enterprise_id IS NULL` 表示**平台级**：平台运营账号、公共设备池、平台通用套餐；
- 有 `enterprise_id` 的账号只能看到本企业的数据；
- 平台账号（enterprise_id 为空且角色为 admin/superadmin）可跨企业管理。

对外入口：
- ensure_default_enterprise()：首次启动时建默认企业，并把存量数据回填进去；
- current_enterprise_id()：当前请求所属企业（None = 平台）；
- scope(query, model)：给查询套上企业过滤；
- in_tenant(obj)：判断某条记录是否属于当前企业。
"""
from __future__ import annotations

from flask import g

from .extensions import db
from .models import (Alert, AuditLog, Department, Device, Enterprise, FileRecord,
                     Group, Order, OrgRole, Plan, RefundRequest, ScheduledTask,
                     Script, Subscription, User)

DEFAULT_NAME = "薪约科技"
DEFAULT_CODE = "SY-001"

# 需要按企业隔离的表（回填与过滤共用这份清单）
# 注意：Plan（套餐目录）默认是平台级的，不进回填清单；企业专属套餐由企业自行创建
TENANT_MODELS = (User, Department, Device, Order, Subscription, RefundRequest,
                 AuditLog, Group, OrgRole, Script, FileRecord, Alert, ScheduledTask)

# 存量回填用的清单：用户单独处理（平台账号要保留 NULL），不能一锅端
BACKFILL_MODELS = tuple(m for m in TENANT_MODELS if m is not User)


def ensure_default_enterprise() -> Enterprise:
    """保证有默认企业；把 enterprise_id 为空的历史数据挂到默认企业下。幂等。"""
    ent = db.session.query(Enterprise).order_by(Enterprise.id).first()
    if ent is None:
        ent = Enterprise(name=DEFAULT_NAME, code=DEFAULT_CODE, status="active")
        db.session.add(ent)
        db.session.commit()
    changed = 0
    # 历史版本曾把公共池设备回填到默认企业。无归属用户的设备必须先归还平台，
    # 否则下面的循环会因为 enterprise_id 已非空而永久保留这份错误归属。
    changed += db.session.query(Device).filter(
        Device.created_by.is_(None),
        Device.enterprise_id.isnot(None),
    ).update({"enterprise_id": None}, synchronize_session=False)
    for model in BACKFILL_MODELS:
        q = db.session.query(model).filter(model.enterprise_id.is_(None))
        if model is Device:
            # 公共池设备属于「平台」，不能挂到某家企业；
            # 只有已经分配给某个用户的设备才需要跟着人走。
            q = q.filter(Device.created_by.isnot(None))
        changed += q.update({"enterprise_id": ent.id}, synchronize_session=False)
    # 用户不在这里回填。
    # 以前这里把所有非 admin 的账号都挂到默认企业，导致「个人版自助注册」的账号
    # 每次重启后自动变成默认企业（薪约科技）的员工账号，账号所属莫名其妙。
    # 现在规则：enterprise_id 为空 = 个人账号（平台级，只能看自己的设备）；
    # 要进某家企业，由管理员在「用户管理 → 账号归属」里显式指定。
    if changed:
        db.session.commit()
    return ent


def current_enterprise_id():
    """当前请求所属企业 id；None 表示平台级（可跨企业）。"""
    if hasattr(g, "enterprise_id"):
        return g.enterprise_id
    return None


class TenantViolation(RuntimeError):
    """跨企业写入被拒绝。"""


def set_current(user) -> None:
    """登录/鉴权通过后把企业上下文写进 g。

    平台账号可以用请求头 `X-Enterprise-Id` 指定"以哪家企业视角"查看，
    这时数据被过滤到该企业，但账号本身仍是平台账号（可管理企业）。
    """
    g.current_user = user
    ent = getattr(user, "enterprise_id", None) if user is not None else None
    view_as = None
    if ent is None and user is not None:
        try:
            from flask import request
            raw = (request.headers.get("X-Enterprise-Id") or "").strip()
        except RuntimeError:
            raw = ""
        if raw:
            try:
                view_as = int(raw)
            except ValueError:
                view_as = None
    g.enterprise_id = ent if ent is not None else view_as
    g.view_as_enterprise = view_as is not None


def is_platform(user) -> bool:
    """平台级账号：没有企业归属（可跨企业）。"""
    return bool(user) and getattr(user, "enterprise_id", None) is None


def scope(query, model):
    """给查询套上企业过滤（平台账号不限制）。"""
    ent_id = current_enterprise_id()
    if ent_id is None:
        return query
    col = getattr(model, "enterprise_id", None)
    if col is None:
        return query
    if hasattr(query, "filter"):
        return query.filter(col == ent_id)
    return query.where(col == ent_id)


def scope_with_public(query, model):
    """企业可见「本企业 + 平台通用（enterprise_id 为空）」的数据，如套餐。"""
    ent_id = current_enterprise_id()
    if ent_id is None:
        return query
    col = getattr(model, "enterprise_id", None)
    if col is None:
        return query
    cond = (col == ent_id) | (col.is_(None))
    if hasattr(query, "filter"):
        return query.filter(cond)
    return query.where(cond)


def belongs(obj) -> bool:
    """该记录是否属于当前企业（平台账号恒 True）。"""
    ent_id = current_enterprise_id()
    if ent_id is None:
        return True
    return getattr(obj, "enterprise_id", None) == ent_id


def stamp(obj):
    """给新对象打上当前企业标记（平台账号创建的对象保持为空）。"""
    ent_id = current_enterprise_id()
    if ent_id is not None and hasattr(obj, "enterprise_id") and getattr(obj, "enterprise_id", None) is None:
        obj.enterprise_id = ent_id
    return obj


def init_app(app) -> None:
    """注册"结构性"隔离：ORM 查询自动过滤 + 写入自动打标/越权拦截。"""
    from flask import jsonify
    from sqlalchemy import event
    from sqlalchemy.orm import Session, with_loader_criteria

    @event.listens_for(Session, "do_orm_execute")
    def _auto_scope(state):  # noqa: ANN001
        if not state.is_select:
            return
        if state.execution_options.get("bypass_tenant"):
            return
        ent = current_enterprise_id()
        if ent is None:
            return
        opts = []
        for model in TENANT_MODELS:
            col = model.enterprise_id
            opts.append(with_loader_criteria(model, col == ent, include_aliases=True))
        # 套餐：企业专属 + 平台通用（enterprise_id 为空）
        opts.append(with_loader_criteria(
            Plan, (Plan.enterprise_id == ent) | Plan.enterprise_id.is_(None),
            include_aliases=True))
        state.statement = state.statement.options(*opts)

    @event.listens_for(Session, "before_flush")
    def _stamp_and_guard(session, flush_context, instances):  # noqa: ANN001, ARG001
        ent = current_enterprise_id()
        if ent is None:
            return
        for obj in session.new:
            if hasattr(obj, "enterprise_id") and getattr(obj, "enterprise_id", None) is None:
                obj.enterprise_id = ent
        for obj in list(session.dirty) + list(session.deleted):
            if not hasattr(obj, "enterprise_id"):
                continue
            oent = getattr(obj, "enterprise_id", None)
            if oent is not None and oent != ent:
                raise TenantViolation(
                    f"不能修改其它企业的数据（{type(obj).__name__}#{getattr(obj, 'id', '?')}）")

    @app.errorhandler(TenantViolation)
    def _tenant_violation(e):  # noqa: ANN001
        return jsonify(error=str(e), code="TENANT_VIOLATION"), 403
