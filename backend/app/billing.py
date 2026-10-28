# -*- coding: utf-8 -*-
"""预付费计费服务（客户端计费采用预计费：预购套餐 + 线上支付 + 到期提醒/断机）。

职责：
- 套餐：种子数据 + 列表
- 下单：创建订单（mock 模拟支付 / alipay / wechat 通道预留）
- 支付：确认支付 → 生成/续期订阅（续费在剩余有效期上叠加）
- 提醒：到期前 3 天提醒一次、到期当天再提醒一次（幂等，reminded 字段去重）
- 到期执行：订阅过期 → 置 expired + 断开该用户套餐内所有运行中的云手机
- 开机拦截：设备开机/创建前校验当前用户是否有有效订阅

支付渠道说明：
- channel=mock：本地测试用，点击“确认支付”直接回调到账，不接第三方；
- channel=alipay/wechat：预留线上支付接口，需商户号/网关配置（见 config PAY_*），
  接入真实网关时由 /api/pay/notify 回调确认支付。
"""
import json
import uuid
from datetime import datetime, timedelta

from flask import current_app

from .extensions import db
from .models import (Plan, Order, Subscription, Notification, User,
                     _utcnow, record_audit)

# 提醒时间窗口（天）
REMIND_EXPIRE_SOON_DAYS = 3


# ---------------------------------------------------------------------------
# 套餐
# ---------------------------------------------------------------------------
DEFAULT_PLANS = [
    # 与前端 Models 页机型对应：轻量版/标准版/畅玩版/旗舰版
    dict(name="轻量版套餐", model="轻量版", price_cents=1900, duration_months=1,
         specs={"resolution": "540×960", "cpu": "2核", "memory": "1GB",
                "storage": "8GB", "android": "Android 9"}, sort=10),
    dict(name="标准版套餐", model="标准版", price_cents=3900, duration_months=1,
         specs={"resolution": "720×1280", "cpu": "4核", "memory": "2GB",
                "storage": "16GB", "android": "Android 11"}, sort=20),
    dict(name="畅玩版套餐", model="畅玩版", price_cents=5900, duration_months=1,
         specs={"resolution": "720×1280", "cpu": "4核", "memory": "4GB",
                "storage": "32GB", "android": "Android 11"}, sort=30),
    dict(name="旗舰版套餐", model="旗舰版", price_cents=9900, duration_months=1,
         specs={"resolution": "1080×1920", "cpu": "8核", "memory": "8GB",
                "storage": "64GB", "android": "Android 12"}, sort=40),
    # 包季（3 个月，约 9 折）
    dict(name="标准版包季", model="标准版", price_cents=9900, duration_months=3,
         specs={"resolution": "720×1280", "cpu": "4核", "memory": "2GB",
                "storage": "16GB", "android": "Android 11"}, sort=50),
    dict(name="畅玩版包季", model="畅玩版", price_cents=14900, duration_months=3,
         specs={"resolution": "720×1280", "cpu": "4核", "memory": "4GB",
                "storage": "32GB", "android": "Android 11"}, sort=60),
    dict(name="旗舰版包季", model="旗舰版", price_cents=24900, duration_months=3,
         specs={"resolution": "1080×1920", "cpu": "8核", "memory": "8GB",
                "storage": "64GB", "android": "Android 12"}, sort=70),
]


def seed_plans():
    """首次启动写入默认套餐。

    只在套餐表为空时补种：管理员删掉的套餐不能因为重启又长回来
    （以前按名称+时长逐条补，删掉默认套餐后每次重启都会原地复活）。
    """
    if db.session.query(Plan.id).first() is not None:
        return
    for p in DEFAULT_PLANS:
        row = dict(p)
        row["specs"] = json.dumps(row["specs"], ensure_ascii=False)
        db.session.add(Plan(**row))
    db.session.commit()


def list_plans(include_inactive: bool = False) -> list:
    from . import tenant
    # 企业只能看到「平台通用套餐 + 本企业专属套餐」
    q = tenant.scope_with_public(db.session.query(Plan), Plan)
    if not include_inactive:
        q = q.filter(Plan.active.is_(True))
    # 已启用的排前面，其次按排序权重、ID
    return q.order_by(Plan.active.desc(), Plan.sort, Plan.id).all()


def get_plan(plan_id: int) -> Plan | None:
    return db.session.get(Plan, plan_id)


