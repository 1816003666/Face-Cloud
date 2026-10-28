# -*- coding: utf-8 -*-
"""统一门户接入（产品侧）。

本模块负责两件事：
1. 内网鉴权：/api/internal/* 只认 X-Internal-Key，定长比较、fail-closed；
2. 商品目录：把 plans 表转成门户契约要求的 {code, data:{products:[...]}}。

契约要点：
- 门户每 10 分钟拉一次 /internal/products，10 秒内必须返回；
- price_cents 单位是分；billing_mode 1=包周期 2=按量；period_hours 包周期必填（单位=小时，1天=24）；
- 内网鉴权失败返回 HTTP 401 + {code:40001}。
"""
from __future__ import annotations

import hmac
import json
import time as _time
import urllib.error
import urllib.request
from functools import wraps

from flask import request, jsonify, g, current_app

INTERNAL_KEY_HEADER = "X-Internal-Key"

# 套餐库存抖动保护：plan_id -> (时间戳, 最近一次有效库存)
_STOCK_CACHE: dict = {}
_STOCK_CACHE_TTL = 600  # 10 分钟（抖动兜底）
_STOCK_CACHE_SHORT = 5   # 5 秒（同窗口内统一数值）


def _unauthorized(msg="无效的内部调用凭证"):
    """内网鉴权失败统一响应（契约 §3.3）。"""
    return jsonify(code=40001, message=msg, data=None), 401


def client_ip() -> str:
    """反代后真实来源 IP。nginx 会带 X-Real-IP / X-Forwarded-For。"""
    xri = (request.headers.get("X-Real-IP") or "").strip()
    if xri:
        return xri
    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return (request.remote_addr or "").strip()


def allowed_ips() -> list:
    raw = current_app.config.get("PORTAL_ALLOWED_IPS") or ""
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def require_internal_key(f):
    """内网鉴权装饰器。未配置密钥时 fail-closed（一律拒绝）。"""

    @wraps(f)
    def wrapper(*args, **kwargs):
        key = str(current_app.config.get("PORTAL_INTERNAL_KEY") or "")
        if not key:
            return _unauthorized("内网密钥未配置")
        got = request.headers.get(INTERNAL_KEY_HEADER, "")
        if not hmac.compare_digest(str(got), key):
            return _unauthorized()
        allow = allowed_ips()
        ip = client_ip()
        if allow and ip not in allow:
            return jsonify(code=40001, message="来源 IP 未授权", data=None), 403
        # 密钥归属绑定：控制器一律用这个身份，绝不用请求体自报的 product_code
        g.portal_product = str(current_app.config.get("PORTAL_PRODUCT_CODE") or "phone")
        return f(*args, **kwargs)

    return wrapper


# ----------------------------- 商品目录 -----------------------------
def _json_list(raw):
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)]
    if isinstance(v, list):
        return v
    return [v]


def _period_hours(plan) -> int:
    if plan.period_hours:
        return int(plan.period_hours)
    return max(1, int(plan.duration_months or 1)) * 30 * 24


def _spec(plan) -> dict:
    try:
        specs = json.loads(plan.specs or "{}")
    except (TypeError, ValueError):
        specs = {}
    if not isinstance(specs, dict):
        specs = {}
    spec = {}
    highlights = _json_list(plan.highlights)
    details = _json_list(plan.details)
    if highlights:
        spec["highlights"] = highlights
    if details:
        spec["details"] = details
    if plan.badge:
        spec["badge"] = plan.badge
    # 业务字段平铺（分辨率/CPU/内存/存储/安卓/含机台数…）
    for k, v in specs.items():
        spec.setdefault(k, v)
    try:
        spec.setdefault("device_count", int(specs.get("device_count") or 1))
    except (TypeError, ValueError):
        spec.setdefault("device_count", 1)
    return spec


def invalidate_stock_cache(plan_id=None):
    """付款/退款/回收后立刻失效库存缓存，保证库存即时变化。"""
    if plan_id is None:
        _STOCK_CACHE.clear()
    else:
        _STOCK_CACHE.pop(int(plan_id), None)


