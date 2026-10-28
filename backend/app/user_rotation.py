# -*- coding: utf-8 -*-
"""普通用户的轮次运行（每人一套配置）。

一轮的动作 = 把该用户名下最多 devices_per_round 台云手机
「清用户数据 → 收回公共池 → 重新分配一批给他」，容器不销毁、不重启。
管理员用的是全局 RotationConfig（销毁重建容器），两者互不影响。
"""
from __future__ import annotations

import json
from datetime import timedelta

from .extensions import db
from .models import Device, Subscription, UserRotation, _utcnow


def get_config(user_id: int) -> UserRotation:
    row = db.session.query(UserRotation).filter_by(user_id=user_id).first()
    if row is None:
        row = UserRotation(user_id=user_id, enabled=False, rounds=4,
                           devices_per_round=2, round_index=1)
        db.session.add(row)
        db.session.commit()
    return row


def config_dict(cfg: UserRotation, user_id: int) -> dict:
    rounds = max(1, int(cfg.rounds or 1))
    return {
        "enabled": bool(cfg.enabled),
        "rounds": rounds,
        "devices_per_round": int(cfg.devices_per_round or 1),
        "round_index": int(cfg.round_index or 1),
        "per_round_hours": round(24 / rounds, 2),
        "started_at": cfg.started_at.isoformat() if cfg.started_at else None,
        "next_round_at": cfg.next_round_at.isoformat() if cfg.next_round_at else None,
        "scope": "user",
    }


def _active_sub(user_id: int):
    now = _utcnow()
    return (db.session.query(Subscription)
            .filter_by(user_id=user_id, status="active")
            .filter(Subscription.expires_at > now)
            .order_by(Subscription.expires_at.desc())
            .first())


def _plan_specs(sub):
    if sub is None or sub.plan is None:
        return {}
    try:
        specs = json.loads(sub.plan.specs or "{}")
        return specs if isinstance(specs, dict) else {}
    except (TypeError, ValueError):
        return {}


def run_round(user_id: int, count: int | None = None) -> dict:
    """执行一轮：清数据 → 收回 → 重新分配。返回统计。"""
    from .billing import wipe_device_data
    from .portal_provision import _pick_devices

    cfg = get_config(user_id)
    want = max(1, int(count or cfg.devices_per_round or 1))
    sub = _active_sub(user_id)
    plan = sub.plan if sub is not None else None
    specs = _plan_specs(sub)

    mine = (db.session.query(Device).filter_by(created_by=user_id)
            .order_by(Device.id).limit(want).all())
    released_ids = set()
    for d in mine:
        wipe_device_data(d)
        d.created_by = None
        d.sub_id = None
        released_ids.add(d.id)
    db.session.commit()

    # 优先分配"不是刚收回的那批"，池子不够时再拿回来
    picked = []
    if want:
        candidates = _pick_devices(specs, want * 3 + 5, plan)
        picked = [d for d in candidates if d.id not in released_ids][:want]
        if len(picked) < want:
            need = want - len(picked)
            extra = [d for d in candidates
                     if d.id in released_ids and d not in picked][:need]
            picked.extend(extra)
    for d in picked:
        d.created_by = user_id
        d.sub_id = sub.id if sub is not None else None
    db.session.commit()

    return {"released": len(released_ids), "allocated": len(picked)}


def schedule_next(cfg: UserRotation) -> None:
    """当前轮结束后排下一轮（不 commit，由调用方统一提交）。"""
    rounds = max(1, int(cfg.rounds or 1))
    cfg.round_index = ((int(cfg.round_index or 1)) % rounds) + 1
    cfg.next_round_at = _utcnow() + timedelta(hours=24 / rounds)


def run_round_async(app, user_id: int, count: int | None = None) -> None:
    """后台线程里跑一轮：必须自己建应用上下文，否则 db.session 不可用。"""
    import threading

    def _worker():
        with app.app_context():
            try:
                result = run_round(user_id, count)
                print(f"[rotation] 用户 {user_id} 立即轮转: {result}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[rotation] 用户 {user_id} 轮转失败: {e}", flush=True)

    threading.Thread(target=_worker, daemon=True).start()


def tick(app) -> None:
    """调度器每 60 秒调用：到点的用户各跑一轮。"""
    with app.app_context():
        now = _utcnow()
        rows = (db.session.query(UserRotation)
                .filter(UserRotation.enabled.is_(True),
                        UserRotation.next_round_at.isnot(None))
                .all())
        for cfg in rows:
            if cfg.next_round_at is None or now < cfg.next_round_at:
                continue
            try:
                result = run_round(cfg.user_id, cfg.devices_per_round)
            except Exception as e:  # noqa: BLE001
                print(f"[rotation] 用户 {cfg.user_id} 轮转失败: {e}", flush=True)
                result = {}
            schedule_next(cfg)
            db.session.commit()
            print(f"[rotation] 用户 {cfg.user_id} 第 {cfg.round_index} 轮: {result}", flush=True)