# ---------------------------------------------------------------------------
# 订单
# ---------------------------------------------------------------------------
def gen_out_trade_no() -> str:
    return f"CP{_utcnow():%Y%m%d%H%M%S}{uuid.uuid4().hex[:8].upper()}"


def sync_plan_device_count(plan) -> int:
    """把 plans.device_count 列与 specs.device_count 对齐。

    台数的唯一来源是 specs.device_count（计价、起购、分配都读它）；
    表上的 device_count 列只是冗余镜像，方便 SQL 查询与后台展示。
    任何改动套餐台数的地方都要调用它，避免两处打架。
    """
    import json as _json
    try:
        specs = _json.loads(plan.specs or "{}")
    except (TypeError, ValueError):
        specs = {}
    if not isinstance(specs, dict):
        specs = {}
    try:
        n = int(specs.get("device_count") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = max(1, int(getattr(plan, "device_count", 1) or 1))
        specs["device_count"] = n
        plan.specs = _json.dumps(specs, ensure_ascii=False)
    plan.device_count = n
    return n


def plan_min_qty(plan) -> int:
    """该套餐最少购买几台（= 套餐标称台数）。"""
    from .portal_provision import _devices_per_unit
    try:
        return max(1, int(_devices_per_unit(plan)))
    except Exception:  # noqa: BLE001
        return max(1, int(getattr(plan, "device_count", 1) or 1))


def plan_min_hours(plan) -> int:
    """该套餐最少买多久（= 套餐周期小时数）。"""
    return max(1, plan_period_hours(plan))


def plan_unit_price_cents(plan) -> float:
    """套餐单价：每台每小时（分）。价格 = price_cents / (台数 × 周期小时)。"""
    from .portal_provision import _devices_per_unit
    hours = plan_period_hours(plan)
    per_unit = max(1, int(_devices_per_unit(plan)))
    base = float(plan.price_cents or 0) / float(per_unit * hours)
    return max(base, 0.0)


def plan_period_hours(plan) -> int:
    return int(plan.period_hours or 0) or max(1, int(plan.duration_months or 1)) * 30 * 24


def quote(plan, qty: int, hours: int) -> dict:
    """报价：台数 × 时长 的总价（分）。

    套餐有「标称规格」：例如旗舰版 = 30 天 · 10 台。
    这个规格是**起购量**——台数与时长都不允许低于它，低于就按规格下限算。
    """
    min_qty = plan_min_qty(plan)
    min_hours = plan_min_hours(plan)
    qty = max(min_qty, int(qty or min_qty))
    hours = max(min_hours, int(hours or min_hours))
    unit = plan_unit_price_cents(plan)
    amount = int(round(unit * qty * hours))
    return {"qty": qty, "hours": hours, "unit_price_cents": round(unit, 4),
            "amount_cents": max(amount, 1),
            "min_qty": min_qty, "min_hours": min_hours}


def quote_by_units(plan, units: int) -> dict:
    """按「份」报价：1 份 = 套餐标称台数 × 套餐周期，价格 = 套餐标价 × 份数。

    站内购买与统一门户保持同一口径：用户只选买几份，台数和时长由套餐决定。
    """
    d = plan_min_qty(plan)          # 一份含几台
    h = plan_min_hours(plan)        # 一份多长
    units = max(1, int(units or 1))
    price = int(plan.price_cents or 0)
    return {
        "units": units,
        "qty": units * d,
        "hours": h,
        "device_count": d,
        "period_hours": h,
        "period_days": max(1, round(h / 24)),
        "price_cents": price,
        "unit_price_cents": round(plan_unit_price_cents(plan), 4),
        "amount_cents": max(1, price * units),
        "min_qty": d,
        "min_hours": h,
    }


def create_order(user_id: int, plan_id: int, channel: str = "mock",
                 qty: int | None = None, hours: int | None = None,
                 units: int | None = None) -> Order:
    """创建预付费订单。channel: mock / alipay / wechat。

    qty/hours 由用户在"确认购买"弹窗里选择：本次要几台云手机、用多长时间。
    不传则用套餐默认值（包含台数 / 套餐周期）。
    """
    plan = get_plan(plan_id)
    if plan is None or not plan.active:
        raise ValueError("套餐不存在或已下架")
    if channel not in ("mock", "alipay", "wechat"):
        raise ValueError("不支持的支付渠道")

    min_qty = plan_min_qty(plan)
    min_hours = plan_min_hours(plan)

    if units is not None:
        # 按份购买：台数 / 时长都由套餐决定，用户只选份数
        try:
            units = int(units)
        except (TypeError, ValueError):
            raise ValueError("份数不合法")
        if units < 1:
            raise ValueError("份数至少为 1")
        q = quote_by_units(plan, units)
    else:
        if qty is not None and int(qty) < min_qty:
            raise ValueError(f"该套餐最少购买 {min_qty} 台云手机")
        if hours is not None and int(hours) < min_hours:
            raise ValueError(f"该套餐最少购买 {round(min_hours / 24)} 天")
        q = quote(plan, qty or min_qty, hours or min_hours)
        units = max(1, round((q["qty"] or min_qty) / min_qty))

    order = Order(user_id=user_id, plan_id=plan.id,
                  units=units,
                  amount_cents=q["amount_cents"],
                  qty=q["qty"], hours=q["hours"],
                  unit_price_cents=int(round(q["unit_price_cents"])),
                  channel=channel, source="web", status="pending",
                  out_trade_no=gen_out_trade_no())
    db.session.add(order)
    db.session.commit()
    record_audit(user_id, "create_order", "order", order.id,
                 {"plan_id": plan.id, "amount_cents": order.amount_cents,
                  "qty": order.qty, "hours": order.hours, "channel": channel})
    return order


def cancel_stale_pending_orders(minutes: int = 0) -> int:
    """把超过 N 分钟未支付的 pending 订单置为 cancelled，返回取消数量。"""
    from datetime import timedelta
    from flask import current_app
    try:
        mins = int(minutes or current_app.config.get("ORDER_PENDING_TIMEOUT_MIN", 30) or 0)
    except (TypeError, ValueError):
        mins = 30
    if mins <= 0:
        return 0
    cutoff = _utcnow() - timedelta(minutes=mins)
    rows = (db.session.query(Order)
            .filter(Order.status == "pending", Order.created_at < cutoff)
            .all())
    for o in rows:
        o.status = "cancelled"
    if rows:
        db.session.commit()
    return len(rows)


def confirm_order_paid(order_id: int, trade_no: str = "") -> Subscription:
    """确认订单已支付（模拟支付回调 / 第三方网关回调共用）。

    - 订单置 paid；
    - **每笔订单生成一条独立订阅**：到期时间各自独立，到期时只回收该订阅名下的设备。
      以前把同套餐的新订单合并到旧订阅里（台数叠加 + 到期取较晚者），
      结果是"再买一台"顺带给之前所有的机器延期，总量超发、且旧机器不会按时回收。
    - 返回新生成的 active 订阅。
    """
    order = db.session.get(Order, order_id)
    if order is None:
        raise ValueError("订单不存在")
    if order.status == "paid":
        raise ValueError("订单已支付")
    plan = get_plan(order.plan_id)
    if plan is None:
        raise ValueError("套餐不存在")

    # 购买只买"额度"，不预分配设备：分配发生在用户点「创建」时
    # （见 routes/devices.py::create_device 的公共池分配分支）。
    # 时长/台数优先用订单里用户选择的值；历史订单没有就回退套餐默认值
    hours = int(order.hours or 0) or plan_period_hours(plan)
    add_days = timedelta(hours=hours)
    from .portal_provision import _devices_per_unit
    qty_add = int(order.qty or 0) or _devices_per_unit(plan)

    order.status = "paid"
    order.trade_no = trade_no or f"TRADE{uuid.uuid4().hex[:16].upper()}"
    order.paid_at = _utcnow()
    _invalidate_stock(plan.id)

    now = _utcnow()
    # 每笔订单一条独立订阅：台数、时长都只属于这笔订单，
    # 到期时各订阅分别回收各自名下的设备（设备创建时绑定到具体 sub_id，
    # 见 routes/devices.py::_active_sub_quotas）。
    from .models import User as _User
    _buyer = db.session.get(_User, order.user_id)
    sub = Subscription(
        user_id=order.user_id,
        plan_id=plan.id,
        status="active",
        started_at=now,
        expires_at=now + add_days,
        qty=qty_add,
        # 订阅继承买家的企业归属：企业用户查订阅/额度时会被企业过滤，
        # 这里留空会让"我的套餐"显示成未开通。
        enterprise_id=(getattr(_buyer, "enterprise_id", None) if _buyer else None),
    )
    db.session.add(sub)
    db.session.commit()
    record_audit(order.user_id, "order_paid", "order", order.id,
                 {"out_trade_no": order.out_trade_no, "trade_no": order.trade_no,
                  "sub_expires": sub.expires_at.isoformat()})

    # 充值成功也发一条站内消息（消息页可见）。只影响"消息"，失败不回滚支付
    try:
        _notify(
            order.user_id,
            "payment_success",
            f"充值成功：{plan.name}",
            f"已支付 ¥{order.amount_cents / 100:.2f}，{plan.name}"
            f"（{hours} 小时）有效期至 {sub.expires_at.strftime('%Y-%m-%d %H:%M')}。",
        )
    except Exception:  # noqa: BLE001
        pass
    return sub


# ---------------------------------------------------------------------------
# 订阅查询 / 校验
# ---------------------------------------------------------------------------
def _invalidate_stock(plan_id=None):
    """库存展示缓存失效（付款/退款/回收后调用）。"""
    try:
        from . import portal
        portal.invalidate_stock_cache(plan_id)
    except Exception:  # noqa: BLE001
        pass


def sold_unallocated(plan, global_scope: bool = False) -> int:
    """该套餐「已卖出但还没创建成云手机」的台数。

    库存 = 公共池可用设备数 − sold_unallocated(套餐)，
    这样付款成功的那一刻库存就下降，创建云手机时不再二次扣减。

    global_scope=True 时按**全平台**统计（库存展示用）：公共池是平台的，
    按当前租户统计会把别的企业的已售额度漏掉，导致库存虚高。
    """
    from .models import Subscription
    now = _utcnow()
    q = (db.session.query(Subscription)
         .filter(Subscription.plan_id == plan.id,
                 Subscription.status == "active",
                 Subscription.expires_at > now))
    if global_scope:
        q = q.execution_options(bypass_tenant=True)
    subs = q.all()
    total = 0
    for s in subs:
        total += max(0, int(s.qty or 1) - s._used_devices())
    return total


def active_subscriptions(user_id: int) -> list:
    """当前用户在有效期内的**全部** active 订阅。

    套餐时长是按套餐各自计时的（见 confirm_order_paid），
    所以用户可能同时有多个套餐在有效期内（轻量版还剩 60 天、标准版还剩 10 天…）。
    """
    now = _utcnow()
    return (db.session.query(Subscription)
            .filter(Subscription.user_id == user_id,
                    Subscription.status == "active",
                    Subscription.expires_at > now)
            .order_by(Subscription.expires_at.desc())
            .all())


def _tier_key(s: Subscription):
    """排序键：档位（价格）高的优先，同价再看到期晚的。"""
    return ((s.plan.price_cents if s.plan else 0), s.expires_at)


def active_subscription(user_id: int) -> Subscription | None:
    """当前"主"订阅：有效期内**档位最高**的那个（买了更好的套餐就该显示更好的）。"""
    subs = active_subscriptions(user_id)
    return max(subs, key=_tier_key) if subs else None


def user_subscriptions(user_id: int) -> list:
    return (db.session.query(Subscription)
            .filter(Subscription.user_id == user_id,
                    Subscription.user_deleted.isnot(True))
            .order_by(Subscription.created_at.desc())
            .all())


def has_valid_subscription(user_id: int) -> bool:
    return active_subscription(user_id) is not None


def subscription_status(user_id: int) -> dict:
    """计费状态（前端顶部/开机前展示）：是否有效、剩余天数、到期时间。

    额外返回 active_plans：**每个套餐各自的剩余时长**（套餐之间不叠加）。
    """
    subs = active_subscriptions(user_id)
    if not subs:
        return {"valid": False, "sub_id": None, "plan_name": "",
                "remain_days": 0, "expires_at": None, "active_plans": []}
    sub = max(subs, key=_tier_key)
    plans = [
        {
            "sub_id": s.id,
            "plan_id": s.plan_id,
            "plan_name": s.plan.name if s.plan else "",
            "model": s.plan.model if s.plan else "",
            "remain_days": s.to_dict()["remain_days"],
            "remain_seconds": s.to_dict()["remain_seconds"],
            "started_at": s.to_dict()["started_at"],
            "expires_at": s.to_dict()["expires_at"],
        }
        for s in sorted(subs, key=_tier_key, reverse=True)
    ]
    main = sub.to_dict()
    return {
        "valid": True,
        "sub_id": sub.id,
        "plan_name": sub.plan.name if sub.plan else "",
        "model": sub.plan.model if sub.plan else "",
        "remain_days": main["remain_days"],
        "remain_seconds": main["remain_seconds"],
        "remain_hours": main["remain_hours"],
        "remain_minutes": main["remain_minutes"],
        "started_at": main["started_at"],
        "expires_at": main["expires_at"],
        "active_plans": plans,
    }


def check_device_access(user_id: int) -> dict:
    """开机/建机前的计费拦截。

    返回 {"allowed": True} 或 {"allowed": False, "reason": "...", "status": {...}}。
    """
    st = subscription_status(user_id)
    if st["valid"]:
        return {"allowed": True, "status": st}
    return {
        "allowed": False,
        "reason": "套餐已到期，请先购买/续费套餐后再使用云手机",
        "status": st,
    }


# ---------------------------------------------------------------------------
# 到期提醒（幂等：订阅.reminded 记录已发类型）
# ---------------------------------------------------------------------------
def _notify(user_id: int, ntype: str, title: str, message: str):
    db.session.add(Notification(user_id=user_id, ntype=ntype,
                                title=title, message=message))
    db.session.commit()


def run_expiry_reminders():
    """每天由调度器调用（也可手动触发）：

    - 到期前 3 天：提醒一次（expire_soon）；
    - 到期当天：再提醒一次（recharge_hint，若前一次未提醒也补）；
    - 已过期：置 expired 并断开该用户所有运行中的 redroid 云手机（expired）。
    """
    from . import orchestrator
    now = _utcnow()
    subs = db.session.query(Subscription).filter(
        Subscription.status == "active").all()

    for s in subs:
        if not s.expires_at:
            continue
        days_left = (s.expires_at - now).total_seconds() / 86400.0
        reminded = set((s.reminded or "").split(","))

        # 到期前 3 天（含当天之前）
        if 0 <= days_left <= REMIND_EXPIRE_SOON_DAYS and "expire_soon" not in reminded:
            _notify(s.user_id, "expire_soon", "套餐即将到期",
                    f"您的「{s.plan.name if s.plan else '云手机'}」套餐将于 "
                    f"{s.expires_at:%Y-%m-%d} 到期，剩余 {int(days_left)} 天，请及时续费。")
            reminded.add("expire_soon")

        # 到期当天（剩余 0~1 天内）再提醒一次
        if 0 <= days_left <= 1 and "recharge_hint" not in reminded:
            _notify(s.user_id, "recharge_hint", "套餐今天到期，请立即续费",
                    f"您的「{s.plan.name if s.plan else '云手机'}」套餐今天到期，"
                    "到期后将自动断开云手机并禁止开机，请尽快续费。")
            reminded.add("recharge_hint")

        # 已到期：置 expired，到回收时间点直接归还设备（不做停机/重启）
        if days_left < 0:
            if "expired" not in reminded:
                _notify(s.user_id, "expired", "套餐已到期",
                        f"您的「{s.plan.name if s.plan else '云手机'}」套餐已到期，"
                        "云手机将收回。购买套餐后即可继续使用。")
                reminded.add("expired")
            s.status = "expired"
            # 到期即可用性已经失效（任何接口都要求有效订阅），留着设备只会让库存卡住，
            # 所以直接回收，不再等"次日 0 点"。
            _release_sub_devices(s.id)
            s.reminded = ",".join(sorted(reminded))
            db.session.commit()
            continue

        s.reminded = ",".join(sorted(reminded))
        db.session.commit()

    # 兜底：已过期/已取消但设备还没收回的，一并处理（幂等）
    expire_due_subscriptions(now)


def expire_due_subscriptions(now=None) -> int:
    """把到点的订阅置为 expired 并**立刻**回收设备（幂等，返回处理条数）。

    以前要等到"过期日次日 0 点"才回收，中间这段时间设备既不能用、
    又不计入可售库存——表现就是"套餐到期了库存没变"。
    现在到期即回收，库存立刻恢复。

    调用点：
    - 调度器每 60s（billing_check）
    - 库存计算前（list_plans / 门户商品目录），保证页面看到的是实时值
    """
    from .models import Device, Subscription

    now = now or _utcnow()
    touched = 0

    # a) active 但已过期 -> expired
    overdue = (db.session.query(Subscription)
               .filter(Subscription.status == "active",
                       Subscription.expires_at.isnot(None),
                       Subscription.expires_at <= now)
               .all())
    for s in overdue:
        s.status = "expired"
        touched += 1

    # b) 已经 expired / cancelled，但设备还挂着 -> 收回
    stale = (db.session.query(Subscription)
             .filter(Subscription.status.in_(("expired", "cancelled")))
             .all())
    stale_ids = [s.id for s in stale]
    if stale_ids:
        bound = (db.session.query(Device.id)
                 .filter(Device.sub_id.in_(stale_ids)).count())
        if bound:
            for sid in stale_ids:
                _release_sub_devices(sid)
            touched += 1

    # c) 兜底回收「悬空设备」——两种情况都会让设备一直挂在用户名下、库存回不来：
    #    1) sub_id 指向一条已被物理删除的订阅（历史清空/退款等场景留下的孤儿）；
    #    2) 该用户名下已没有任何生效订阅，但设备还挂着（到期没及时回收的兜底）。
    orphan = release_orphan_devices()
    if orphan:
        touched += 1

    if touched:
        db.session.commit()
        _invalidate_stock()
    return touched


_orphan_last_run = 0.0
_ORPHAN_MIN_INTERVAL = 60  # 秒


def release_orphan_devices(force: bool = False) -> int:
    """回收悬空设备，返回处理台数（幂等）。

    覆盖 `_release_sub_devices` 之外的两类漏网：
    - 设备 sub_id 指向不存在的订阅（订阅行被删后遗留）；
    - 设备归属的用户已无任何生效订阅。
    """
    global _orphan_last_run
    import time as _t
    if not force and (_t.time() - _orphan_last_run) < _ORPHAN_MIN_INTERVAL:
        return 0
    _orphan_last_run = _t.time()

    from sqlalchemy import or_
    from .models import Device, Subscription, User

    now = _utcnow()
    active_user_ids = [u for (u,) in db.session.query(Subscription.user_id)
                       .filter(Subscription.status == "active",
                               Subscription.expires_at.isnot(None),
                               Subscription.expires_at > now).distinct()]
    valid_sub_ids = [i for (i,) in db.session.query(Subscription.id).all()]
    # 平台账号（无企业归属的管理员）名下的设备是公共池/运维设备，不属于"悬空"
    platform_owner_ids = [u.id for u in db.session.query(User)
                          .filter(User.enterprise_id.is_(None),
                                  User.role.in_(("admin", "superadmin"))).all()]

    q = db.session.query(Device).filter(Device.created_by.isnot(None))
    if platform_owner_ids:
        q = q.filter(Device.created_by.notin_(platform_owner_ids))

    cond = []
    if valid_sub_ids:
        cond.append(Device.sub_id.notin_(valid_sub_ids))
    else:
        cond.append(Device.sub_id.isnot(None))
    if active_user_ids:
        cond.append(Device.created_by.notin_(active_user_ids))
    else:
        cond.append(Device.created_by.isnot(None))
    q = q.filter(or_(*cond))

    n = 0
    for d in q.all():
        try:
            wipe_device_data(d)
        except Exception as e:  # noqa: BLE001
            print(f"[billing] 清数据失败（继续回收）{d.serial}: {e}", flush=True)
        d.created_by = None
        d.sub_id = None
        d.enterprise_id = None
        n += 1
    return n


def _plan_period_hours(s):
    """订阅对应套餐的周期小时数（老套餐回退 月×720）。"""
    try:
        p = s.plan
        if p is None:
            return 720
        return int(p.period_hours or 0) or max(1, int(p.duration_months or 1)) * 720
    except Exception:  # noqa: BLE001
        return 720


def _reclaim_at(sub):
    """设备回收时间：小时套餐（<=24h）到期即回收；其它套餐过期日次日 0 点（本地）。"""
    from datetime import timedelta
    hours = _plan_period_hours(sub)
    if hours <= 24:
        return sub.expires_at
    local = sub.expires_at + timedelta(hours=8)
    local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return local_midnight - timedelta(hours=8)


# Android 会在 /sdcard 下自带这些标准目录，清空它们的内容即可，
# 目录本身保留，避免个别应用启动时因为找不到 /sdcard/Android 之类而异常。
_SDCARD_STD_DIRS = ("Alarms", "Android", "Audiobooks", "DCIM", "Documents",
                    "Download", "Movies", "Music", "Notifications", "Pictures",
                    "Podcasts", "Recordings", "Ringtones")


def _sdcard_wipe_script(base: str = "/sdcard") -> str:
    """生成在设备上执行的清理脚本：标准目录清内容，其它文件/目录整个删掉。"""
    loop = " ".join(_SDCARD_STD_DIRS)
    keep = " ".join("! -name %s" % d for d in _SDCARD_STD_DIRS)
    return (
        f"cd {base} && for x in {loop}; do rm -rf \"$x\"/* \"$x\"/.[!.]*; done; "
        f"find . -mindepth 1 -maxdepth 1 {keep} -exec rm -rf {{}} +"
    )


def wipe_device_data(device) -> None:
    """用 adb 清掉该设备上用户装的应用数据和外部存储文件。

    只发 adb 指令：不重启、不删除容器，也不动机器里的系统分区。
    清不掉（设备离线/没连 adb）只记日志，不影响回收流程。
    """
    from flask import current_app
    from . import orchestrator
    serial = str(device.serial or "")
    if device.backend != "redroid" or ":" not in serial:
        return
    keep = {str(current_app.config.get("DEFAULT_LAUNCHER_PKG") or "")}
    keep.discard("")
    try:
        orchestrator.adb_connect(serial)
        rc, out, err = orchestrator._run_adb(
            ["-s", serial, "shell", "pm", "list", "packages", "-3"], timeout=20)
        text = (out or b"").decode("utf-8", "replace") + (err or b"").decode("utf-8", "replace")
        pkgs = [ln.split(":", 1)[1].strip() for ln in text.splitlines()
                if ln.startswith("package:")]
        if not pkgs and rc != 0:
            raise RuntimeError(text.strip()[:120] or "adb 无法读取应用列表")
        wiped = 0
        for pkg in pkgs:
            if pkg in keep:      # 保留桌面启动器，免得下一位用户拿到空白桌面
                continue
            orchestrator._run_adb(["-s", serial, "shell", "pm", "clear", pkg], timeout=30)
            wiped += 1
        orchestrator._run_adb(["-s", serial, "shell", _sdcard_wipe_script()], timeout=60)
        print(f"[billing] 已清理设备 {serial} 的应用数据（{wiped} 个第三方应用）", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[billing] 清理设备 {serial} 失败（不影响回收）: {e}", flush=True)


def release_over_quota(user_id: int) -> int:
    """把用户名下「超出有效套餐额度」的云手机收回公共池，返回收回台数。

    场景：套餐到期/退单后，设备可能还挂在用户名下（sub_id 已失效或为空），
    导致库存永远回不来。这里按「有效订阅 qty 之和」对账：
      - 设备数 <= 额度：不动；
      - 设备数 >  额度：优先收回 sub_id 指向失效订阅或为空的设备。
    """
    from .models import Device, Subscription

    now = _utcnow()
    active = (db.session.query(Subscription)
              .filter(Subscription.user_id == user_id,
                      Subscription.status == "active",
                      Subscription.expires_at.isnot(None),
                      Subscription.expires_at > now)
              .all())
    quota = sum(int(s.qty or 0) for s in active)
    valid_ids = {s.id for s in active}

    owned = (db.session.query(Device)
             .filter(Device.created_by == user_id)
             .order_by(Device.id)
             .all())
    if len(owned) <= quota:
        return 0

    over = len(owned) - quota
    # 优先收「没绑定订阅」或「绑定的订阅已失效」的设备
    def _rank(d):
        if d.sub_id is None:
            return 0
        return 0 if d.sub_id not in valid_ids else 1

    candidates = sorted(owned, key=lambda d: (_rank(d), d.id))
    released = 0
    for d in candidates:
        if released >= over:
            break
        if d.sub_id in valid_ids:
            continue        # 仍在生效的订阅，不能收
        try:
            wipe_device_data(d)
        except Exception as e:  # noqa: BLE001
            print(f"[billing] 超额回收清数据失败 {d.serial}: {e}", flush=True)
        d.created_by = None
        d.sub_id = None
        try:
            d.enterprise_id = None
        except Exception:  # noqa: BLE001
            pass
        released += 1
    if released:
        db.session.commit()
        _invalidate_stock()
    return released


def _release_sub_devices(sub_id: int):
    """订阅到期/退款后把该订阅名下的设备直接收回公共池。

    回收动作：
    1. 用 adb 清掉设备上该用户的应用数据与外部存储（容器不重启、不删除）；
    2. 只清归属（created_by / sub_id），设备保持运行状态，重新计入可售库存。

    除了严格属于该订阅的设备，还会兜底收回「该用户名下没有订阅归属（sub_id 为空）
    且已没有其它有效订阅」的设备——历史数据里有些机器不会写 sub_id，否则退款/到期
    后这些机器会一直挂在用户名下，库存永远回不来。
    """
    from .models import Device, Subscription
    devices = (db.session.query(Device)
               .filter(Device.sub_id == sub_id)
               .all())
    sub = db.session.get(Subscription, sub_id)
    if sub is not None and not has_valid_subscription(sub.user_id):
        seen = {d.id for d in devices}
        loose = (db.session.query(Device)
                 .filter(Device.created_by == sub.user_id,
                         Device.sub_id.is_(None))
                 .all())
        devices += [d for d in loose if d.id not in seen]
    for d in devices:
        # 清数据失败（设备离线/adb 抖动）不能挡住回收：
        # 否则退款/到期后设备会一直挂在用户名下，库存永远回不来。
        try:
            wipe_device_data(d)
        except Exception as e:  # noqa: BLE001
            print(f"[billing] 清数据失败（继续回收）{d.serial}: {e}", flush=True)
        d.created_by = None
        d.sub_id = None
        # 企业名下的设备被回收后必须回到「平台公共池」：
        # 只清 created_by 而留下 enterprise_id，企业管理员仍会在设备管理里看到它，
        # 公共池也拿不到这台设备。
        try:
            d.enterprise_id = None
        except Exception:  # noqa: BLE001
            pass
    db.session.commit()
    return len(devices)


def unread_notifications(user_id: int) -> list:
    rows = (db.session.query(Notification)
            .filter(Notification.user_id == user_id,
                    Notification.is_read.is_(False))
            .order_by(Notification.created_at.desc())
            .limit(20).all())
    return [n.to_dict() for n in rows]


def _notif_query(user_id: int, status: str = "all"):
    """站内消息查询。status: all / unread / read。"""
    q = db.session.query(Notification).filter(Notification.user_id == user_id)
    if status == "unread":
        q = q.filter(Notification.is_read.is_(False))
    elif status == "read":
        q = q.filter(Notification.is_read.is_(True))
    return q


def notification_counts(user_id: int) -> dict:
    """消息数量统计（全部 / 未读 / 已读）。"""
    total = _notif_query(user_id, "all").count()
    unread = _notif_query(user_id, "unread").count()
    return {"total": total, "unread": unread, "read": total - unread}


def user_notifications(user_id: int, status: str = "all",
                       limit: int = 50, offset: int = 0) -> dict:
    """全部站内消息（已读 + 未读），供手机端消息页使用。

    排序：未读在前，同组内按时间倒序；末尾用 id 兜底，保证分页稳定不重复。
    返回 {items, total, unread, read, has_more}。
    """
    if status not in ("all", "unread", "read"):
        status = "all"
    # 上限 200 是有意的防滥用钳制，前端传更大也只给 200 条
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    q = _notif_query(user_id, status)
    # is_read 是布尔，升序 => False（未读）排前面
    rows = (q.order_by(Notification.is_read.asc(),
                       Notification.created_at.desc(),
                       Notification.id.desc())
            .offset(offset).limit(limit).all())
    counts = notification_counts(user_id)
    if status == "unread":
        scope_total = counts["unread"]
    elif status == "read":
        scope_total = counts["read"]
    else:
        scope_total = counts["total"]
    return {
        "items": [n.to_dict() for n in rows],
        "total": counts["total"],
        "unread": counts["unread"],
        "read": counts["read"],
        "has_more": offset + len(rows) < scope_total,
    }


def mark_notifications_read(user_id: int):
    db.session.query(Notification).filter(
        Notification.user_id == user_id,
        Notification.is_read.is_(False)).update({"is_read": True})
    db.session.commit()