def _stock(limit, specs=None, plan=None) -> int:
    """门户可售余量：按套餐规格/指定服务器过滤后的可用设备数，封顶 limit。"""
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        return 0
    # 先把到期的订阅结算掉，否则页面会看到"套餐到期了库存还没变"（调度器最多滞后 60s）
    try:
        from .billing import expire_due_subscriptions
        expire_due_subscriptions()
    except Exception:  # noqa: BLE001 - 结算失败不能影响目录展示
        pass
    try:
        from .portal_provision import _available_query
        # 公共池是平台的：必须绕过企业数据隔离，否则别的企业的账号
        # 只能看到自己企业名下的设备 → 套餐库存算成 0（"又售罄了"）
        avail = _available_query(specs, plan) \
            .execution_options(bypass_tenant=True).count()
    except Exception:  # noqa: BLE001
        avail = 0
    # 已售出但还没创建成云手机的额度，也要从库存里扣掉：
    # 用户付款成功那一刻库存就该下降，而不是等他在设备管理里点「创建」才变。
    # 同样是全局口径（跨企业），否则企业账号看到的库存会被其他企业的销量影响成负数/0。
    if plan is not None:
        try:
            from .billing import sold_unallocated
            avail -= sold_unallocated(plan, global_scope=True)
        except Exception:  # noqa: BLE001
            pass
    # lim <= 0 表示「不封顶」：门户目录用它显示与平台一致的实时库存
    raw = max(0, int(avail))
    avail = raw if lim <= 0 else min(lim, raw)

    # 短期缓存（5s）：同一时刻不同账号/多次刷新看到同一个数字，
    # 避免"订阅刚好到期"这种瞬时状态变化让两个人看到不同库存。
    # 注意缓存里存的是**未封顶**的原始值：/api/plans 用 99999 查过一次之后，
    # 门户目录（limit=60）不能因此拿到 82 这种超过自己上限的数字。
    key = plan.id if plan is not None else None
    now_ts = _time.time()
    cached = _STOCK_CACHE.get(key)
    if cached and (now_ts - cached[0]) <= _STOCK_CACHE_SHORT and cached[1] > 0:
        hit = int(cached[1])
        return hit if lim <= 0 else min(lim, hit)
    if raw > 0:
        _STOCK_CACHE[key] = (now_ts, raw)
    if avail > 0:
        return avail

    # 抖动保护：后端重启/设备状态刷新期间，running 会短暂归零，
    # 导致整页套餐瞬间变成「已售罄」。这里用最近一次有效值兜底（10 分钟内），
    # 但只在"库里确实一台 running 都没有"时启用——真卖光了不会走这个分支。
    try:
        from .extensions import db
        from .models import Device
        running_total = db.session.query(Device).filter(Device.status == "running").count()
    except Exception:  # noqa: BLE001
        running_total = 1
    if running_total == 0 and cached and (now_ts - cached[0]) <= _STOCK_CACHE_TTL:
        return int(cached[1])
    return avail


def _query_plans():
    from .extensions import db
    from .models import Plan
    return (db.session.query(Plan)
            .filter(Plan.active.is_(True))
            .filter(Plan.sku_id.isnot(None), Plan.sku_id != "")
            .order_by(Plan.sort, Plan.id).all())


def build_products() -> list:
    """把 plans 表转成门户商品目录（只含 active 且已配 sku_id 的套餐）。"""
    limit = current_app.config.get("PORTAL_STOCK_LIMIT", 60)
    out = []
    for p in _query_plans():
        try:
            _ps = json.loads(p.specs or "{}")
        except (TypeError, ValueError):
            _ps = {}
        _ph = _period_hours(p)
        try:
            per_unit = int(_ps.get("device_count") or 1)
        except (TypeError, ValueError):
            per_unit = 1
        out.append({
            "sku_id": p.sku_id,
            "name": p.name,
            "spec": _spec(p),
            "price_cents": int(p.price_cents or 0),
            # 平铺出来，门户不用去 spec 里挖（之前门户只拿到 spec.device_count，容易显示成 1 台）
            "price_yuan": round(int(p.price_cents or 0) / 100, 2),
            "device_count": per_unit,
            "period_label": (f"{_ph // 24} 天" if _ph % 24 == 0 else f"{_ph} 小时"),
            "units_label": (f"1 份 = {_ph // 24} 天 · {per_unit} 台"
                            if _ph % 24 == 0 else f"1 份 = {_ph} 小时 · {per_unit} 台"),
            "billing_mode": int(p.billing_mode or 1),
            "period_hours": _ph,
            # 兼容旧门户：同时给出"天"（向上取整），旧代码按 *24 也能工作
            "period_days": max(1, (_ph + 23) // 24),
            "stock": _stock(limit, _ps, p),
        })
    return out


# ----------------------------- 代理登录（路线 A） -----------------------------
def portal_login(username, password):
    """调用统一门户 /api/auth/login 验证门户用户。

    返回：
      None                              -> 未配置门户（不回落）
      {"ok": True, "user_id","username"} -> 门户验证通过
      {"ok": False, "error": "invalid" | "disabled" | "unavailable" | "param"}
    """
    base = str(current_app.config.get("PORTAL_BASE_URL") or "").strip().rstrip("/")
    if not base:
        return None
    p = str(current_app.config.get("PORTAL_LOGIN_PATH") or "/api/auth/login").strip()
    if not p.startswith("/"):
        p = "/" + p
    url = base + p
    try:
        timeout = max(1.0, float(current_app.config.get("PORTAL_LOGIN_TIMEOUT_MS", 5000)) / 1000.0)
    except (TypeError, ValueError):
        timeout = 5.0
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 网络/超时/5xx 一律按"门户不可用"
        return {"ok": False, "error": "unavailable"}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {"ok": False, "error": "unavailable"}
    code = data.get("code")
    payload = data.get("data") or {}
    if code == 0:
        u = payload.get("user") or {}
        return {"ok": True,
                "user_id": str(u.get("id") or ""),
                "username": str(u.get("username") or username)}
    if code == 2003:
        return {"ok": False, "error": "invalid"}
    if code == 2004:
        return {"ok": False, "error": "disabled"}
    if code == 1001:
        return {"ok": False, "error": "param"}
    return {"ok": False, "error": "unavailable"}

