"""导出功能：设备 / 告警 / 审计 导出 CSV（带 BOM，Excel 可直接打开）。"""
import csv
import io
from flask import jsonify, Blueprint, Response, g
exports_bp = Blueprint("exports", __name__)
from ..extensions import db
from ..models import Device, Alert, AuditLog
from ..auth import login_required


def _csv_response(filename: str, header: list, rows: list) -> Response:
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM，Excel 中文不乱码
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename={filename}"})


@exports_bp.get("/export/devices")
@login_required
def export_devices():
    from .. import scoping
    rows = scoping.scope_query(db.session.query(Device), g.current_user).order_by(Device.id).all()
    data = [[d.id, d.name, d.serial, d.backend, d.status,
             d.group.name if d.group else "",
             d.ip or "", d.cpu, d.mem, d.created_at.isoformat() if d.created_at else ""]
            for d in rows]
    return _csv_response("devices.csv",
                         ["ID", "名称", "序列号", "后端", "状态", "分组", "出口IP", "CPU%", "内存%", "创建时间"],
                         data)


@exports_bp.get("/export/alerts")
@login_required
def export_alerts():
    rows = db.session.query(Alert).order_by(Alert.id.desc()).all()
    data = [[a.id, a.level, a.type, a.device_id or "", a.status,
             a.message, a.created_at.isoformat() if a.created_at else "",
             a.resolved_at.isoformat() if a.resolved_at else ""]
            for a in rows]
    return _csv_response("alerts.csv",
                         ["ID", "级别", "类型", "设备ID", "状态", "信息", "创建时间", "解决时间"],
                         data)


@exports_bp.get("/export/audit")
@login_required
def export_audit():
    rows = db.session.query(AuditLog).order_by(AuditLog.id.desc()).limit(5000).all()
    data = [[a.id, a.actor.username if a.actor else "", a.action,
             f"{a.target_type}#{a.target_id}" if a.target_id else a.target_type,
             a.detail, a.created_at.isoformat() if a.created_at else ""]
            for a in rows]
    return _csv_response("audit.csv",
                         ["ID", "操作者", "动作", "对象", "详情", "时间"],
                         data)
