"""操作审计路由：仅 admin 及以上可查看。

支持过滤（actor_id / action / target_type / 起止时间）与高频操作统计。
"""
from flask import jsonify, request, Blueprint
audit_bp = Blueprint("audit", __name__)

from ..extensions import db
from ..models import AuditLog
from .. import tenant
from ..auth import require_role, require_perm

from datetime import datetime
from sqlalchemy import func


def _iso_utc(dt):
    """库里存的是 naive UTC，输出给前端必须带 Z，否则会被当成本地时间（差 8 小时）。"""
    from ..models import iso_utc
    return iso_utc(dt)



@audit_bp.get("/audit")
@require_perm("audit.view")
def list_audit():
    query = tenant.scope(db.session.query(AuditLog), AuditLog)

    actor_id = request.args.get("actor_id")
    if actor_id:
        query = query.filter(AuditLog.actor_id == int(actor_id))
    action = request.args.get("action")
    if action:
        query = query.filter(AuditLog.action == action)
    target_type = request.args.get("target_type")
    if target_type:
        query = query.filter(AuditLog.target_type == target_type)
    start = request.args.get("start")
    if start:
        query = query.filter(AuditLog.created_at >= datetime.fromisoformat(start))
    end = request.args.get("end")
    if end:
        query = query.filter(AuditLog.created_at <= datetime.fromisoformat(end))

    limit = min(int(request.args.get("limit", 200)), 500)
    logs = query.order_by(AuditLog.id.desc()).limit(limit).all()
    return jsonify([_serialize(l) for l in logs])


@audit_bp.get("/audit/stats")
@require_perm("audit.view")
def audit_stats():
    """高频操作过滤：统计各 action 出现次数，便于快速定位高频操作。"""
    rows = (db.session.query(AuditLog.action, func.count(AuditLog.id))
            .group_by(AuditLog.action).order_by(func.count(AuditLog.id).desc())
            .limit(20).all())
    return jsonify([{"action": a, "count": c} for a, c in rows])


def _serialize(l: AuditLog):
    return {
        "id": l.id,
        "actor_id": l.actor_id,
        "action": l.action,
        "target_type": l.target_type,
        "target_id": l.target_id,
        "detail": l.detail,
        "created_at": _iso_utc(l.created_at),
    }
