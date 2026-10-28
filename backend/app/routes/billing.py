# -*- coding: utf-8 -*-
"""预付费计费 API（套餐 / 下单 / 模拟支付 / 订阅 / 提醒）。"""
import json
import uuid
from datetime import timedelta

from flask import Blueprint, request, jsonify, g

from ..extensions import db
from ..models import Order, Plan, Subscription, User, record_audit, _utcnow
from ..auth import login_required, require_role, require_perm, require_platform
from .. import billing
from .. import tenant

billing_bp = Blueprint("billing", __name__)


def _with_username(rows: list) -> list:
    """给订阅/退款单这类"只有 user_id"的列表补上用户名，前端就不用显示 #13 这种内部 ID。

    一次批量查 users，避免 N+1。
    """
    ids = {r.get("user_id") for r in rows if r.get("user_id")}
    if not ids:
        return rows
    users = {u.id: u for u in db.session.query(User).filter(User.id.in_(ids)).all()}
    for r in rows:
        u = users.get(r.get("user_id"))
        r["username"] = u.username if u else ""
        r["display_name"] = (getattr(u, "display_name", "") or "") if u else ""
    return rows


# ---------------- 套餐 ----------------
@billing_bp.get("/plans")
@login_required
def list_plans():
    """套餐列表（含包月/包季）；附带实时可用库存，前端据此显示"已售罄"。"""
    import json as _json
    from .. import portal, scoping
    # 管理员能看到已停用的套餐（方便重新启用）；普通用户只看在售的
    include_inactive = scoping.is_privileged(g.current_user)
    out = []
    for p in billing.list_plans(include_inactive=include_inactive):
        # 未上架（没有门户商品号）的套餐只给管理员看：
        # 管理员用它判断"这套餐还没上架"，用户购买页不该出现也买不到。
        if not include_inactive and not (p.sku_id or "").strip():
            continue
        d = p.to_dict()
        try:
            specs = _json.loads(p.specs or "{}")
        except (TypeError, ValueError):
            specs = {}
        try:
            d["stock"] = portal._stock(99999, specs, p)
        except Exception:  # noqa: BLE001
            d["stock"] = None
        # 起购规格：台数 / 时长下限（套餐标称值）
        d["min_qty"] = billing.plan_min_qty(p)
        d["min_hours"] = billing.plan_min_hours(p)
        d["min_days"] = max(1, round(d["min_hours"] / 24))
        out.append(d)
    return jsonify(out)


