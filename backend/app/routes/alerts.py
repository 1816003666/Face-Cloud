"""告警系统路由：仅 admin 及以上可见。

三类告警：
    device_offline      设备离线（调度器心跳检测）
    resource_limit      资源超限（CPU/内存超过阈值，调度器检测）
    operation_failure   操作失败（设备操控 / 脚本回放 / 任务执行失败自动产生）

提供：列表(可筛选) / 汇总统计 / 确认(ack) / 解决(resolve)。
"""
from datetime import datetime
from flask import jsonify, request, Blueprint, g
alerts_bp = Blueprint("alerts", __name__)

from ..extensions import db
from ..models import iso_utc, Alert
from ..auth import require_role, require_perm


@alerts_bp.get("/alerts")
@require_perm("alert.view")
def list_alerts():
    q = db.session.query(Alert)

    status = request.args.get("status")
    if status:
        q = q.filter(Alert.status == status)
    level = request.args.get("level")
    if level:
        q = q.filter(Alert.level == level)
    atype = request.args.get("type")
    if atype:
        q = q.filter(Alert.type == atype)
    device_id = request.args.get("device_id", type=int)
    if device_id:
        q = q.filter(Alert.device_id == device_id)

    limit = min(int(request.args.get("limit", 200)), 500)
    rows = q.order_by(Alert.id.desc()).limit(limit).all()
    return jsonify([_ser(a) for a in rows])


@alerts_bp.get("/alerts/summary")
@require_perm("alert.view")
def alerts_summary():
    """顶部看板：总数 / 未解决数 / 严重 / 警告 / 按类型分布。"""
    from sqlalchemy import func
    total = db.session.query(func.count(Alert.id)).scalar() or 0
    active = db.session.query(func.count(Alert.id)).filter(
        Alert.status != "resolved").scalar() or 0
    critical = db.session.query(func.count(Alert.id)).filter(
        Alert.level == "critical", Alert.status != "resolved").scalar() or 0
    warning = db.session.query(func.count(Alert.id)).filter(
        Alert.level == "warning", Alert.status != "resolved").scalar() or 0
    by_type = (db.session.query(Alert.type, func.count(Alert.id))
               .filter(Alert.status != "resolved")
               .group_by(Alert.type).all())
    return jsonify({
        "total": total,
        "active": active,
        "critical": critical,
        "warning": warning,
        "by_type": {t: c for t, c in by_type},
    })


@alerts_bp.post("/alerts/<int:aid>/ack")
@require_perm("alert.handle")
def ack_alert(aid):
    a = db.session.get(Alert, aid)
    if a is None:
        return jsonify(error="告警不存在"), 404
    a.status = "acknowledged"
    a.acknowledged_by = g.current_user.id
    db.session.commit()
    return jsonify(_ser(a))


@alerts_bp.post("/alerts/<int:aid>/resolve")
@require_perm("alert.handle")
def resolve_alert(aid):
    a = db.session.get(Alert, aid)
    if a is None:
        return jsonify(error="告警不存在"), 404
    a.status = "resolved"
    a.resolved_at = datetime.utcnow()
    a.resolved_by = g.current_user.id
    db.session.commit()
    return jsonify(_ser(a))


def _ser(a: Alert):
    return {
        "id": a.id,
        "level": a.level,
        "type": a.type,
        "message": a.message,
        "device_id": a.device_id,
        "status": a.status,
        "detail": a.detail,
        "created_at": iso_utc(a.created_at),
        "resolved_at": iso_utc(a.resolved_at),
    }
