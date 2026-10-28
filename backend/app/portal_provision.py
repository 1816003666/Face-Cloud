# -*- coding: utf-8 -*-
"""门户开通（POST /api/internal/provision）—— 幂等状态机。

契约要点（门户侧对接说明 §2.2）：
- 幂等键 order_item_id（string）必须唯一索引；重复调用返回首次结果，不重复建实例；
- 先落库（pending）→ 条件更新抢占（granting）→ success/failed，进程被杀后可靠对齐；
- 30 秒内返回。建 redroid 容器是慢操作，因此"先占位返回成功 + 后台补建"，
  与实施说明 §3.2 允许的异步开通一致；
- 失败码：4003 sku 无效/下架、4004 库存不足、5001 内部错误。
  门户按 4003/4004 直接退款（不重试），其它 code 走系统失败重试。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import or_, text

from .extensions import db
from .models import (Device, DeviceLog, Plan, PortalProvision,
                     PortalUserMap, Subscription, User)

ERR_PARAM = 1001
ERR_SKU = 4003
ERR_STOCK = 4004
ERR_INTERNAL = 5001


class BizError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _cfg(name, default=None):
    return current_app.config.get(name, default)


def _now():
    return datetime.utcnow()


def _iso_z(dt):
    return (dt.isoformat() + "Z") if dt else None


def _ok(p, device_count: int | None = None):
    """返回给门户。quantity = 份数；device_count = 台数（= 份数 × 每份台数）。"""
    units = int(p.quantity or 1)
    devices = int(device_count) if device_count is not None else units
    return {"code": 0, "message": "ok",
            "data": {"instance_id": p.instance_id,
                     "expire_at": _iso_z(p.expire_at),
                     "quantity": units,          # 份数（保持原有语义）
                     "units": units,
                     "device_count": devices}}, 200


def _fail(code, msg):
    return {"code": code, "message": msg, "data": None}, 200


def resolve_or_create_shadow(portal_user_id, portal_username=None):
    """门户 UUID → 本地影子用户：惰性建档，只按 UUID 匹配，绝不按用户名。

    portal_username 是门户侧的用户名/昵称，只用于界面展示（display_name）；
    登录名仍是 portal_<UUID>，避免和本地账号重名互相顶掉。
    """
    pid = (portal_user_id or "").strip()
    if not pid:
        return None
    nice = (portal_username or "").strip()[:64] or None

    row = db.session.get(PortalUserMap, pid)
    if row is not None:
        u = db.session.get(User, row.local_user_id)
        if u is not None:
            if nice and (u.display_name or "") != nice:
                u.display_name = nice
                db.session.commit()
            return u
        # 映射还在、账号被删了（清库/迁移后常见）。以前这里会继续往下建号，
        # 然后栽在 portal_user_map 主键冲突上 → 门户侧只看到「影子用户创建失败」，
        # 每一单都失败并自动退款。现在先把失效映射删掉，再重建账号。
        try:
            db.session.delete(row)
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
    prefix = str(_cfg("PORTAL_SHADOW_PREFIX", "portal_"))
    uname = (prefix + pid)[:64]
    u = User(username=uname,
             display_name=nice or ("门户用户" + pid[:6]),
             hashed_password=str(_cfg("PORTAL_SHADOW_PASSWORD_HASH",
                                      "!portal-shadow-no-local-login")),
             role=str(_cfg("PORTAL_SHADOW_ROLE", "operator")),
             is_active=True)
    db.session.add(u)
    try:
        db.session.flush()
        # SQLite 会复用被删用户的 id；清掉该 id 上的孤儿订阅/通知，
        # 否则新影子用户会"继承"到别人遗留的套餐（越权拿到有效订阅）。
        try:
            db.session.execute(text("DELETE FROM subscriptions WHERE user_id=:u"), {"u": u.id})
            db.session.execute(text("DELETE FROM notifications WHERE user_id=:u"), {"u": u.id})
        except Exception:  # noqa: BLE001
            db.session.rollback()
        db.session.add(PortalUserMap(portal_user_id=pid, local_user_id=u.id))
        db.session.commit()
        return u
    except Exception as e:  # noqa: BLE001 - 并发唯一键冲突 / 映射残留
        db.session.rollback()
        # 1) 映射已存在：只在账号还活着时复用，否则删掉残留映射重试一次
        row = db.session.get(PortalUserMap, pid)
        if row is not None:
            u = db.session.get(User, row.local_user_id)
            if u is not None:
                return u
            try:
                db.session.delete(row)
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
        # 2) 同名影子账号已存在（上次建号中途失败留下的）：直接接管
        u2 = db.session.query(User).filter_by(username=uname).first()
        if u2 is not None:
            try:
                db.session.add(PortalUserMap(portal_user_id=pid, local_user_id=u2.id))
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
            return u2
        print(f"[portal] 影子用户建号失败 pid={pid}: {e}", flush=True)
        return None


def _preflight_backend(backend):
    """开通前自检：建机环境没就绪就明确失败，绝不"返回成功但没建成"。"""
    from . import orchestrator
    if backend != "redroid":
        return
    if not orchestrator._ssh_enabled():
        raise BizError(ERR_INTERNAL, "云手机创建环境未就绪：SSH 未配置")
    image = str(_cfg("REDROID_IMAGE", "") or "")
    if not image:
        return
    client = None
    try:
        client = orchestrator._ssh_connect()
        _rc, out, _err = orchestrator._ssh_exec(
            client, "docker image inspect %s >/dev/null 2>&1 && echo YES || echo NO" % image)
        if "YES" not in (out or ""):
            raise BizError(ERR_INTERNAL,
                           "云手机镜像不存在：%s（请在目标机 docker pull，或改 REDROID_IMAGE）" % image)
    except BizError:
        raise
    except Exception:  # noqa: BLE001 - 探测本身失败不拦
        pass
    finally:
        try:
            if client is not None:
                client.close()
        except Exception:  # noqa: BLE001
            pass


def _devices_per_unit(plan):
    """一个「单元」含几台云手机 —— 只认套餐自己写的台数。

    历史包袱：这里曾经先读全局配置 PORTAL_DEVICES_PER_UNIT（默认 1），
    只要那个变量丢了或被设错，所有套餐的台数都会被算成同一个值，
    一次就能差 10 倍（旗舰 ¥99 → ¥990，或畅玩小时 ¥0.5 → ¥0.05）。
    现在固定按「套餐 specs.device_count → plans.device_count 列 → 1」取值，
    全局开关已废弃（即使 .env 里还留着也不会再生效）。
    """
    specs = {}
    try:
        specs = json.loads(plan.specs or "{}")
    except (TypeError, ValueError):
        specs = {}
    if not isinstance(specs, dict):
        specs = {}

    for candidate in (specs.get("device_count"), getattr(plan, "device_count", None)):
        try:
            n = int(candidate)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return 1



_SPEC_FIELDS = (
    ("resolution", "spec_resolution"),
    ("cpu", "spec_cpu"),
    ("memory", "spec_memory"),
    ("storage", "spec_storage"),
    ("android", "spec_android"),
)


def _spec_ok(device, specs):
    """按套餐 specs 匹配设备配置：任一方为空视为不限制。"""
    if not isinstance(specs, dict):
        return True
    for key, attr in _SPEC_FIELDS:
        want = str(specs.get(key) or "").strip().lower()
        have = str(getattr(device, attr, "") or "").strip().lower()
        if want and have and want != have:
            return False
    return True


def _portal_user_ids():
    return [r[0] for r in db.session.query(PortalUserMap.local_user_id).all()]


def _plan_alloc_ips(plan):
    """套餐指定的可分配服务器 IP 列表（空=按默认池/1080 规则）。"""
    if plan is None:
        return []
    try:
        ips = json.loads(plan.alloc_ips or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(ips, list):
        return []
    return [str(x).strip() for x in ips if str(x).strip()]


def _available_query(specs=None, plan=None):
    """可分配设备：运行中、未被任何门户用户占用（管理员名下也算可用）。

    默认使用**全部服务器**；只有套餐显式配置了 alloc_ips 时才限定服务器。
    """
    q = db.session.query(Device).filter(
        Device.status == "running",
        # 绑着订阅的设备一律不算可售（到期/退款后 _release_sub_devices 会把它解绑）
        Device.sub_id.is_(None),
    )
    backend = str(_cfg("DEVICE_BACKEND", "redroid") or "")
    if backend:
        q = q.filter(Device.backend == backend)
    # 只把「未分配 / 管理员名下」的设备视为可分配；
    # 门户影子用户和本平台普通用户（viewer/operator）占用的设备都不动。
    from .models import User
    non_admin = db.session.query(User.id).filter(~User.role.in_(("admin", "superadmin")))
    q = q.filter(or_(Device.created_by.is_(None), ~Device.created_by.in_(non_admin)))
    alloc = _plan_alloc_ips(plan)
    if alloc:
        q = q.filter(Device.ip.in_(alloc))
    return q


def _pick_devices(specs, count, plan=None):
    """按配置匹配、按 id 顺序取 count 台可用设备。"""
    out = []
    for d in _available_query(specs, plan).order_by(Device.id).all():
        if _spec_ok(d, specs):
            out.append(d)
            if len(out) >= count:
                break
    return out



def _remaining_stock(specs=None, plan=None):
    """可分配余量。PORTAL_STOCK_LIMIT <= 0 表示不封顶（与 portal._stock 一致）。"""
    try:
        limit = int(_cfg("PORTAL_STOCK_LIMIT", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    try:
        available = _available_query(specs, plan).count()
    except Exception:  # noqa: BLE001
        available = 0
    available = max(0, int(available))
    return available if limit <= 0 else min(limit, available)


def _grant_subscription(user, plan, hours, qty_add: int = 0):
    """建/续订阅（不 commit，由调用方统一提交）。返回到期时间（UTC naive）。

    qty_add：本次新增的可创建云手机台数（开通时=套餐台数；续费时传 0 只延期）。
    """
    now = _now()
    sub = (db.session.query(Subscription)
           .filter_by(user_id=user.id, plan_id=plan.id, status="active")
           .order_by(Subscription.expires_at.desc()).first())
    if sub is not None and sub.expires_at and sub.expires_at > now:
        sub.expires_at = sub.expires_at + timedelta(hours=hours)
        if qty_add > 0:
            sub.qty = (sub.qty or 1) + qty_add
        if not sub.started_at:
            sub.started_at = now
    else:
        sub = Subscription(user_id=user.id, plan_id=plan.id, status="active",
                           started_at=now, expires_at=now + timedelta(hours=hours),
                           qty=max(1, qty_add))
        db.session.add(sub)
    db.session.flush()
    return sub.expires_at


def _build_devices_async(device_ids, backend, app):
    """后台补建容器：先返回成功，再逐台建；失败置 Device.status=error。"""
    def worker():
        with app.app_context():
            from . import orchestrator
            for did in device_ids:
                d = db.session.get(Device, did)
                if d is None or d.status != "creating":
                    continue
                err = ""
                try:
                    st = orchestrator.create_device(backend, d.name) or {}
                    if st.get("serial"):
                        d.serial = st.get("serial", "")
                        d.status = st.get("status") or "running"
                        d.ip = st.get("ip", "") or d.ip
                        d.fingerprint = st.get("fingerprint", "") or d.fingerprint
                    else:
                        d.status = "error"
                        err = str(st.get("error") or "创建失败（无 serial）")
                except Exception as e:  # noqa: BLE001
                    db.session.rollback()
                    d = db.session.get(Device, did)
                    err = str(e)
                    if d is not None:
                        d.status = "error"
                if err and d is not None:
                    try:
                        db.session.add(DeviceLog(device_id=d.id, level="error",
                                                 message="门户开通建机失败：" + err[:400]))
                    except Exception:  # noqa: BLE001
                        db.session.rollback()
                db.session.commit()
    threading.Thread(target=worker, daemon=True).start()


def handle_provision(payload):
    payload = payload or {}
    order_item_id = str(payload.get("order_item_id") or "").strip()
    portal_user_id = str(payload.get("portal_user_id") or "").strip()
    sku_id = str(payload.get("sku_id") or "").strip()
    if not order_item_id or not portal_user_id or not sku_id:
        return _fail(ERR_PARAM, "order_item_id / portal_user_id / sku_id 必填")
    try:
        quantity = int(payload.get("quantity") or 1)
    except (TypeError, ValueError):
        return _fail(ERR_PARAM, "quantity 非法")
    max_qty = int(_cfg("PORTAL_MAX_QTY_PER_ORDER", 3) or 3)
    if quantity < 1 or quantity > max_qty:
        return _fail(ERR_PARAM, "quantity 需在 1..%d" % max_qty)
    raw_hours = payload.get("period_hours")
    raw_days = payload.get("period_days")
    period_hours = None
    try:
        if raw_hours is not None and str(raw_hours).strip() != "":
            period_hours = int(raw_hours)
        elif raw_days is not None and str(raw_days).strip() != "":
            period_hours = int(raw_days) * 24  # 兼容旧字段：天 → 小时
            current_app.logger.warning(
                "[portal] 开通入参只带 period_days=%s，按 ×24 兜底为 %s 小时",
                raw_days, period_hours)
    except (TypeError, ValueError):
        return _fail(ERR_PARAM, "period_hours / period_days 非法")

    # 1) 幂等：已完成直接返回首次结果
    p = db.session.query(PortalProvision).filter_by(order_item_id=order_item_id).first()
    if p is not None and p.status == "success":
        return _ok(p)

    plan = (db.session.query(Plan)
            .filter(Plan.active.is_(True), Plan.sku_id == sku_id).first())
    if plan is None:
        return _fail(ERR_SKU, "商品不存在或已下架")

    hours = period_hours or int(plan.period_hours or 0) or max(1, int(plan.duration_months or 1)) * 30 * 24

    # 2) 先落库（唯一索引兜住并发）
    if p is None:
        p = PortalProvision(order_item_id=order_item_id, portal_user_id=portal_user_id,
                            sku_id=sku_id, quantity=quantity, period_hours=hours,
                            status="pending")
        db.session.add(p)
        try:
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
            p = db.session.query(PortalProvision).filter_by(order_item_id=order_item_id).first()
            if p is None:
                return _fail(ERR_INTERNAL, "开通记录写入失败")
            if p.status == "success":
                return _ok(p)

    # 3) 抢占 pending → granting（条件更新，防并发重复建机）
    claimed = (db.session.query(PortalProvision)
               .filter(PortalProvision.order_item_id == order_item_id,
                       PortalProvision.status.in_(("pending", "failed")))
               .update({"status": "granting", "updated_at": _now()},
                       synchronize_session=False))
    db.session.commit()
    if not claimed:
        p = db.session.query(PortalProvision).filter_by(order_item_id=order_item_id).first()
        if p is not None and p.status == "success":
            return _ok(p)
        return _fail(ERR_INTERNAL, "开通处理中，请用同一 order_item_id 重试")

    try:
        user = resolve_or_create_shadow(portal_user_id)
        if user is None:
            raise BizError(ERR_INTERNAL, "影子用户创建失败")

        # 续费：同一影子用户 + 同一 sku 已有成功开通 → 只延期，复用原 instance_id
        prior = (db.session.query(PortalProvision)
                 .filter(PortalProvision.portal_user_id == portal_user_id,
                         PortalProvision.sku_id == sku_id,
                         PortalProvision.status == "success",
                         PortalProvision.id != p.id)
                 .order_by(PortalProvision.id).first())
        if prior is not None and prior.instance_id:
            p.expire_at = _grant_subscription(user, plan, hours)
            p.instance_id = prior.instance_id
            p.device_ids = "[]"
            p.status = "success"
            db.session.commit()
            current_app.logger.info(
                "[portal] 续费 order_item=%s sku=%s 延 %sh", order_item_id, sku_id, hours)
            return _ok(p, device_count=_devices_per_unit(plan))

        # 首次开通：只给额度，不预分配设备。
        # 门户用户之后登录云手机平台，自己点「创建」时再从公共池分配（见 routes/devices.py）。
        per_unit = _devices_per_unit(plan)
        count = quantity * per_unit
        cap = int(_cfg("PORTAL_MAX_DEVICES_PER_PROVISION", 200) or 200)
        if count > cap:
            raise BizError(ERR_STOCK, "单次开通台数超限（%d > %d 台），请拆成多单" % (count, cap))

        # 库存校验：额度给出去之前先看池子里还有没有货，避免"卖出去却建不出机器"。
        try:
            import json as _json
            specs = _json.loads(plan.specs or "{}")
        except (TypeError, ValueError):
            specs = {}
        try:
            avail = _remaining_stock(specs, plan)
        except Exception:  # noqa: BLE001
            avail = None
        if avail is not None and count > avail:
            raise BizError(ERR_STOCK,
                           "库存不足：需要 %d 台，当前可分配 %d 台" % (count, avail))

        current_app.logger.info(
            "[portal] 开通 order_item=%s user=%s sku=%s 份数=%s 台数=%s 周期=%sh",
            order_item_id, portal_user_id, sku_id, quantity, count, hours)

        p.expire_at = _grant_subscription(user, plan, hours, qty_add=count)
        p.instance_id = "ph-%06d" % p.id
        p.device_ids = "[]"
        p.status = "success"
        db.session.commit()
        return _ok(p, device_count=count)
    except BizError as e:
        db.session.rollback()
        current_app.logger.warning(
            "[portal] 开通失败 order_item=%s sku=%s code=%s msg=%s",
            order_item_id, sku_id, e.code, e.msg)
        p = db.session.query(PortalProvision).filter_by(order_item_id=order_item_id).first()
        if p is not None:
            p.status = "failed"
            p.error_code = e.code
            p.error_msg = str(e.msg)[:255]
            db.session.commit()
        return _fail(e.code, e.msg)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        p = db.session.query(PortalProvision).filter_by(order_item_id=order_item_id).first()
        if p is not None:
            p.status = "failed"
            p.error_code = ERR_INTERNAL
            p.error_msg = str(e)[:255]
            db.session.commit()
        return _fail(ERR_INTERNAL, "内部错误：%s" % str(e)[:120])