@billing_bp.post("/plans")
@require_perm("plan.manage")
def create_plan():
    """管理员创建套餐。"""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(error="套餐名必填"), 400
    try:
        price_cents = int(data.get("price_cents", 0))
    except (TypeError, ValueError):
        return jsonify(error="价格必须是整数（分）"), 400
    if price_cents <= 0:
        return jsonify(error="价格必须大于 0"), 400
    raw_hours = data.get("period_hours")
    period_hours = None
    if raw_hours not in (None, ""):
        try:
            period_hours = int(raw_hours)
        except (TypeError, ValueError):
            return jsonify(error="周期小时数必须是整数"), 400
        if period_hours < 1:
            return jsonify(error="周期小时数至少为 1"), 400
        duration_months = max(1, (period_hours + 719) // 720)
    else:
        try:
            duration_months = int(data.get("duration_months", 1))
        except (TypeError, ValueError):
            return jsonify(error="时长格式错误"), 400
        if duration_months not in (1, 3):
            return jsonify(error="时长仅支持 1（包月）或 3（包季）个月"), 400
    server_ips = data.get("server_ips") or []
    if not isinstance(server_ips, list):
        server_ips = []
    server_ips = [str(x).strip() for x in server_ips if str(x).strip()]
    sku_id = (data.get("sku_id") or "").strip() or None
    if sku_id and Plan.query.filter_by(sku_id=sku_id).first() is not None:
        return jsonify(error="门户商品号已被占用"), 400
    _specs = dict(data.get("specs") or {})
    try:
        _specs["device_count"] = max(1, int(data.get("device_count") or _specs.get("device_count") or 1))
    except (TypeError, ValueError):
        _specs.setdefault("device_count", 1)
    p = Plan(
        name=name,
        model=(data.get("model") or "标准版").strip(),
        price_cents=price_cents,
        # 台数与 specs 保持镜像，避免列里永远是默认 1
        device_count=int(_specs.get("device_count") or 1),
        duration_months=duration_months,
        period_hours=period_hours,
        alloc_ips=json.dumps(server_ips, ensure_ascii=False),
        sku_id=sku_id,
        specs=json.dumps(_specs, ensure_ascii=False),
        sort=int(data.get("sort", 0)),
        active=bool(data.get("active", True)),
    )
    db.session.add(p)
    db.session.flush()
    if not p.sku_id:
        p.sku_id = "phone-%d" % p.id  # 未填 SKU 时自动生成，保证能同步到门户
    db.session.commit()
    record_audit(g.current_user.id, "create_plan", "plan", p.id,
                 {"name": name, "price_cents": price_cents})
    return jsonify(p.to_dict()), 201


# ---------------- 套餐管理（admin） ----------------
@billing_bp.put("/plans/<int:plan_id>")
@require_perm("plan.manage")
def update_plan(plan_id):
    """管理员编辑套餐。"""
    p = db.session.get(Plan, plan_id)
    if p is None:
        return jsonify(error="套餐不存在"), 404
    data = request.get_json(silent=True) or {}
    if "name" in data and str(data.get("name") or "").strip():
        p.name = str(data["name"]).strip()
    if "model" in data and str(data.get("model") or "").strip():
        p.model = str(data["model"]).strip()
    if "price_cents" in data:
        try:
            price_cents = int(data["price_cents"])
        except (TypeError, ValueError):
            return jsonify(error="价格必须是整数（分）"), 400
        if price_cents <= 0:
            return jsonify(error="价格必须大于 0"), 400
        p.price_cents = price_cents
    if "period_hours" in data:
        try:
            ph = int(data["period_hours"])
        except (TypeError, ValueError):
            return jsonify(error="周期小时数必须是整数"), 400
        if ph < 1:
            return jsonify(error="周期小时数至少为 1"), 400
        p.period_hours = ph
        p.duration_months = max(1, (ph + 719) // 720)
    if "sku_id" in data:
        sku_id = (data.get("sku_id") or "").strip() or None
        if sku_id:
            other = Plan.query.filter(Plan.sku_id == sku_id, Plan.id != p.id).first()
            if other is not None:
                return jsonify(error="门户商品号已被占用"), 400
        p.sku_id = sku_id
    if "server_ips" in data:
        ips = data.get("server_ips") or []
        if not isinstance(ips, list):
            ips = []
        p.alloc_ips = json.dumps(
            [str(x).strip() for x in ips if str(x).strip()], ensure_ascii=False)
    if "duration_months" in data:
        try:
            dm = int(data["duration_months"])
        except (TypeError, ValueError):
            return jsonify(error="时长格式错误"), 400
        if dm not in (1, 3):
            return jsonify(error="时长仅支持 1（包月）或 3（包季）个月"), 400
        p.duration_months = dm
    if "device_count" in data:
        try:
            dc = int(data["device_count"])
        except (TypeError, ValueError):
            return jsonify(error="云手机数量必须是整数"), 400
        if dc < 1:
            return jsonify(error="云手机数量至少为 1"), 400
        p.device_count = dc
    if "specs" in data or "device_count" in data:
        _s = dict(data.get("specs") or {}) if "specs" in data else json.loads(p.specs or "{}")
        if "device_count" in data:
            _s["device_count"] = dc
        p.specs = json.dumps(_s, ensure_ascii=False)
        # 列与 specs 对齐（台数以 specs 为准）
        billing.sync_plan_device_count(p)
    if "sort" in data:
        p.sort = int(data.get("sort", 0))
    if "active" in data:
        p.active = bool(data.get("active", True))
    if not p.sku_id:
        p.sku_id = "phone-%d" % p.id  # 老套餐补自动 SKU
    db.session.commit()
    record_audit(g.current_user.id, "update_plan", "plan", p.id, {"name": p.name})
    return jsonify(p.to_dict())


@billing_bp.delete("/plans/<int:plan_id>")
@require_perm("plan.manage")
def delete_plan(plan_id):
    """管理员删除套餐（有订单/订阅关联时仅停用，不物理删除）。"""
    p = db.session.get(Plan, plan_id)
    if p is None:
        return jsonify(error="套餐不存在"), 404
    # 直接物理删除：历史订单/订阅保留快照（plan_id 悬空后 plan_name 显示为空），
    # 用原生 SQL 删除，避免 ORM 把子表 plan_id 置 NULL 触发非空约束。
    from sqlalchemy import text
    db.session.execute(text("DELETE FROM plans WHERE id = :pid"), {"pid": plan_id})
    db.session.commit()
    db.session.expire_all()
    record_audit(g.current_user.id, "delete_plan", "plan", plan_id, {})
    return jsonify({"message": "已删除套餐"})


@billing_bp.post("/plans/<int:plan_id>/toggle")
@require_perm("plan.manage")
def toggle_plan(plan_id):
    """管理员启停套餐。"""
    p = db.session.get(Plan, plan_id)
    if p is None:
        return jsonify(error="套餐不存在"), 404
    p.active = not p.active
    db.session.commit()
    record_audit(g.current_user.id, "toggle_plan", "plan", p.id, {"active": p.active})
    return jsonify(p.to_dict())


# ---------------- 订单 / 订阅管理（admin） ----------------
@billing_bp.get("/admin/orders")
@require_platform
def admin_orders():
    """全部订单（新的在前），可按状态过滤。"""
    q = tenant.scope(db.session.query(Order), Order).order_by(Order.id.desc())
    status = (request.args.get("status") or "").strip()
    if status:
        q = q.filter_by(status=status)
    orders = q.limit(500).all()
    # 管理台需要看清"这单是谁下的"：补上用户名与所属企业
    from ..models import User
    user_rows = {u.id: u for u in db.session.query(User).filter(
        User.id.in_([o.user_id for o in orders] or [0])).all()}
    ent_names = {}
    try:
        from ..models import Enterprise
        ent_names = {e.id: e.name for e in db.session.query(Enterprise).all()}
    except Exception:  # noqa: BLE001
        ent_names = {}
    out = []
    for o in orders:
        d = o.to_dict()
        u = user_rows.get(o.user_id)
        if u is not None:
            d["username"] = u.username
            d["enterprise_name"] = ent_names.get(u.enterprise_id, "")
        d.setdefault("username", "")
        d.setdefault("enterprise_name", "")
        out.append(d)
    return jsonify(out)


@billing_bp.get("/admin/portal-orders")
@require_platform
def admin_portal_orders():
    """门户订单：/internal/provision 落下的开通记录（管理员可查）。"""
    from ..models import PortalProvision, PortalUserMap, Plan, User
    rows = (db.session.query(PortalProvision)
            .order_by(PortalProvision.id.desc()).limit(300).all())
    plans = {p.sku_id: p for p in db.session.query(Plan).all() if p.sku_id}
    maps = {m.portal_user_id: m.local_user_id
            for m in db.session.query(PortalUserMap).all()}
    users = {u.id: u.username for u in db.session.query(User).all()}
    out = []
    for r in rows:
        plan = plans.get(r.sku_id)
        uid = maps.get(r.portal_user_id)
        try:
            device_ids = json.loads(r.device_ids or "[]")
        except (TypeError, ValueError):
            device_ids = []
        out.append({
            "id": r.id,
            "order_item_id": r.order_item_id,
            "sku_id": r.sku_id,
            "plan_name": plan.name if plan else "",
            "amount_yuan": round((plan.price_cents or 0) / 100, 2) if plan else None,
            "portal_user_id": r.portal_user_id,
            "local_user_id": uid,
            "username": users.get(uid, ""),
            "quantity": r.quantity,
            "period_hours": r.period_hours,
            "status": r.status,
            "instance_id": r.instance_id,
            "device_ids": device_ids,
            "expire_at": r.expire_at.isoformat() if r.expire_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "error_code": r.error_code,
            "error_msg": r.error_msg,
        })
    return jsonify(out)


@billing_bp.get("/admin/subscriptions")
@require_platform
def admin_subscriptions():
    """生效中的订阅（status=active 且未到期，新的在前）。"""
    from datetime import datetime, timedelta
    subs = (tenant.scope(db.session.query(Subscription), Subscription)
            .filter(Subscription.status == "active",
                    Subscription.expires_at > datetime.utcnow())
            .order_by(Subscription.id.desc()).limit(500).all())
    return jsonify(_with_username([s.to_dict() for s in subs]))


@billing_bp.get("/admin/billing-stats")
@require_platform
def billing_stats():
    """计费概览：订单数/收入/活跃订阅/待支付。"""
    paid = tenant.scope(db.session.query(Order), Order).filter_by(status="paid").all()
    revenue = sum(o.amount_cents for o in paid) / 100
    return jsonify({
        "order_total": tenant.scope(db.session.query(Order), Order).count(),
        "paid_total": len(paid),
        "revenue_yuan": round(revenue, 2),
        "active_subs": tenant.scope(db.session.query(Subscription), Subscription).filter_by(status="active").count(),
        "pending_orders": tenant.scope(db.session.query(Order), Order).filter_by(status="pending").count(),
    })


# ---------------- 订单 / 支付 ----------------
@billing_bp.get("/orders/quote")
@login_required
def quote_order():
    """下单前报价：?plan_id=1&qty=2&hours=4 → 总价/单价/默认时长。"""
    plan_id = request.args.get("plan_id")
    try:
        plan = billing.get_plan(int(plan_id))
    except (TypeError, ValueError):
        plan = None
    if plan is None:
        return jsonify(error="套餐不存在"), 404
    default_hours = billing.plan_period_hours(plan)
    raw_units = (request.args.get("units") or "").strip()
    if raw_units:
        try:
            units = int(raw_units)
        except (TypeError, ValueError):
            return jsonify(error="份数不合法"), 400
        q = billing.quote_by_units(plan, units)
        return jsonify(**q, plan_id=plan.id, plan_name=plan.name,
                       default_hours=default_hours,
                       default_qty=q["min_qty"])
    try:
        qty = int(request.args.get("qty") or 1)
        hours = int(request.args.get("hours") or default_hours)
    except (TypeError, ValueError):
        return jsonify(error="数量或时长不合法"), 400
    q = billing.quote(plan, qty, hours)
    return jsonify(**q, plan_id=plan.id, plan_name=plan.name,
                   default_hours=default_hours,
                   default_qty=q["min_qty"],
                   price_cents=plan.price_cents)


@billing_bp.post("/orders")
@login_required
def create_order():
    """创建预付费订单。

    body: {"plan_id": 1, "channel": "mock"|"alipay"|"wechat"}
    channel=mock 时可直接调 POST /orders/<id>/pay 模拟支付到账（测试用）。
    """
    cur = g.current_user
    data = request.get_json(silent=True) or {}
    plan_id = data.get("plan_id")
    channel = (data.get("channel") or "mock").strip()
    try:
        pid = int(plan_id)
    except (TypeError, ValueError):
        return jsonify(error="缺少有效的 plan_id"), 400

    # 用户在这次订单里选的台数与时长（不传则用套餐默认值）
    raw_units = data.get("units")
    raw_qty = data.get("qty")
    raw_hours = data.get("hours")
    units = None
    try:
        if raw_units not in (None, ""):
            units = int(raw_units)
        qty = int(raw_qty) if raw_qty not in (None, "") else None
        hours = int(raw_hours) if raw_hours not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify(error="数量或时长不合法"), 400
    if units is not None and (units < 1 or units > 100):
        return jsonify(error="份数需在 1~100 之间"), 400
    if qty is not None and (qty < 1 or qty > 2000):
        return jsonify(error="台数不合法"), 400
    if hours is not None and (hours < 1 or hours > 24 * 366):
        return jsonify(error="时长不合法"), 400

    # 同一套餐已有未支付订单时复用，但把台数/时长/金额按这次的选择更新，
    # 避免用户改了数量后还沿用旧订单。
    existing = (Order.query
                .filter_by(user_id=cur.id, plan_id=pid, status="pending")
                .order_by(Order.id.desc()).first())
    plan = billing.get_plan(pid)
    if plan is None:
        return jsonify(error="套餐不存在或已下架"), 400
    # 库存校验：别让用户选完台数才发现没货
    try:
        from .. import portal as portal_mod
        import json as _json
        try:
            specs = _json.loads(plan.specs or "{}")
        except (TypeError, ValueError):
            specs = {}
        avail = portal_mod._stock(99999, specs, plan)
        # 按份购买时，要买多少台 = 份数 × 每份台数
        need = qty
        if units is not None:
            need = units * max(1, billing.plan_min_qty(plan))
        if need and avail < need:
            return jsonify(error=f"库存不足：当前仅剩 {avail} 台，请减少份数或稍后再试", stock=avail), 409
    except Exception:  # noqa: BLE001 - 库存算不出来不阻塞下单
        pass

    if existing is not None:
        if units is not None:
            q = billing.quote_by_units(plan, units)
        else:
            q = billing.quote(plan, qty or existing.qty or 1,
                              hours or existing.hours or billing.plan_period_hours(plan))
            units = max(1, round((q["qty"] or 1) / max(1, billing.plan_min_qty(plan))))
        existing.units = units
        existing.qty = q["qty"]
        existing.hours = q["hours"]
        existing.amount_cents = q["amount_cents"]
        existing.unit_price_cents = int(round(q["unit_price_cents"]))
        existing.channel = channel
        db.session.commit()
        return jsonify(existing.to_dict()), 200

    try:
        order = billing.create_order(cur.id, pid, channel, qty=qty, hours=hours, units=units)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(order.to_dict()), 201


@billing_bp.post("/orders/<int:order_id>/pay")
@login_required
def pay_order(order_id):
    """模拟支付：测试环境确认订单到账并激活订阅。

    body: {"trade_no": "可选，支付平台流水号"}
    """
    cur = g.current_user
    order = db.session.get(Order, order_id)
    if order is None or order.user_id != cur.id:
        return jsonify(error="订单不存在"), 404
    if order.status == "paid":
        return jsonify(error="订单已支付"), 400
    data = request.get_json(silent=True) or {}
    trade_no = (data.get("trade_no") or "").strip()
    try:
        sub = billing.confirm_order_paid(order.id, trade_no=trade_no)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify({
        "message": "支付成功",
        "order": db.session.get(Order, order.id).to_dict(),
        "subscription": sub.to_dict(),
    })


@billing_bp.post("/pay/notify")
def pay_notify():
    """第三方支付网关回调（alipay/wechat 接入时由网关调用）。

    支持 out_trade_no + trade_no 的 JSON 体；支付宝表单通知需接入时再扩展解析。
    """
    data = request.get_json(silent=True) or {}
    out_trade_no = (data.get("out_trade_no") or "").strip()
    trade_no = (data.get("trade_no") or "").strip()
    if not out_trade_no:
        return jsonify(error="缺少 out_trade_no"), 400
    order = Order.query.filter_by(out_trade_no=out_trade_no).first()
    if order is None:
        return jsonify(error="订单不存在"), 404
    if order.status == "paid":
        return jsonify(message="重复通知", result="success")
    try:
        billing.confirm_order_paid(order.id, trade_no=trade_no)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(message="success", result="success")


# ---------------- 订阅 / 状态 / 提醒 ----------------
@billing_bp.get("/subscription")
@login_required
def my_subscription():
    """当前用户订阅状态（含剩余天数 + 套餐额度）。"""
    data = billing.subscription_status(g.current_user.id)
    # 套餐额度：quota = 生效订阅 qty 之和（同套餐重复购买是"台数叠加"）
    #           used  = 该用户已创建的云手机数
    #           plan_specs = 最近购买的生效订阅规格（用于"套餐配置 vs 后台现有云手机配置"匹配）
    import json as _json
    from datetime import datetime as _dt
    from ..models import Subscription, Device
    uid = g.current_user.id
    subs = (Subscription.query
            .filter_by(user_id=uid, status="active")
            .filter(Subscription.expires_at > _dt.utcnow())
            .order_by(Subscription.id.desc())
            .all())
    data["device_quota"] = sum((s.qty or 1) for s in subs)
    data["device_used"] = Device.query.filter_by(created_by=uid).count()
    # 已过期但名下仍挂着设备的订阅：这些设备没被回收，配额要算进去，
    # 否则会出现"配额 20 / 已用 30"的矛盾
    expired = (Subscription.query
               .filter(Subscription.user_id == uid,
                       Subscription.status.in_(("active", "expired")),
                       Subscription.expires_at <= _dt.utcnow())
               .all())
    if expired:
        data["device_quota"] += (Device.query
                                 .filter(Device.created_by == uid,
                                         Device.sub_id.in_([s.id for s in expired]))
                                 .count())
    specs = {}
    if subs and subs[0].plan:
        try:
            specs = _json.loads(subs[0].plan.specs or "{}")
        except Exception:
            specs = {}
    data["plan_specs"] = specs
    return jsonify(data)


@billing_bp.get("/orders")
@login_required
def my_orders():
    """当前用户的订单列表；?minutes=15 只看最近 N 分钟创建的（订阅记录页用）。"""
    from datetime import timedelta
    from ..models import _utcnow
    try:
        minutes = int(request.args.get("minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    q = Order.query.filter_by(user_id=g.current_user.id)
    # 用户删掉的订单只是从自己列表里隐藏，平台侧（计费管理/收入统计）仍然保留
    q = q.filter(Order.hidden_by_user.isnot(True))
    if minutes > 0:
        q = q.filter(Order.created_at >= _utcnow() - timedelta(minutes=minutes))
    return jsonify([o.to_dict() for o in q.order_by(Order.id.desc()).all()])


def _order_locked_by_live_sub(o) -> bool:
    """这笔订单是否「正在支撑一个生效中的订阅」。

    只有这种情况禁止删除：订单删了、套餐却还在用会自相矛盾。
    续费单（paid_at ≠ 订阅 started_at）不算。
    """
    if o is None or o.status != "paid" or o.paid_at is None:
        return False
    live = (db.session.query(Subscription)
            .filter(Subscription.user_id == o.user_id,
                    Subscription.plan_id == o.plan_id,
                    Subscription.status == "active",
                    Subscription.expires_at.isnot(None),
                    Subscription.expires_at > _utcnow())
            .order_by(Subscription.expires_at.desc())
            .first())
    if live is None or live.started_at is None:
        return False
    return abs((live.started_at - o.paid_at).total_seconds()) <= 60


@billing_bp.delete("/orders/<int:oid>")
@login_required
def delete_my_order(oid):
    """用户从自己的订单列表里删除一条记录（软删除，只对自己隐藏）。

    规则：
    - 只能删自己的订单；
    - 已支付且仍在生效的订阅对应订单不给删，避免"删了订单但机器还在用"；
    - 其余（待支付/已取消/已过期/已退款/续费单）都可以删；
    - 平台侧保留记录，收入统计、对账不受影响。
    """
    from ..models import Subscription, record_audit
    o = db.session.get(Order, oid)
    if o is None or o.user_id != g.current_user.id:
        return jsonify(error="订单不存在"), 404
    if _order_locked_by_live_sub(o):
        return jsonify(error="该订单对应的套餐仍在生效，到期后即可删除"), 400
    o.hidden_by_user = True
    db.session.commit()
    record_audit(g.current_user.id, "hide_order", "order", o.id,
                 {"out_trade_no": o.out_trade_no, "status": o.status})
    return jsonify(ok=True, order_id=o.id)


@billing_bp.post("/orders/batch-delete")
@login_required
def batch_delete_my_orders():
    """批量删除自己的订单（软隐藏）。生效中订阅对应的订单会单独返回，不中断整批。"""
    from ..models import record_audit
    data = request.get_json(silent=True) or {}
    raw = data.get("ids") or []
    if not isinstance(raw, list) or not raw:
        return jsonify(error="请选择要删除的订单"), 400
    ids = []
    for x in raw[:200]:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not ids:
        return jsonify(error="订单号不合法"), 400

    hidden, blocked, missing = [], [], []
    for oid in ids:
        o = db.session.get(Order, oid)
        if o is None or o.user_id != g.current_user.id:
            missing.append(oid)
            continue
        if _order_locked_by_live_sub(o):
            blocked.append(oid)
            continue
        o.hidden_by_user = True
        hidden.append(oid)
    if hidden:
        db.session.commit()
        record_audit(g.current_user.id, "hide_orders", "order", None,
                     {"count": len(hidden), "ids": hidden[:50]})
    return jsonify(ok=len(hidden), hidden=hidden, blocked=blocked, missing=missing)


@billing_bp.delete("/subscriptions/<int:sub_id>")
@login_required
def delete_my_subscription(sub_id):
    """用户删除自己的一条订阅记录（软删除，只从列表隐藏）。

    只允许删除已取消/已过期的记录；生效中的订阅必须通过到期或退款结束。
    """
    from ..models import Subscription, record_audit
    sub = db.session.get(Subscription, sub_id)
    if sub is None or sub.user_id != g.current_user.id:
        return jsonify(error="订阅不存在"), 404
    if sub.status == "active":
        return jsonify(error="生效中的订阅不能删除"), 400
    # 用查询级 update：即使 ORM 里的值因历史脏数据被读成 True，也能真正落库
    db.session.query(Subscription).filter_by(id=sub_id).update(
        {"user_deleted": True}, synchronize_session=False)
    db.session.commit()
    record_audit(g.current_user.id, "delete_subscription", "subscription", sub_id,
                 {"plan_id": sub.plan_id, "status": sub.status})
    return jsonify(message="已删除")


@billing_bp.get("/my/device-stats")
@login_required
def my_device_stats():
    """「我的云手机」统计：只算**已购买套餐**分配到的设备，不把平台设备池算进去。

    - 个人用户：自己有效订阅名下的设备
    - 企业管理员：本企业所有成员有效订阅名下的设备（企业统一采购的场景）
    - 平台账号：不适用（他们看数据看板）
    """
    from ..models import Device, Subscription, User

    cur = g.current_user
    is_enterprise_admin = (cur.enterprise_id is not None
                           and cur.role in ("admin", "superadmin"))
    if is_enterprise_admin:
        owner_ids = [u.id for u in
                     db.session.query(User).filter(User.enterprise_id == cur.enterprise_id).all()]
    else:
        owner_ids = [cur.id]

    if not owner_ids:
        return jsonify(total=0, online=0, subscriptions=0)

    sub_ids = [s.id for s in (db.session.query(Subscription)
                              .filter(Subscription.user_id.in_(owner_ids),
                                      Subscription.status == "active")
                              .all())]
    if not sub_ids:
        return jsonify(total=0, online=0, subscriptions=0)

    rows = (db.session.query(Device.status)
            .filter(Device.sub_id.in_(sub_ids))
            .all())
    total = len(rows)
    online = sum(1 for (st,) in rows if st == "running")
    return jsonify(total=total, online=online, subscriptions=len(sub_ids))


@billing_bp.get("/subscriptions")
@login_required
def my_subscriptions():
    """当前用户全部订阅历史（新的在前），附带该订阅最近一次退款申请的状态。"""
    from ..models import RefundRequest
    subs = billing.user_subscriptions(g.current_user.id)
    out = [s.to_dict() for s in subs]
    # 续费要用：套餐标价（一份）与周期，前端据此显示金额和续费后的到期时间
    for d, s in zip(out, subs):
        try:
            d["plan_price_cents"] = int(getattr(s.plan, "price_cents", 0) or 0)
            d["plan_period_hours"] = int(billing.plan_period_hours(s.plan)) if s.plan else 0
        except Exception:  # noqa: BLE001
            d["plan_price_cents"] = 0
            d["plan_period_hours"] = 0
    if out:
        ids = [d["id"] for d in out]
        latest = {}
        rows = (RefundRequest.query
                .filter(RefundRequest.subscription_id.in_(ids),
                        RefundRequest.user_id == g.current_user.id)
                .order_by(RefundRequest.id.desc()))
        for r in rows:
            latest.setdefault(r.subscription_id, r)
        for d in out:
            r = latest.get(d["id"])
            if r is not None:
                d["refund"] = {
                    "id": r.id,
                    "status": r.status,
                    "reason": r.reason or "",
                    "admin_note": r.admin_note or "",
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "handled_at": r.handled_at.isoformat() if r.handled_at else None,
                }
    return jsonify(out)


@billing_bp.get("/billing/check")
@login_required
def check_access():
    """开机/建机前置校验：是否允许使用云手机。"""
    result = billing.check_device_access(g.current_user.id)
    return jsonify(result)


@billing_bp.get("/notifications")
@login_required
def my_notifications():
    """未读提醒（到期前 3 天 / 到期当天 / 已到期）。"""
    return jsonify(billing.unread_notifications(g.current_user.id))


@billing_bp.get("/notifications/all")
@login_required
def all_notifications():
    """全部站内消息：已读 + 未读，未读排前面，同组内按时间倒序。

    查询参数：status = all（默认）/ unread / read
              limit 默认 50（上限 200）、offset 默认 0
    返回 {items, total, unread, read, has_more}
    """
    status = (request.args.get("status") or "all").strip()
    limit = request.args.get("limit", type=int) or 50
    offset = request.args.get("offset", type=int) or 0
    return jsonify(billing.user_notifications(g.current_user.id, status, limit, offset))


@billing_bp.post("/notifications/read")
@login_required
def mark_read():
    """标记全部提醒已读。"""
    billing.mark_notifications_read(g.current_user.id)
    return jsonify(message="ok")


# ---------------- 退款申请（阶段2） ----------------
# 付款后这段时间内允许"已经建过云手机"的用户自助申请退款（仍需管理员审核）。
REFUND_GRACE_MINUTES = 5


@billing_bp.post("/subscriptions/<int:sub_id>/renew")
@login_required
def renew_subscription(sub_id):
    """续费订阅：只延长到期时间，不增加台数、不改设备归属。

    body: {"units": 1}   份数，1 份 = 套餐周期（小时），价格 = 套餐标价 × 份数
    规则：
    - 只能续自己名下、且未失效的订阅（cancelled 的不给续）；
    - 未过期的在原到期时间上叠加；已过期的从当前时间起算；
    - 生成一条 paid 订单，计入收入与订单列表；
    - 不新增订阅、不回收设备。
    """
    from ..models import Order as _Order
    cur = g.current_user
    sub = db.session.get(Subscription, sub_id)
    if sub is None or sub.user_id != cur.id:
        return jsonify(error="订阅不存在"), 404
    if sub.status == "cancelled":
        return jsonify(error="该订阅已作废，无法续费"), 400
    plan = billing.get_plan(sub.plan_id)
    if plan is None or not plan.active:
        return jsonify(error="套餐已下架，无法续费"), 400

    data = request.get_json(silent=True) or {}
    raw_units = data.get("units", 1)
    if raw_units in (None, ""):
        raw_units = 1
    try:
        units = int(raw_units)
    except (TypeError, ValueError):
        return jsonify(error="份数不合法"), 400
    # 注意别用 `or 1`：传 0 会被当成 1，用户没点倍数也被扣钱
    if units < 1 or units > 100:
        return jsonify(error="份数需在 1~100 之间"), 400

    period = billing.plan_period_hours(plan)
    add_seconds = units * period * 3600
    now = _utcnow()
    base = sub.expires_at if (sub.expires_at and sub.expires_at > now) else now
    new_exp = base + timedelta(seconds=add_seconds)

    amount = int(plan.price_cents or 0) * units
    order = _Order(
        user_id=cur.id,
        plan_id=plan.id,
        amount_cents=max(1, amount),
        qty=0,
        hours=units * period,
        unit_price_cents=int(round(billing.plan_unit_price_cents(plan))),
        channel="mock",
        source="web",
        status="paid",
        out_trade_no=billing.gen_out_trade_no(),
        trade_no=f"RENEW{uuid.uuid4().hex[:16].upper()}",
        paid_at=now,
        enterprise_id=getattr(cur, "enterprise_id", None),
    )
    db.session.add(order)
    sub.expires_at = new_exp
    if sub.status == "expired":
        sub.status = "active"
    db.session.commit()

    try:
        billing._invalidate_stock(plan.id)
    except Exception:  # noqa: BLE001
        pass
    record_audit(cur.id, "renew_subscription", "subscription", sub.id,
                 {"order_id": order.id, "units": units,
                  "expires_at": sub.expires_at.isoformat()})
    try:
        billing._notify(cur.id, "payment_success", f"续费成功：{plan.name}",
                        f"已支付 ¥{order.amount_cents / 100:.2f}，"
                        f"有效期延至 {sub.expires_at.strftime('%Y-%m-%d %H:%M')}。")
    except Exception:  # noqa: BLE001
        pass
    return jsonify(ok=True, order_id=order.id, subscription_id=sub.id,
                   amount_yuan=round(order.amount_cents / 100, 2),
                   units=units,
                   expires_at=sub.expires_at.isoformat() + "Z")


@billing_bp.post("/subscription/<int:sub_id>/refund")
@login_required
def request_refund(sub_id):
    """自助发起退款，生成待处理退款单给管理台审核。

    规则：
    - 订阅必须有效；
    - 必须能找到该套餐下已支付的订单；
    - 已经创建/使用过云手机的，只有**付款后 5 分钟内**还能自助申请，
      超过就要联系客服（防止"用完再退"）。
    """
    from datetime import datetime as _dt, timedelta
    from ..models import Subscription, Device, RefundRequest, Order, record_audit, _utcnow
    cur = g.current_user
    sub = db.session.get(Subscription, sub_id)
    if sub is None or sub.user_id != cur.id:
        return jsonify(error="订阅不存在"), 404
    if sub.status != "active":
        return jsonify(error="该订阅已失效，无法退款"), 400
    # 规则：只允许"付款生效的套餐订单"申请退款 —— 找到该用户在该套餐下最近一笔已支付订单
    paid_order = (Order.query
                  .filter_by(user_id=cur.id, plan_id=sub.plan_id, status="paid")
                  .order_by(Order.paid_at.desc())
                  .first())
    if paid_order is None:
        return jsonify(error="只有通过付款生效的套餐订单才能申请退款"), 400
    if Device.query.filter_by(created_by=cur.id).count() > 0:
        paid_at = paid_order.paid_at or sub.started_at
        if paid_at is None or (_utcnow() - paid_at) > timedelta(minutes=REFUND_GRACE_MINUTES):
            return jsonify(error="你已创建/使用过云手机，不支持自助退款，请联系客服"), 400
    if RefundRequest.query.filter_by(subscription_id=sub_id, status="pending").first():
        return jsonify(error="该订阅已有待处理的退款申请"), 400
    data = request.get_json(silent=True) or {}
    amount = int(sub.plan.price_cents) if sub.plan else 0
    r = RefundRequest(user_id=cur.id, subscription_id=sub_id, order_id=paid_order.id,
                      plan_name=(sub.plan.name if sub.plan else ""),
                      amount_cents=(paid_order.amount_cents or amount),
                      reason=(data.get("reason") or "").strip()[:200])
    db.session.add(r)
    db.session.commit()
    record_audit(cur.id, "refund_request", "subscription", sub_id, {"refund_id": r.id})
    return jsonify(r.to_dict()), 201


@billing_bp.get("/subscription/<int:sub_id>/refunds")
@login_required
def my_refund_history(sub_id):
    """当前用户某条订阅的全部退款申请（新的在前），前端用它画申请历史。"""
    from ..models import RefundRequest, Subscription
    sub = db.session.get(Subscription, sub_id)
    if sub is None or sub.user_id != g.current_user.id:
        return jsonify(error="订阅不存在"), 404
    rows = (RefundRequest.query
            .filter_by(subscription_id=sub_id, user_id=g.current_user.id)
            .order_by(RefundRequest.id.desc()).all())
    return jsonify([r.to_dict() for r in rows])


@billing_bp.get("/admin/refunds")
@require_platform
def admin_refunds():
    """管理台：退款单列表。"""
    from ..models import RefundRequest
    rows = tenant.scope(db.session.query(RefundRequest), RefundRequest).order_by(RefundRequest.id.desc()).all()
    return jsonify(_with_username([r.to_dict() for r in rows]))


@billing_bp.post("/admin/orders/<int:oid>/refund")
@require_platform
def admin_refund_order(oid):
    """平台主动退单：订单退款 → 作订阅废 → 收回该订阅名下的云手机。

    与「用户自助申请 → 管理员审核」不同，这里是管理员直接从订单列表发起，
    一步完成，同时落一条已通过的退款单方便在「退款单」页签追溯。
    """
    from datetime import datetime as _dt
    from ..models import RefundRequest, Device

    o = db.session.get(Order, oid)
    if o is None:
        return jsonify(error="订单不存在"), 404
    if o.status == "refunded":
        return jsonify(error="该订单已退款，无需重复操作"), 400
    if o.status != "paid":
        return jsonify(error="只有已支付的订单可以退单"), 400

    # 定位「这笔订单」对应的订阅。
    # 优先：同一用户 + 同一套餐里、与本次支付时间最接近的那条（±5 分钟内）。
    # 以前只找 status=active 的订阅，遇到"订单已过期后再退单"就找不到订阅，
    # 结果只退了钱、既不作废订阅也不回收设备（审计里 released=0、subscription_id=null）。
    subs = (db.session.query(Subscription)
            .filter(Subscription.user_id == o.user_id,
                    Subscription.plan_id == o.plan_id)
            .all())
    sub = None
    base = o.paid_at or o.created_at
    if base is not None:
        best_diff = None
        for s in subs:
            if s.started_at is None:
                continue
            diff = abs((s.started_at - base).total_seconds())
            if diff > 300:      # 超过 5 分钟的都不认为是这笔订单产生的
                continue
            if best_diff is None or diff < best_diff:
                best_diff = diff
                sub = s
    if sub is None:
        # 兜底：同用户的生效订阅（保持原行为，避免老数据找不到订阅）
        sub = next((s for s in subs if s.status == "active"), None)

    released = 0
    sub_id = None
    if sub is not None:
        sub_id = sub.id
        # 已过期/已取消也统一置为 cancelled：钱退了，订阅不该再显示"有效/已过期"
        sub.status = "cancelled"
        try:
            released = billing._release_sub_devices(sub.id) or 0
        except Exception as e:  # noqa: BLE001 - 回收失败不能挡住退款
            print(f"[billing] 退单回收设备失败 sub={sub.id}: {e}", flush=True)
        db.session.add(RefundRequest(
            user_id=o.user_id,
            subscription_id=sub.id,
            order_id=o.id,
            plan_name=(sub.plan.name if sub.plan else ""),
            amount_cents=o.amount_cents,
            reason="平台主动退单",
            status="approved",
            handled_at=_dt.utcnow(),
            admin_note=f"由 {g.current_user.username} 在订单列表发起",
        ))
        # 兜底：退完这一单，如果用户名下设备数已超过剩余有效额度，把多出来的收回
        try:
            released += billing.release_over_quota(o.user_id)
        except Exception as e:  # noqa: BLE001
            print(f"[billing] 超额设备回收失败 user={o.user_id}: {e}", flush=True)

    o.status = "refunded"
    db.session.commit()
    record_audit(g.current_user.id, "refund_order", "order", o.id,
                 {"amount_cents": o.amount_cents, "released": released,
                  "subscription_id": sub_id})
    return jsonify(ok=True, order_id=o.id, subscription_id=sub_id,
                   released=released,
                   amount_yuan=round((o.amount_cents or 0) / 100, 2))


@billing_bp.post("/admin/refunds/<int:rid>/<action>")
@require_platform
def admin_refund_action(rid, action):
    """管理台：approve（同意，订阅作废）/ reject（驳回）。"""
    from datetime import datetime as _dt
    from ..models import RefundRequest, Subscription
    if action not in ("approve", "reject"):
        return jsonify(error="未知操作"), 400
    r = db.session.get(RefundRequest, rid)
    if r is None:
        return jsonify(error="退款单不存在"), 404
    if r.status != "pending":
        return jsonify(error="该退款单已处理"), 400
    r.status = "approved" if action == "approve" else "rejected"
    r.handled_at = _dt.utcnow()
    if action == "approve":
        sub = db.session.get(Subscription, r.subscription_id)
        if sub is not None:
            sub.status = "cancelled"
            # 退款通过=服务终止：设备直接收回公共池（不停机、不重启），
            # 否则这些云手机会一直挂在该用户名下，套餐/门户库存永远不恢复。
            try:
                billing._release_sub_devices(sub.id)
            except Exception:  # noqa: BLE001 - 回收失败不能阻断退款
                pass
    db.session.commit()
    return jsonify(r.to_dict())
