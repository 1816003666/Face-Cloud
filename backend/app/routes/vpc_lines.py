"""VPC 出口线路：SOCKS5 管理。挂在 /api/vpc-lines，不占用客户 VPC 的 /api/vpc。"""
import re
import time
from datetime import datetime

from flask import Blueprint, g, jsonify, request

from .. import orchestrator
from ..auth import require_role, require_perm
from ..extensions import db
from ..models import Vpc, VpcLine, record_audit

vpc_lines_bp = Blueprint("vpc_lines", __name__)

_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_last_op_time: dict[int, float] = {}
_THROTTLE_SECONDS = 30


def _utcnow():
    return datetime.utcnow()


def _check_throttle(lid: int) -> str | None:
    now = time.time()
    last = _last_op_time.get(lid, 0)
    if now - last < _THROTTLE_SECONDS:
        remain = int(_THROTTLE_SECONDS - (now - last))
        return f"操作过于频繁，请 {remain} 秒后重试"
    return None


def _record_op(lid: int):
    _last_op_time[lid] = time.time()


def _valid_ipv4(host: str) -> bool:
    if not _IPV4_RE.match(host or ""):
        return False
    return all(0 <= int(p) <= 255 for p in host.split("."))


def _line_dict(line: VpcLine) -> dict:
    data = line.to_dict()
    data["vpc_count"] = db.session.query(Vpc).filter_by(vpc_line_id=line.id).count()
    return data


@vpc_lines_bp.post("/vpc-lines/dev-proxy")
@require_perm("line.manage")
def deploy_dev_proxy():
    """没有现成 SOCKS5 时：在云手机服务器拉起本机测试代理，并登记为出口线路。"""
    result = orchestrator.ensure_dev_socks5()
    if not result.get("ok"):
        return jsonify(error=result.get("error") or "部署测试代理失败"), 501
    host = result["host"]
    port = int(result["port"])
    line = db.session.query(VpcLine).filter_by(host=host, port=port).first()
    created = False
    if line is None:
        line = VpcLine(
            name="本机测试代理",
            host=host,
            port=port,
            username="",
            password="",
            status="stopped",
        )
        db.session.add(line)
        created = True
    else:
        line.name = line.name or "本机测试代理"
    db.session.commit()
    record_audit(g.current_user.id, "deploy_vpc_dev_proxy", "vpc_line", line.id,
                 {"host": host, "port": port, "created": created})
    body = _line_dict(line)
    body["created"] = created
    body["hint"] = "测试代理仅监听服务器 127.0.0.1，出口 IP 通常与直连相同，用于验证绑定。"
    return jsonify(body), 201 if created else 200


@vpc_lines_bp.get("/vpc-lines")
@require_perm("line.manage")
def list_lines():
    rows = db.session.query(VpcLine).order_by(VpcLine.id).all()
    return jsonify([_line_dict(x) for x in rows])


@vpc_lines_bp.post("/vpc-lines")
@require_perm("line.manage")
def create_line():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    host = (data.get("host") or "").strip()
    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        return jsonify(error="端口必须是整数"), 400
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not name:
        return jsonify(error="线路名必填"), 400
    if not _valid_ipv4(host):
        return jsonify(error="host 必须是 IPv4 地址"), 400
    if not (1 <= port <= 65535):
        return jsonify(error="端口需在 1..65535"), 400
    exist = db.session.query(VpcLine).filter_by(host=host, port=port).first()
    if exist:
        return jsonify(error="该 SOCKS5 地址已存在"), 409
    line = VpcLine(
        name=name, host=host, port=port, username=username,
        password=password, status="stopped",
    )
    db.session.add(line)
    db.session.commit()
    record_audit(g.current_user.id, "create_vpc_line", "vpc_line", line.id,
                 {"name": name, "host": host, "port": port})
    return jsonify(_line_dict(line)), 201


@vpc_lines_bp.delete("/vpc-lines/<int:lid>")
@require_perm("line.manage")
def delete_line(lid):
    line = db.session.get(VpcLine, lid)
    if line is None:
        return jsonify(error="线路不存在"), 404
    bound = db.session.query(Vpc).filter_by(vpc_line_id=lid).count()
    if bound:
        return jsonify(error=f"仍有 {bound} 个客户网络使用该线路，请先解除"), 409
    db.session.delete(line)
    db.session.commit()
    record_audit(g.current_user.id, "delete_vpc_line", "vpc_line", lid, {"name": line.name})
    return jsonify(message="已删除")


@vpc_lines_bp.post("/vpc-lines/<int:lid>/test")
@require_perm("line.manage")
def test_line(lid):
    line = db.session.get(VpcLine, lid)
    if line is None:
        return jsonify(error="线路不存在"), 404
    throttle = _check_throttle(lid)
    if throttle:
        return jsonify(error=throttle), 429
    result = orchestrator.test_socks5_line(
        line.host, line.port, line.username or "", line.password or "",
    )
    line.last_test_at = _utcnow()
    line.last_test_ok = bool(result.get("ok"))
    if result.get("ok"):
        line.last_egress_ip = result.get("egress_ip") or ""
        if line.status == "error":
            line.status = "stopped"
    else:
        line.status = "error"
    db.session.commit()
    record_audit(g.current_user.id, "test_vpc_line", "vpc_line", lid,
                 {"ok": result.get("ok"), "egress_ip": line.last_egress_ip})
    _record_op(lid)
    body = _line_dict(line)
    body["ok"] = result.get("ok")
    if not result.get("ok"):
        body["error"] = result.get("error")
        return jsonify(body), 400
    return jsonify(body)


@vpc_lines_bp.post("/vpc-lines/<int:lid>/start")
@require_perm("line.manage")
def start_line(lid):
    line = db.session.get(VpcLine, lid)
    if line is None:
        return jsonify(error="线路不存在"), 404
    throttle = _check_throttle(lid)
    if throttle:
        return jsonify(error=throttle), 429
    result = orchestrator.test_socks5_line(
        line.host, line.port, line.username or "", line.password or "",
    )
    line.last_test_at = _utcnow()
    line.last_test_ok = bool(result.get("ok"))
    if not result.get("ok"):
        line.status = "error"
        db.session.commit()
        return jsonify(error=result.get("error") or "测试未通过，无法运行"), 400
    line.last_egress_ip = result.get("egress_ip") or line.last_egress_ip or ""
    line.status = "running"
    db.session.commit()
    _record_op(lid)
    record_audit(g.current_user.id, "start_vpc_line", "vpc_line", lid,
                 {"egress_ip": line.last_egress_ip})
    return jsonify(_line_dict(line))


@vpc_lines_bp.post("/vpc-lines/<int:lid>/stop")
@require_perm("line.manage")
def stop_line(lid):
    line = db.session.get(VpcLine, lid)
    if line is None:
        return jsonify(error="线路不存在"), 404
    bound = db.session.query(Vpc).filter_by(vpc_line_id=lid).count()
    line.status = "stopped"
    db.session.commit()
    record_audit(g.current_user.id, "stop_vpc_line", "vpc_line", lid, {"name": line.name})
    body = _line_dict(line)
    if bound:
        body["hint"] = f"停止线路不会自动拆掉已挂载的 {bound} 个客户网络，需在对应网络解除出口。"
    return jsonify(body)
