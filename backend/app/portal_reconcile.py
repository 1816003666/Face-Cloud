# -*- coding: utf-8 -*-
"""门户对账上报（GET/POST /api/internal/reconciliation/report）。

契约（门户侧对接说明 §5 第 5 项）：
- 门户每天 04:10 对前一日交易对账（门户流水 vs 云手机侧上报的账单）；
- 本接口供门户拉取云手机侧「指定业务日」的开通账单明细，由门户与自身流水比对；
- 未启用（PORTAL_RECONCIL_ENABLED=false）时返回 5001，不参与对账。

响应（统一信封）：
    {"code":0,"message":"ok","data":{
        "date":"2026-10-15",
        "items":[{"order_item_id":..,"portal_user_id":..,"sku_id":..,
                  "quantity":..,"period_hours":..,"status":..,
                  "instance_id":..,"amount_cents":..,"created_at":..}]}}
"""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import current_app

from .extensions import db
from .models import Plan, PortalProvision

ERR_PARAM = 1001
ERR_DISABLED = 5001


def _utcnow():
    return datetime.utcnow()


def _iso(dt):
    return (dt.isoformat() + "Z") if dt else None


def _fail(code, msg):
    return {"code": code, "message": msg, "data": None}, 200


def _parse_day(date_str):
    """解析 YYYY-MM-DD；缺省取 UTC 昨天（门户 04:10 对前一日账）。"""
    if date_str:
        try:
            return datetime.strptime(str(date_str).strip(), "%Y-%m-%d")
        except ValueError:
            return None
    now = _utcnow()
    return datetime(now.year, now.month, now.day) - timedelta(days=1)


def handle_reconcile(payload):
    if not current_app.config.get("PORTAL_RECONCIL_ENABLED", False):
        return _fail(ERR_DISABLED, "对账上报未启用（PORTAL_RECONCIL_ENABLED=false）")

    payload = payload or {}
    day = _parse_day(payload.get("date"))
    if day is None:
        return _fail(ERR_PARAM, "date 格式应为 YYYY-MM-DD")

    start = day
    end = day + timedelta(days=1)

    rows = (db.session.query(PortalProvision)
            .filter(PortalProvision.created_at >= start,
                    PortalProvision.created_at < end)
            .order_by(PortalProvision.created_at)
            .all())

    # sku -> plan.price_cents；账单金额 = 单价 × 份数
    plans = {}
    for p in db.session.query(Plan).filter(Plan.sku_id.isnot(None)).all():
        if p.sku_id:
            plans[p.sku_id] = p

    items = []
    for r in rows:
        plan = plans.get(r.sku_id)
        unit = int(plan.price_cents or 0) if plan else 0
        qty = int(r.quantity or 1)
        items.append({
            "order_item_id": r.order_item_id,
            "portal_user_id": r.portal_user_id,
            "sku_id": r.sku_id,
            "quantity": qty,
            "period_hours": int(r.period_hours or 0),
            "status": r.status,
            "instance_id": r.instance_id or "",
            "amount_cents": unit * qty,
            "created_at": _iso(r.created_at),
        })

    return {"code": 0, "message": "ok", "data": {
        "date": day.strftime("%Y-%m-%d"),
        "items": items,
    }}, 200
